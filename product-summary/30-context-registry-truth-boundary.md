# Context Registry & Truth Boundary

## Overview
Two layered subsystems preventing LLM hallucination of unexecuted actions and pre-flight context leakage. The Context Registry gates what context enters the LLM prompt; the Truth Boundary validates what the LLM claims it has done.

---

## Truth Boundary (`core/actions.py`)

Post-generation claim validation layer. Prevents the LLM from asserting it performed actions (created tasks, sent messages, set reminders) without corresponding evidence.

### Architecture
- **`ActionResult` dataclass**: Evidence record with `action_type`, `status`, `details`, `id`
- **Context var accumulator**: `begin_action_context()` / `clear_action_context()` lifecycle — accumulates `ActionResult` across mutation points without signature changes
- **`validate_action_claims()`**: Scans generated text for claim phrases, cross-references against accumulated evidence, rewrites unbacked claims
- **`CLAIM_LEXICON`**: Phrase-family classifier — categorizes claims into `task`, `calendar`, `monitoring`, `communication`, `research` families
- **`RESERVED_ACTION_PATTERNS`**: Regex patterns for in-text claim detection and rewrite
- **`render_actions()`**: Deterministic receipt formatter for evidence display

### Integration
- `send_telegram()` in `core/webhook/telegram.py`: Final send boundary invariant — snapshots evidence, validates, appends receipts, drains context
- Six mutation paths wired: workflows.py, dispatch.py, completion_handler.py, quick_process.py, pulse/tools.py, pulse/memory.py
- Two confirmation states: `awaiting_actionable_confirmation` (action disputes), `awaiting_disambiguation_confirmation` (entity ambiguity)
- Internal messages skip validation via `skip_validation` param

---

## Context Registry (`core/context/`)

Pre-retrieval entity-grounding layer. Controls what context is fetched for each use case, preventing entity-free queries from leaking semantically similar but irrelevant context.

### Architecture
- **`schema.py`**: `ContextResult`, `RetrievalItem`, `GateDecision` typed outputs
- **`config.py`**: 6 named strategies with per-strategy `threshold`, `top_k`, `weight`, `gate_mode`, `semantic_enabled`, `semantic_requires_anchor`, `fact_sources`
- **`gates.py`**: Hard gate (reject cross-entity), soft gate (downrank), none pass-through. Neutral context penalty: 0.5x score
- **`pipeline.py`**: `execute_context_strategy()` — resolves graph node anchors, runs fact sources, runs semantic search (only if anchored when required), applies gates, enforces top_k, logs observability

### Strategies
| Strategy | Config Name | Gate | Semantic | Key Behavior |
|---|---|---|---|---|
| Pre-Flight Briefing | `PRE_FLIGHT_CONFIG` | hard | requires_anchor | threshold=0.45, top_k=12, fact_sources include meeting_minutes keyword pass for entity-ILIKEd memories |
| Daily Briefing | `BRIEFING_CONFIG` | soft | enabled | Scoped to known entities |
| Hindsight Retrieval | `HINDSIGHT_CONFIG` | soft | enabled | Past memories for current tasks |
| Hydrate Tasks | `HYDRATE_TASKS_CONFIG` | none | disabled | Tasks by text_search only |
| Hydrate Memories | `HYDRATE_MEMORIES_CONFIG` | soft | enabled | Memory lookup with entity gating |
| Brain Synth | `BRAIN_SYNTH_CONFIG` | soft | enabled | Entity memory for canonical pages |

### Entity Resolution
- Graph node lookup (`graph_nodes` table, types: person/organization/project)
- Word-level matching (not full-label substring) handles test prefixes and multi-word labels
- Matched query terms appended to `query_entities` for gate overlap checks
- Memory entity extraction uses graph node labels (Fix D) — replaces `\b[A-Z][a-z]+\b` regex which produced false positives ("Quick", "Friday") and missed multi-word labels ("Armour Cyber")

---

## Prompt Registry (`core/prompts/`)

All LLM prompts centralized in dedicated files:

| File | Purpose |
|------|---------|
| `guards.py` | Action integrity guard, read-only query engine instruction |
| `query.py` | Anaphora resolution prompt, brain interrogation prompt |
| `briefing.py` | Daily briefing, sentinel pre-flight prompts |
| `classify.py` | Intent classification prompt |
| `workflow.py` | Disambiguation, confirmation workflow prompts |
| `ingest.py` | Note extraction, entity extraction prompts |

---

## JSON Fail-Closed

Three critical functions now return deterministic safe text on JSON parse failure instead of raw `.text.strip()`:
- `interrogate_brain()` — returns `"I couldn't process that query. Please try again."`
- `handle_daily_brief()` — returns minimal safe briefing
- `process_sentinel()` — returns `"No relevant context found."`

---

## Phase 9 — Pre-Flight Context Fix (Jun 30, 2026)

### The Gap
Handover memories (IDs 1092, 1093) were never indexed for associative retrieval — zero rows in `retrieval_passages`, `retrieval_phrase_nodes`, `retrieval_index_runs`. Root cause: `schedule_index_memory` used `asyncio.create_task(index_memory(...))` which is killed when Vercel serverless returns a response (~2s) before the 15s LLM extraction completes. Additionally, `RETRIEVAL_INDEXING_ENABLED` defaults to `false` (all retrieval features OFF per `config.py`).

Since `associative_enabled=true` in production, `search_memories_compat` called `associative_retrieve()` which queries retrieval tables — not `memories.embedding` directly. New memories were invisible to the sentinel's pre-flight context.

### Fix A — Legacy Vector Path for Pre-Flight
`pipeline.py` passes `use_associative=False` to `search_memories_compat` for `PRE_FLIGHT_CONFIG` only. This uses the `match_memories_hybrid` RPC (pgvector on `memories.embedding` column) instead of associative retrieval. New memories populate `embedding` at creation time (`dispatch.py`) and are findable immediately — no indexing dependency.

### Fix B — Config Tuning (Phase 9)
- `top_k`: 3 → 12 (wider net for pre-flight context)
- `threshold`: 0.7 → 0.55 (lower barrier for marginal matches, further lowered to 0.45 in Phase 10)
- Removed `"emails"` from `fact_sources` (dead source — never wired; re-added in Phase 10 with working implementation)
- `Literal` type annotation on `fact_sources`: cleaned to `"tasks" | "people"`

### Fix C — Index Queue
Replaced fire-and-forget `asyncio.create_task(index_memory(...))` with a reliable queue:
1. `schedule_index_memory()` does a synchronous INSERT into `pending_retrieval_index_jobs` (~5ms)
2. New `process_pending_index_jobs(max_jobs=2)` sweeps pending jobs with atomic status claiming
3. Calls `index_memory()` from the sentinel piggyback (not the webhook response lifecycle)
4. Retry tracking: 3 failed attempts → `dead_letter` status
5. Partial UNIQUE index `idx_pending_index_jobs_memory` prevents duplicate active jobs per memory_id
6. Migration: `db/10_pending_index_jobs.sql`

### Fix D — Graph Label Entity Extraction
Entity extraction from memory content now matches against `known_labels_lower` dict (populated from `graph_nodes` during anchor resolution) rather than the `\b[A-Z][a-z]+\b` regex. Benefits:
- No false positives: "Quick", "Friday", "The", "So" are not treated as entities
- Multi-word labels preserved: "Armour Cyber" matches correctly
- Case-insensitive matching via lowercased labels
- Only person/org/project nodes serve as entity sources — excludes noise

### Backfill
4 pending index jobs queued at `priority=1` for unindexed memories: IDs 1092, 1093, 1110, 1115. Next sentinel run will process them.

---

## Phase 10 — Sentinel Pre-Flight Quality Fix (Jul 8, 2026)

### The Gap
The sentinel's pre-flight briefing was producing generic context like *"Several meetings with Equisoft have been completed, including a recurring meeting, a kickoff call, and weekly catch-up calls"* — a calendar reminder dressed as intelligence. Rich context existed in the DB (IAM meeting minutes with specific decisions, action items, and past-due dates; kickoff notes about red tape; today's briefings mentioning "Equisoft sync tonight") but was not surfacing due to three interacting problems.

### Fix 1 — Sentinel Prompt: Restate → Summarize
**File:** `core/pulse/sentinel.py:175`

Changed the sentinel AI prompt from:
```
Restate only what is shown… Do not guess connections between the context and the meeting.
```
To:
```
Summarize the relevant context for this meeting. You may draw explicit inferences from dates and action items shown (e.g., if a due date is in the past, note it as overdue). Do not fabricate facts not present in the retrieved context.
```

**Why safe:** Pre-flight is a meeting prep use case — the user reviews it before walking in. The Phase 8 truth boundary constraint (designed to prevent hallucination in email drafting and briefings) overshot here. The "not present" guardrail prevents fabrication while allowing "action items are 3 weeks overdue" from visible due dates.

### Fix 2 — `meeting_minutes` Keyword Fact Source
**Files:** `core/context/config.py`, `core/context/pipeline.py`

Added `"meeting_minutes"` to `PRE_FLIGHT_CONFIG.fact_sources`. In `execute_context_strategy()`, a new block runs after the fact sources loop: for each extracted entity name (`query_entities[:3]`), query `memories.content` via `ILIKE '%entity%'`, deduped against the semantic pass via `seen_memory_ids` set. Items tagged with the matched entity so the hard grounding gate keeps them (anchor overlap guaranteed at score 0.9).

**Impact:** 4 weeks of Equisoft IAM meeting minutes (memory 627), kickoff red-tape note (226), and today's "Equisoft sync tonight" briefings now surface regardless of whether the meeting title embedding scores above threshold against the minutes' architecture-focused text.

`RetrievalItem.source` type expanded to include `"meeting_minutes"`. `StrategyConfig.fact_sources` Literal expanded to include `"meeting_minutes"`.

### Fix 3 — Threshold Lowered (Option A Interim)
**File:** `core/context/config.py:37`

`threshold: 0.55 → 0.45` — lower barrier for semantically-similar but phrase-mismatched memories. The `meeting_minutes` keyword pass (Fix 2) serves as the Option B hybrid, so named-entity meetings no longer depend on embedding similarity as the sole retrieval path.

### Effect on Tonight's Equisoft Meeting
Pre-flight for "Recurring meeting with Equisoft" now surfaces:
- IAM meeting minutes (Option 1 connector framework, overdue action items to Keyvan/Cesar/Charles due June 12-15)
- Kickoff context ("lots of red tape at Equisoft, monthly invoicing, 30-day payment terms")
- Today's briefings mentioning "Equisoft sync tonight"
- AI can flag past-due dates rather than emitting generic prose

---

## Test Coverage

| Suite | Tests | Type | Scope |
|---|---|---|---|
| `tests/sim/test_context_registry.py` | 8 | LIVE_DB | T1–T8: gates, anchor resolution, ranking |
| `tests/sim/test_simulated_flows.py` | 11 | LIVE_DB | T9–T14: claims, JSON fail, session continuity |
| `tests/sim/test_index_queue.py` | 4 | LIVE_DB | C1–C4: enqueue, process, dedupe, retry→dead_letter |
| `tests/sim/test_preflight_context.py` | 2 | LIVE_DB | P1: routing assertion (use_associative=False); P2: entity extraction |
| `tests/unit/test_context_registry.py` | 7 | Unit | Gate logic, isolation, neutral context, pre-flight isolation |
| `tests/unit/test_actions.py` | 6 | Unit | Render, validate, lifecycle |

Run: `LIVE_DB=true PYTHONPATH=. pytest -c /dev/null -o asyncio_mode=auto tests/sim/ tests/unit/ -v`
