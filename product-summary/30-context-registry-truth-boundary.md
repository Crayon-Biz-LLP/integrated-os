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
| Pre-Flight Briefing | `PRE_FLIGHT_CONFIG` | hard | requires_anchor | Returns empty context unless entities are resolved from query |
| Daily Briefing | `BRIEFING_CONFIG` | soft | enabled | Scoped to known entities |
| Hindsight Retrieval | `HINDSIGHT_CONFIG` | soft | enabled | Past memories for current tasks |
| Hydrate Tasks | `HYDRATE_TASKS_CONFIG` | none | disabled | Tasks by text_search only |
| Hydrate Memories | `HYDRATE_MEMORIES_CONFIG` | soft | enabled | Memory lookup with entity gating |
| Brain Synth | `BRAIN_SYNTH_CONFIG` | soft | enabled | Entity memory for canonical pages |

### Entity Resolution
- Graph node lookup (`graph_nodes` table, types: person/organization/project)
- Word-level matching (not full-label substring) handles test prefixes
- Matched query terms appended to `query_entities` for gate overlap checks
- Fallback: capitalized words in query text as entity hints

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

## Test Coverage

| Suite | Tests | Type | Scope |
|---|---|---|---|
| `tests/sim/test_context_registry.py` | 8 | LIVE_DB | T1–T8: gates, anchor resolution, ranking |
| `tests/sim/test_simulated_flows.py` | 11 | LIVE_DB | T9–T14: claims, JSON fail, session continuity |
| `tests/unit/test_context_registry.py` | 7 | Unit | Gate logic, isolation, neutral context |
| `tests/unit/test_actions.py` | 6 | Unit | Render, validate, lifecycle |

Run: `LIVE_DB=true PYTHONPATH=. pytest -c /dev/null -o asyncio_mode=auto tests/sim/ -v`
