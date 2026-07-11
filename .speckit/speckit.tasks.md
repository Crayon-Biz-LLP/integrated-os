# Rhodey OS — Task Backlog (Spec Kit Format)
> Ordered by priority. Dependencies listed. Each task is self-contained.

---

## Tier 0 — STOP THE BLEEDING (Do This Week)

### [COMPLETED] T-001: Add try/except to handle_confident_note()
**File**: `core/webhook/handler.py`
**Depends on**: Nothing
**Risk**: Low — additive change
**Deploy safe**: YES

```
IF get_embedding() throws:
  → set raw_dumps.status = 'embedding_failed'
  → print error (temporary until audit log exists)
  → return Telegram receipt "✅ Captured. Memory indexing will retry shortly."
  → DO NOT mark as completed
```

---

### [COMPLETED] T-002: Create system_audit_logs table
**File**: New Supabase migration
**Depends on**: Nothing
**Risk**: Zero — additive
**Deploy safe**: YES

```sql
CREATE TABLE system_audit_logs (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  function_name TEXT NOT NULL,
  event_type TEXT CHECK (event_type IN ('error', 'warning', 'info', 'retry', 'dlq_write')),
  message TEXT,
  raw_input TEXT,
  created_at TIMESTAMPTZ DEFAULT NOW()
);
```

---

### [COMPLETED] T-003: Create dead_letter_queue table
**File**: New Supabase migration
**Depends on**: T-002
**Risk**: Zero — additive
**Deploy safe**: YES

```sql
CREATE TABLE dead_letter_queue (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  source_table TEXT DEFAULT 'raw_dumps',
  source_id UUID REFERENCES raw_dumps(id),
  content TEXT,
  failure_reason TEXT,
  retry_count INTEGER DEFAULT 0,
  resolved BOOLEAN DEFAULT FALSE,
  created_at TIMESTAMPTZ DEFAULT NOW()
);
```

---

### [COMPLETED] T-004: Add log_audit() utility function
**File**: `core/webhook/handler.py` (shared utils)
**Depends on**: T-002
**Risk**: Low — additive
**Deploy safe**: YES

```python
def log_audit(function_name, event_type, message, raw_input=None):
    try:
        supabase.table("system_audit_logs").insert({
            "function_name": function_name,
            "event_type": event_type,
            "message": message,
            "raw_input": str(raw_input)[:500] if raw_input else None
        }).execute()
    except Exception as e:
        print(f"[AUDIT LOG FAILED] {function_name} | {message} | {e}")
```

---

### [COMPLETED] T-005: Replace print() errors with log_audit() calls
**File**: `core/webhook/handler.py`, `core/pulse/engine.py`
**Depends on**: T-004
**Risk**: Low
**Deploy safe**: YES — purely additive, no logic change

Scan for every `except` block and add `log_audit()` call before existing handling.

---

## Tier 1 — PIPELINE INTEGRITY (Week 2)

### [COMPLETED] T-006: Introduce 'staged' → 'processed' state machine in raw_dumps
**File**: `core/webhook/handler.py`, Supabase migration
**Depends on**: T-001, T-004
**Risk**: Medium — changes status values used in queries
**Deploy safe**: YES with migration first

```sql
-- Migration: add new status values
ALTER TABLE raw_dumps DROP CONSTRAINT IF EXISTS raw_dumps_status_check;
ALTER TABLE raw_dumps ADD CONSTRAINT raw_dumps_status_check 
  CHECK (status IN ('staged', 'processed', 'embedding_failed', 'noise', 'completed'));
-- 'completed' kept for backwards compat — deprecated, not deleted
```

Modify `handle_confident_note()`:
1. Insert `raw_dumps` with `status: staged`
2. Attempt embedding
3. On success → insert `memories` → update `raw_dumps.status = processed`
4. On failure → log to DLQ → update `raw_dumps.status = embedding_failed`

---

### T-007: Backfill 41 orphaned notes [REMOVED]
**File**: ~~`scripts/backfill_orphaned_notes.py`~~ — script deleted during cleanup; functionality not needed
**Depends on**: T-006, T-002, T-003
**Risk**: Medium — touches production data
**Deploy safe**: RUN ONCE manually, NOT in CI

```python
# Finds raw_dumps with status='completed' that have no memories entry
# Attempts embedding + memory insert for each
# Logs success/failure to system_audit_logs
# Puts failures in dead_letter_queue
```

---

### [COMPLETED] T-008: Janitor heartbeat GitHub Action
**File**: `.github/workflows/janitor.yml`
**Depends on**: T-006
**Risk**: Low — read-only queries + Telegram alert
**Deploy safe**: YES

Schedule: `cron: '*/30 * * * *'` with IST business hours filter in Python.
Alert format: `⚠️ Rhodey Janitor: {n} records stalled in pipeline. Check raw_dumps.`

---

## Tier 2 — MEMORY HARDENING (Month 1)

### [COMPLETED] T-009: Temporal Lineage on tasks table
**File**: New Supabase migration
**Depends on**: T-006 stable for 2 weeks
**Risk**: High — schema change to live table
**Deploy safe**: WITH careful migration + backfill

```sql
ALTER TABLE tasks ADD COLUMN is_current BOOLEAN DEFAULT TRUE;
ALTER TABLE tasks ADD COLUMN version INTEGER DEFAULT 1;
ALTER TABLE tasks ADD COLUMN superseded_by UUID REFERENCES tasks(id);
```

Modify task update logic:
1. Relies ENTIRELY on Supabase `BEFORE UPDATE` triggers (`trg_temporal_task_update`).
2. Python application-level versioning (`versioned_update`) was stripped out to prevent primary key churn.
3. Standard `.update()` calls on the tasks table will automatically preserve history into archived rows while keeping the `id` perfectly stable.

---

### [COMPLETED] T-010: Temporal Lineage on canonical_pages
**File**: Supabase migration
**Depends on**: T-009 stable
**Risk**: Medium — additive
**Deploy safe**: YES

Same `is_current` + `version` pattern, completely managed via `BEFORE UPDATE` database trigger. `brain_synth.py` runs standard `.update()` instead of manually overwriting.

---

### [COMPLETED] T-011: Idempotency guard on raw_dumps insert
**File**: `core/webhook/handler.py`
**Depends on**: T-006
**Risk**: Low
**Deploy safe**: YES

```python
# Before insert, check:
duplicate = supabase.table("raw_dumps")\
    .select("id")\
    .eq("content", content)\
    .eq("source", source)\
    .gte("created_at", (datetime.now() - timedelta(seconds=60)).isoformat())\
    .execute()
if duplicate.data:
    return  # Silent discard, already logged
```

---

## Tier 3 — OBSERVABILITY (Month 2)

### T-012: Rhodey OS Health Dashboard (Streamlit or web)
**Depends on**: T-002, T-003, T-006
**Risk**: Zero — read-only view
**Deploy safe**: YES

Tables to surface:
- `raw_dumps` status breakdown (staged / processed / embedding_failed / noise)
- `dead_letter_queue` unresolved count
- `system_audit_logs` last 20 errors
- `memories` total count + growth chart
- `tasks` open vs closed ratio

---

## Deferred Backlog

### TF-001 (P3): Decisions Table
**Status**: Deferred — not urgent
**What**: Create a `decisions` table to track explicit decisions with lifecycle (active/superseded/reversed). Add DECISION category to classifier. Wire pulse to surface active decisions.
**Why**: Currently decisions are implicit in task creation or pulse briefing text. A dedicated table enables querying "what did I decide about X?" without re-reading briefings.
**Depends on**: Graph stabilization (let backfill run clean for 2-4 weeks)

### TF-002 (P4): Graph Edge Expiry
**Status**: Deferred — not urgent
**What**: Add `last_confirmed_at`, `valid_until` columns to `graph_edges`. Monthly pulse check queries edges older than 6 months and asks Danny to verify or retire them.
**Why**: Graph edges from months ago may be stale (e.g., someone changed jobs). Without expiry, the graph accumulates noise that degrades query results.
**Depends on**: Graph running clean for 3+ months

### TF-003 (P5): People Table Enrichment
**Status**: Deferred — not urgent
**What**: Add `org`, `last_interaction_date`, `notes` columns to `people` table. Backfill from WORKS_AT/CLIENT_OF graph edges and memory entities_mentioned data.
**Why**: The people table currently has sparse fields beyond name and strategic weight. Enrichment would enable richer person profiles in the UI and pulse context.
**Depends on**: Graph edge quality confirmed (happens naturally as part of P3 timeline)

## Today's Changes (June 21, 2026)

### T-404: Brain Graph — Danny-Centered Ego Graph with Episode Stream
**Status**: Completed
**Details**: Replaced the legacy D3.js FullGraph with a split-pane Danny-centered brain view:
- **New `/api/graph/ego`**: Returns Danny-centered 2-hop ego graph with configurable depth/cap. Uses parallel batched Supabase queries (200 UUIDs per batch) to avoid PostgREST URL length limits. Unbounded 1-hop edge fetch (849 edges), type-priority client-side node sorting.
- **New `/api/graph/neighborhood`**: UUID-safe 1-hop graph from any node. Resolves memory_id through MENTIONS to entity node before fetching.
- **New `/api/graph/resolve-memory`**: Maps memory_id → primary entity via highest-weight MENTIONS edge.
- **NeuralDisc (PixiJS v8)**: Split-pane layout — left: LifeStream → right: interactive WebGL force-directed graph. Danny boots as permanent center. Node click loads neighborhood, background click returns to Danny. Fixed UUID type rot (GraphNode.id is string, not number). AbortController + sequence guard for stale-response defense. Fix: `order by weight desc` was filtering out Danny's real edges — removed LIMIT on 1-hop query. Fix: `.in()` URL length issue — parallel batched queries. Fix: `returnToDanny` race condition. Removed empty-state instructional block.
- **Ego Graph fixed**: Dedup edges by sorted UUID pair + relationship to handle A→B/B→A duplicates. Stable root lookup via `core_config root_entity_id` with ILIKE fallback. All capped queries use ORDER BY for determinism.
- **Infinite loop fix**: `onDiagnostics` callback was inline, causing NeuralDisc's render effect to rebuild the PIXI scene on every React render (60+ FPS → GPU/CPU flood → tab crash). Fixed by wrapping in `useCallback` + reading all callback props through stable refs (onNodeClickRef, onBackgroundClickRef, onDiagnosticsRef). Render effect dep array reduced from 10 to 5, removing callback props and `nodes` (position data flows through layoutData).
- **Episode stream**: New `/api/episodes/stream` endpoint clusters graph-linked memories into episodes using 3 signals: shared non-root entity overlap (within 2h), same source metadata (within 1h), same memory_type (within 30min). Union-find transitive closure for overlapping clusters. **Critical fix**: Original clustering used all entity IDs including Danny (root), which caused every memory to merge into one "About Danny" episode — most memories share Danny. Now fetches root_entity_id from core_config and excludes it from the overlap check. EpisodeStream component replaces raw LifeStream in graph page.
- **Zoom/Pan**: NeuralDisc now uses a mainContainer wrapper for all scene objects. Mouse wheel zooms toward cursor. Background drag pans the graph. Click/drag detection: <5px drag → background click (return to Danny), ≥5px → pan. Zoom controls overlay (+/-/Fit buttons).
- **Collapsible sidebar**: Left pane toggles between 320px and 0 via `PanelLeftClose`/`PanelLeft` button in toolbar, giving the graph full viewport width when hidden.

### T-405: Future Graph / Stream Improvements (Backlog)
**Status**: Deferred
**Details**: Four enhancements identified post-launch for the brain graph page:

1. **PIXI Object Pooling**: Currently every scene rebuild destroys and recreates all PIXI Graphics objects. Implement object pooling (reuse existing Graphics instances, update positions/scales in place) to make hover-only passes and zoom/pan nearly instant (~0ms allocation). Critical for smooth interaction with 100+ node graphs.

2. **Smooth Zoom/Pan Animations**: Currently zoom-to-fit and +/- buttons snap instantly. Add spring-physics tweening via PIXI ticker or a lightweight easing function so that "Fit" and zoom level changes glide smoothly to the target transform.

3. **Multi-Select + Expand-in-Place Nodes**: Currently clicking a node replaces the entire graph with that node's neighborhood. Add Shift-click multi-select to highlight multiple nodes simultaneously. Add expand/collapse toggle on individual nodes to load 2-hop neighbors without leaving the current graph view.

4. **Episode Stream Infinite Scroll + Date Range**: Currently the episode stream loads a fixed batch (up to 80 memories grouped). Add true infinite scroll pagination (offset/cursor-based) and date-range filtering so users can browse weeks/months of clustered history without overwhelming the initial load.

---

## Dependency Map

```
T-002 (audit table)
  └─ T-004 (log_audit function)
       └─ T-001 (fix handle_confident_note)
       └─ T-005 (replace print() errors)
            └─ T-006 (staged/processed state machine)
                 └─ T-007 (backfill orphaned notes)
                 └─ T-008 (janitor heartbeat)
                 └─ T-011 (idempotency guard)
                      └─ T-009 (temporal lineage tasks)
                           └─ T-010 (temporal lineage canonical_pages)
                                └─ T-012 (health dashboard)

T-003 (DLQ table) — parallel to T-002, required by T-006
```


## Today's Changes (June 19, 2026)

### T-403: Associative Retrieval Engine — Full Rollout
**Status**: Completed
**Details**: Replaced the legacy pgvector-only `match_memories_hybrid` path with a 7-signal associative retrieval pipeline:
- **7 new retrieval tables**: `retrieval_passages`, `retrieval_phrase_nodes`, `retrieval_node_stats`, `retrieval_passage_phrase_links`, `retrieval_memory_bundle_links`, `retrieval_alias_edges`, `retrieval_index_runs`.
- **7-signal ranking**: Semantic (embedding cosine), PPR (graph traversal), recency, importance, project boost, specificity (node degree), person_boost — configurable weights in `core/retrieval/ranking.py`.
- **Parallel query analysis**: LLM entity extraction (Gemini Flash Lite) + lexical word n-grams run concurrently via `asyncio.gather()`.
- **Redis caching**: SHA-256 keyed cache for LLM extraction (1h TTL) and embeddings (24h TTL) — warm path eliminates ~3.5s of Gemini calls.
- **GIN trigram index**: `idx_phrase_nodes_text` on `normalized_text` using `gin_trgm_ops` — phrase lookups at ~5ms from ~80ms.
- **Multi-key failover**: Embedding layer (`core/llm/embedding.py`) iterates `get_gemini_clients()` on 429 errors instead of exponential backoff.
- **PostgREST nested joins**: Collapsed N+1 queries via `!inner` syntax — 4 DB roundtrips → 1.
- **asyncio.to_thread()**: All sync DB calls wrapped to avoid blocking event loop.
- **Alias edge backfill**: 3,760 heuristic edges upserted bridging synonymous labels.
- **Forward indexing**: `schedule_index_memory()` wired into all 13 memory insertion paths — every new memory auto-indexes.
- **Production backfill**: 470 memories indexed across all types (note, Journal, outcome, reflection, relationship_note, Prayer, Prophecy, Psalm, archive).
- **4 per-site feature flags** all ON: `RETRIEVAL_ASSOCIATIVE_ENTITY_SUMMARY`, `RECENT_MEMORIES`, `HINDSIGHT`, `HYDRATE`, plus `RETRIEVAL_INDEXING_ENABLED`.
- **Performance**: Cold path 3.5–5.0s (was ~23s baseline), warm path 1.8–3.5s (was ~9s pgvector). Eval runs #7–14 validated progressive optimization.
- **HINDSIGHT_STALE logic**: Three-way COMPASS TONE (HINDSIGHT_STALE / HINDSIGHT_EMPTY / neither).
- **`get_gemini_client()` singleton removed** — all consumers now use `get_gemini_clients()` for multi-key rotation.

## Today's Changes (June 16, 2026)

### T-402: LLM Layer Consolidation — Eliminate All Duplicated Code
**Status**: Completed
**Details**: Eliminated 11 patterns of code duplication across 45+ files:
- **Supabase clients**: Removed 17 redundant `create_client()` calls across app code. All now use `from core.services.db import get_supabase`.
- **Gemini clients**: Single source in `core/llm/client.py`. Added `get_gemini_clients()` supporting up to 3 API keys (`GEMINI_API_KEY`, `GEMINI_API_KEY_2`, `GEMINI_API_KEY_3`) with transparent failover on `429`/`RESOURCE_EXHAUSTED`.
- **Google credentials**: Unified under `core/services/google_service.py::get_google_creds()`. Removed inline OAuth re-creation in `email.py`, `call_ingest.py`, `renew_drive_channel.py`.
- **Fallback chain**: `backfill_graph.py` no longer maintains its own LLM fallback chain. Deleted ~200 lines of duplicated retry/embedding/fallback code. Now delegates to `core/llm/compat.py`.
- **Double rate limiter**: Removed redundant `flash_lite_limiter.acquire_async()` in `fallback.py` — the single call in `providers.py` is sufficient. Effective throughput doubled.
- **Model constants**: `CLASSIFICATION_MODEL`, `EMBEDDING_DIMENSION`, `GEMMA_FALLBACK_MODEL`, `OPENROUTER_MODEL`, `RETRYABLE_ERRORS`, `NON_RETRYABLE_ERRORS` all centralized in `core/llm/constants.py`.
- **Pending decision handlers**: Deleted 3 redundant files (`call.py`, `whatsapp.py`, `teams.py` — ~300 lines total). All channels now route through `core/webhook/utils.py::process_channel_pending_decision()`.
- **Hardcoded model strings**: Replaced `"gemini-3.5-flash"` and `"gemini-3.1-flash-lite"` with `SYNTHESIS_MODEL` and `CLASSIFICATION_MODEL` imports in `compat.py`, `backfill_graph.py`, and concept sweep scripts.
- **Dead code**: Removed unused `supabase_url`/`supabase_key` vars in `temporal_lineage.py`. Removed all duplicate constants from `core/pulse/llm.py`.

## Today's Changes (June 15, 2026)

### T-401: Knowledge Graph Hardening (Layers 1-4) + Concept Fluidity
**Status**: Completed
**Details**: Executed a massive 4-session upgrade to the graph architecture:
- **Layer 1-2 (Schema + Guardrails)**: Purged legacy nodes. Added temporal (`valid_from`, `valid_until`) and epistemic (`epistemic_status`) tracking. Replaced BANNED_RELATIONSHIPS with `VALID_EDGE_MATRIX` positive allowlist. Built zombie triggers for task/project completion to auto-close graph edges.
- **Layer 3 (Context Salience Engine)**: Deployed `get_context_for()` bidirectional recursive CTE in Postgres. Built Python token-budgeted `assemble_context()` packer and `compute_css()` math model ($ln(1+count) \times e^{-\lambda t} \times W_{dist} \times W_{epi}$).
- **Layer 4 (Active Reasoning)**: Wired email triage and Morning Pulse to use `assemble_context()` instead of flat task dumps. Activated Clarifier Phase 2 with similarity dedup checks.
- **Concept Fluidity (Synaptic Plasticity)**: Upgraded ontology to support `concept` nodes. Added `EVOKES`, `RELATES_TO`, `ASSOCIATED_WITH` relationships to `VALID_EDGE_MATRIX`. Built and ran `concept_sweep_batch.py` to extract abstract concepts from all 416 historical memories.
- **Frontend Upgrades**: Updated Next.js `node-pending-list` and `graph-pending-list` to surface `eval_context` (justification, linked entities, memory source) and `epistemic_status` in a collapsible UI. Built proactive `find_similar_node` detection that flags 85%+ label matches and offers a 1-click `[Merge into this]` button before approval. Added `processing_log` table for idempotency tracking.

## Completed Features (Recent)

### T-021: Voice Memo → Note Pipeline
**File**: `core/webhook/multimodal.py`
**Status**: Completed
**Change**: Audio files now use an audio-aware extraction prompt ("Transcribe this audio message exactly as spoken") instead of the image OCR prompt. Audio transcripts skip the `ALT IMAGE:` prefix and use `extraction_method: voice_memo`. This allows voice memos to flow cleanly through the NOTE classification pipeline.

### T-022: Classifier Tuning for NOTE Intent
**File**: `core/webhook/classify.py`
**Status**: Completed
**Change**: Added three explicit NOTE rules to the classification prompt:
- MEETING NOTES & OBSERVATIONS → NOTE (not COMPLETION)
- PROJECT UPDATES → NOTE (not TASK)
- IDEAS → NOTE (not TASK)
This enables natural-language note capture without special syntax.

### T-023: Evening Roundup Endpoint
**File**: `api/index.py` → `/api/roundup`
**Status**: Completed
**Change**: New scheduled endpoint that sends an evening Telegram prompt: "🌆 Evening roundup — any meeting notes, ideas, or project updates from today?" Includes anti-nag guard (skips if ≥3 notes already captured today). Secured via `x-pulse-secret`. Scheduled via cron-job.org at 2PM and 8PM IST.

### T-024: `/note` Command with Entity Extraction
**File**: `core/webhook/handler.py`
**Status**: Completed
**Change**: New `/note <text>` Telegram command. Runs classifier normally (extracts entity/project/person metadata), then overrides `intent → NOTE`, `confidence → 1.0`, `receipt → 🧠`. Preserves all downstream processing. Empty `/note` sets `WAITING_FOR_NOTE` session flag with 5-minute timeout for continuation.

### T-019: Fix pending graph node label-drift duplicates
**File**: `core/skills/backfill_graph.py`, Supabase migration
**Status**: Completed
**Trigger**: Five graph nodes (Paulsons Ledgers, Appa, Paulsons, Dev Team, Henry Dsouza) kept reappearing in the Decision Pulse even after approval.
**Root cause**: Two independent triggers:
- **Label drift**: Backfill re-extracted entities with slightly different labels ("Paulsons" vs "Paulsons Ledgers"). `pending_entities_cache` only tracked `status == 'pending'` with exact match, so it missed the approximate match and re-inserted.
- **Silent upsert failure**: If the `graph_nodes` upsert after approval failed, the label was absent from `graph_entities` and the next backfill re-submitted it.
**Fix**:
1. `fetch_pending_entities()` now loads labels across all statuses (`pending`, `approved`, `rejected`)
2. `_check_pending_label_exists()` does strict normalised `ILIKE` + fuzzy `ILIKE %label%` fallback (≥6 chars) before insert
3. Unique index `idx_pending_graph_nodes_label_dedup` on `lower(trim(label))` provides hard DB-level constraint

### T-020: Fix "Untitled" email rows + missing classification guard in Decision Pulse
**File**: `core/pulse/engine.py`
**Status**: Completed
**Trigger**: (A) Email rows with NULL `suggested_title` showed as "Untitled" in the Decision Pulse. (B) 158 `fyi`/`ignored` emails flooded the pulse because the email channel had no classification filter.
**Root cause**: Two bugs from the unified `messages` table merge:
1. The old subject fallback was lost when `suggested_title` went NULL
2. Email (and call) channels were missing the `classification == 'actionable'` guard that WhatsApp and Teams already had
**Fix**:
1. `engine.py`: Added `subject` to the Decision Pulse query, fallback chain: `suggested_title → subject → 'Untitled'`
2. `engine.py`: Added `row.get('classification') == 'actionable'` filter to email and call channels
3. Backfill: `UPDATE messages SET danny_decision = 'skipped' WHERE channel = 'email' AND classification IN ('fyi', 'ignored') AND danny_decision IS NULL;` (158 rows)

## Completed Features (Recent)

### T-301: Graph Ontology Overhaul (Phase 0)
- **Status**: Completed (June 12, 2026)
- **Details**: Rebuilt the knowledge graph from scratch. Removed `concept`, `emotional_state`, `resource`, `task`, `practice`, `cluster` node types. Added `place`, `animal`. Removed `RELATES_TO`, `BELONGS_TO`, `AUTHORED`, `FEELS`, `INVOLVES` edge types. New 16-type ontology: `DISCUSSED_WITH`, `MET_WITH`, `INTRODUCED`, `FRIEND_OF`, `PARENT_OF`, `SPOUSE_OF`, `SIBLING_OF`, `FAMILY_OF`, `PET_OF`, `MENTORS`, `WORKS_AT`, `WORKS_ON`, `CLIENT_OF`, `VENDOR_TO`, `MEMBER_OF`, `SERVES_AT`. `OWNS` kept as programmatic-only for node approval flow. Banned all catch-all relationship types.
- **Deployed**: Schema: `ALTER TABLE ... DROP CONSTRAINT` for removed types. Backfill prompt rebuilt. Graph cleaned: 0 junk edges (all old types deleted), orphaned concept/emotional_state/resource nodes deleted.

### T-302: raw_dumps Excluded from Graph Extraction
- **Status**: Completed (June 12, 2026)
- **Details**: `FETCH_MEMORIES()` now excludes all `raw_dumps` source records — found to produce 100% hallucinated edges. `source_table` column added to `pending_graph_edges` and `graph_edges`. `source_text` formatted as `memories:{id}` instead of `raw:{id}`. MEMORY_TYPES filtered to `Journal, note, outcome, reflection, relationship_note` only.

### T-303: Entity Grounding — No Concept Auto-Create
- **Status**: Completed (June 12, 2026)
- **Details**: `extract_graph_elements()` receives `fetch_known_entities()` list — prompt matches against approved person/org/project nodes. New entities outside the list only created if clearly identifiable place/animal. `_resolve_node()` in `graph.py` returns `None` instead of auto-creating `concept` nodes for missing labels during edge approval. Missing labels now generate a rejection with "create the node first" guidance.

### T-304: People ↔ Graph Nodes Linkage
- **Status**: Completed (June 12, 2026)
- **Details**: `people.graph_node_id` FK → `graph_nodes.id` added. 89/99 people records backfilled via label matching. Enables bidirectional lookup: "who is this person in the graph?" and "what graph edges touch this person?"

### T-305: Commitments on Tasks
- **Status**: Completed (June 12, 2026)
- **Details**: `tasks.direction` (inbound/outbound/waiting_on), `committed_to` (person name), `committed_on` (timestamp) columns added. `quick_process.py` enhanced to extract these during NOTE classification. Pulse engine queries tasks with direction/committed_to and highlights outbound and waiting_on commitments in briefing. Classifier updated: "meetings this week?" routes to QUERY (not DAILY_BRIEF).

### T-306: Sentiment on Memories
- **Status**: Completed (June 12, 2026)
- **Details**: `memories.sentiment_score` (REAL, -1.0 to +1.0), `sentiment` (TEXT label), `entities_mentioned` (TEXT[]) columns added. Extracted at ingestion time by Flash Lite during NOTE classification. Emotions live on memory metadata, not graph nodes (the FEELS edge type was removed).

### T-307: Decisions UI — Graph Edges Tab
- **Status**: Completed (June 12, 2026)
- **Details**: New Decisions dashboard module with Graph Edges tab. `graph-pending-list.tsx` component: inline editing for source_label, target_label, relationship before approving. Backend `POST /api/graph-edge-action` endpoint for Approve/Edit/Reject. Badge count on tab showing total pending.

### T-308: RLS on Sensitive Tables
- **Status**: Completed (June 12, 2026)
- **Details**: RLS enabled and policies created for `pending_graph_edges`, `pending_graph_nodes`, `messages`, `system_audit_logs`, `dead_letter_queue`. Service role key bypasses RLS — server-side code unaffected.

### T-101: Unify Message Tables (Phase 1-4)
- **Status**: Completed
- **Details**: Dropped `emails`, `whatsapp_messages`, `email_pending_tasks`, `call_pending_items`. Created unified `messages` table with `channel` discriminator. Updated Python backend and Next.js frontend queries. Dropped old sequences.

### T-102: Microsoft Teams Ingestion (Phase 5)
- **Status**: Completed
- **Details**: Added `core/skills/teams_ingest.py` for scheduled pull of Teams chats via Graph API. Includes full document extraction (PDF, DOCX, XLSX) and SharePoint attachment download fix using `/shares/` API.

### T-103: Graph Node NLP Correction Flow
- **Status**: Completed
- **Details**: Built an interactive confirmation loop allowing the user to correct pending graph nodes via natural language (e.g. "g2 is Paulson"). System interprets via Gemini, presents a proposal, and waits for explicit `yes` confirmation before DB commit. Hardened against JSON parsing errors and stale sessions.

### T-104: Personal Capture Pipeline
- **Status**: Completed
- **Details**: Four-phase feature enabling capture of Danny's own thoughts (meeting notes, ideas, project updates, voice memos). Phase 1: classifier tuning for NOTE intent. Phase 2: `/api/roundup` evening check-in endpoint. Phase 3: voice memo → note pipeline. Phase 4: `/note` command with entity extraction and empty-state handling.

### T-013: Extract Supabase Schema
**File**: `db/schema.sql`, `db/rpcs.sql`
**Status**: Completed
**Details**: Introspected live Supabase database via `supabase_execute_sql` and `supabase_list_tables`. Dumped tables, columns, defaults, primary keys, foreign keys, and RPC definitions to version control.

## Serendipity Engine Fix (Completed)

### T-014: Wire people and resources to Serendipity
**File**: `core/pulse/memory.py`, `core/webhook/dispatch.py`
**Status**: Completed
**Details**: Mapped `people` and `resources` lists to `graph_nodes` via `label` matches. Appended to `start_node_ids` in `find_serendipity_paths` to enable true multi-hop serendipity across domains (not just task-based). Updated `dispatch.py` to pass `get_people()`.

## Graph Integrity (Completed)

### T-015: Guard A — Orphaned BELONGS_TO edge cleanup
**File**: `core/pulse/graph.py`, `core/skills/backfill_graph.py`
**Status**: Completed
**Details**: Before inserting a new BELONGS_TO edge for a task, delete any existing BELONGS_TO edge with matching `metadata->>task_id`. Applied in both `write_graph_edges_for_task` (live pulse) and `sync_tasks_to_graph` within `backfill_graph.py` (batch task sync). 

### T-016: Guard B — Text-anchoring hallucination prevention
**File**: `core/skills/backfill_graph.py`
**Status**: Completed
**Details**: Added `"CRITICAL RULE: Only extract entities that are explicitly, verbatim stated in the text"` to the LLM prompt. Added Python-level validation that drops any extracted node whose label is not a substring of the source text (case-insensitive). Edges referencing dropped labels are also dropped. "Danny" is always permitted (for AUTHORED edges).

### T-017: HITL — Pending approval for new person/project nodes
**File**: `core/skills/backfill_graph.py`, `core/pulse/engine.py`, `core/webhook/handler.py`, `core/pulse/graph.py`
**Status**: Completed
**Details**: Created `pending_graph_nodes` table. `get_or_create_node()` and `upsert_nodes()` route new `person`, `project`, or `organization` nodes to `pending_graph_nodes` with `status: pending` instead of creating them directly. Decision Pulse queries and surfaces them with `g{id}` inline keyboard. `process_graph_pending_decision()` in `graph.py` handles approve/reject callbacks. In-memory `pending_entities_cache` prevents duplicates during batch runs.

### T-018: Time-aware calendar events + fixed schedule query routing
**File**: `core/webhook/dispatch.py`, `core/webhook/classify.py`
**Status**: Completed
**Details**: Fixed `classify.py` so schedule questions with time ranges (e.g. "meetings this week?") route to `QUERY` instead of `DAILY_BRIEF`, enabling proper date range resolution. Added current-time injection and `[PAST]` tagging for calendar events in both `interrogate_brain()` and `handle_daily_brief()`. Applied strict output formatting (no invented headings, max 600 tokens, mandatory stop sequence) to both code paths.

## Today's Changes (June 22, 2026)

### T-500: Pipeline Integrity — Tier 0/1/2 Hardening
**Status**: Completed
**Details**: Finalized the last remaining Tier 0–2 backlog items that were already built in code but undocumented:
- **raw_dumps status CHECK constraint applied**: Added SQL-level CHECK to enforce valid states (`pending`, `staged`, `processed`, `embedding_failed`, `noise`, `completed`).
- **Temporal Lineage on tasks**: Created `trg_temporal_task_update` PostgreSQL trigger. BEFORE UPDATE inserts the old row as a historical record and increments version, preserving the primary key (no Google Calendar sync breakage).
- **Temporal Lineage on canonical_pages**: Created `trg_temporal_canonical_pages_update` PostgreSQL trigger with same pattern. Brain synth now writes page versions instead of overwriting.
- **memories table schema fix**: `supersedes_id`/`superseded_by` changed from `uuid` to `int8` to match the table's primary key type.
- **Documentation synchronized**: All backlog items marked completed. AGENTS.md, speckit.*, and product-summary/ brought in sync with codebase reality.

## Today's Changes (Jun 27, 2026)

### T-700: Conversational Persistence + Memory Hygiene (Completed)
**Status**: Completed
**Details**: Built conversational thread state, workflow engine, and hardened memory hygiene:

**Conversational Threads & Workflows:**
- `conversation_threads` + `conversation_workflows` tables (migration `db/09_conversation_threads.sql`)
- `resolve_thread()` routing chain in `core/lib/conversation.py`: open workflow → exact entity → prior bot question → general
- `check_and_resume_workflow()` in `core/webhook/workflows.py`: deterministic phrase matcher (set-based, saves LLM call for short replies), LLM fallback, unrelated note preservation (does not cancel workflow), atomic idempotency guard, supersede detection
- Producer wiring in `dispatch.py`: `handle_project_update()`, `handle_confident_task()`, `handle_confident_note()`
- Consumer precedence in `handler.py`: workflow check before classification
- Expiry pruning via Sentinel piggyback (`core/pulse/sentinel.py`)
- 16/16 integration tests passing

**Memory Hygiene:**
- Memory expiry enforcement: `associative_retrieve()` filters `memories.expires_at` post-PPR
- Memory versioning: `version_memory_for_update()` in `core/services/db.py` — archives memory before mutation. Wired into enrichment paths in `dispatch.py` and `completion_handler.py`
- Deletion/index cleanup: `cleanup_memory_retrieval_index()` in `core/retrieval/cleanup.py` — cascading deletion of retrieval tables
- Daily orphan sweep via Sentinel piggyback (`sweep_orphan_retrieval_entries()`, 20h guard)
- Raw dump lifecycle: stale `staged`/`pending` >24h → `abandoned` via Sentinel piggyback
- Migrations cleaned existing orphans in production

**COMPLETION misclassification:**
- Fixed in `classify.py`: pre-filter checks fuzzy analysis field before keyword-based completion matcher runs
- Key rule: "A completion has TWO parts — task identifier and completion action"

**Entity Resolver rewrite:**
- `interrogate_brain()` now uses graph edges instead of conversation history for entity disambiguation
- Parallel LLM calls for each entity class (person, org, project) with graph as data source
- Removed fragile history-based prior-anchoring code

**Query carry-forward:**
- `active_anchor` from entity resolution persisted to thread record
- Loaded by `resolve_thread()` for next message in same thread
- Anaphora prompt enhanced with anchor context

### T-701: Memory Hygiene Defence-in-Depth (Deferred)
**Status**: Deferred
**What**:
1. **Memory versioning bypass potential**: Application-level versioning (`version_memory_for_update()`) is easier to skip than a DB trigger. Consider adding a `BEFORE UPDATE` trigger on `memories` as defence-in-depth once confidence in app-level patterns is established.
2. **Cleanup-by-routine vs cleanup-by-constraint**: Sentinel piggyback orphan sweep (20h window) is pragmatic but not enforced by schema. Consider foreign key + `ON DELETE CASCADE` from retrieval tables to `memories`, or a trigger-based cascade, to eliminate the gap window.

## Today's Changes (June 23, 2026)

### T-600: Comprehensive System Audit & Hardening (Tiers 0-5)
**Status**: Completed
**Details**: Executed a 38-point massive system hardening based on the 6-tier classification pass:
- **Tier 0 (Active Crashes)**: `completion_handler` status values added to `raw_dumps_status_check`. Rotated and redacted plaintext secrets from `config.json` and `frontend/.env.local`. Fixed `.eq('is_current', False)` polarity bug in `context.py` and missing return in `context_salience.py`.
- **Tier 1 (Data Corruption)**: Restored entity extraction in `quick_process.py` (indentation fix). Replaced crash-prone `.format()` with `.replace()` in `extractor.py`/`search.py`. Fixed `auto_approve.py` JSONB metadata overwrite. Removed double-versioning `create_versioned_task` from `calendar.py` and `temporal_lineage.py`. Fixed schema type mismatches (`uuid` to `int8`).
- **Tier 2 (Ghost Record Isolation)**: Added `.eq('is_current', True)` to 10 queries across `utils.py`, `commands.py`, `engine.py`, `memory.py`, `practices.py`, `tools.py`, `email_ingest.py`, `outlook_ingest.py`, `dispatch.py`, and Next.js `route.ts`.
- **Tier 3 (Tests & Deployment)**: Fixed crashing `test_retrieval.py` patches. Deleted stale RPCs from `rpcs.sql`. Created `02_temporal_lineage_triggers.sql` migration. Fixed `package.json` lint script. Pinned `requirements.txt`. Purged orphan `__pycache__` dirs.
- **Tier 4 (Security)**: Plugged exception leaks (`detail=str(e)`) across 12 endpoints. Hardened weak `.endswith()` cron auth. Added Google Drive webhook auth (`X-Goog-Channel-Token` check). Added Next.js dashboard auth guard.
- **Tier 5 (Frontend)**: Fixed NeuralDisc zoom state coupling. Fixed `null as any` onNodeClick types. Fixed D3 teardown loop on dependency change. Fixed duplicate Radix UI SelectItem values in graph-pending.

### T-601: Task Lifecycle Hardening
**Status**: Completed
**Details**: Second hardening pass targeting silent bugs in the completion flow, recurrence logic, Google Calendar sync, and partial batch failures:
- **`recurrence="none"` truthy bug fixed** (`core/pulse/tools.py`): String `"none"` is truthy in Python — non-recurring tasks were entering the recurring skip path. Guard changed to `td.get('recurrence') not in ['none', '']`.
- **UNTIL boundary exhaustion fixed** (`core/pulse/tools.py`): When a recurring series' RRULE UNTIL date is past and no future instances remain (`skip_recurring_instance` returns `"No upcoming instances found"`), the master task is now permanently closed as `done` instead of looping as `todo` forever.
- **404 auto-heal in `sync_to_calendar`** (`core/services/google_service.py`): Externally deleted Google Calendar events trigger DB null + fresh re-provision. Non-404 errors re-raise to prevent clearing valid IDs on transient failures. DB nulled *before* re-provisioning.
- **Partial batch sync visibility** (`core/webhook/completion_handler.py`): `execute_completion_closure` collects failed task IDs and surfaces them to Telegram. Status: `partially_synced`.
- **LLM matcher fallback chain**: Flash Lite → Gemini 3.5 Flash before parking as `awaiting_completion_match`.
- **Ordinal/keyword disambiguation** (`resolve_completion_disambiguation`): Accepts digits, ordinal words ("first", "second"), "n"/"none".
- **Zombie recovery extended** (`core/services/db.py`): `zombie_recovery()` now also resets `processing_completion` orphans stuck > 10 min.
- **Pulse `completed_task_ids` fixed** (`core/pulse/engine.py`): Was dead code — now actually calls `update_task_status()`.
- **11-test integration suite built** (`tests/clusters/`): 7 cluster files covering merge/dedup, deletion/cancellation (2a/2b/2c), lineage integrity, metadata persistence, recurrence boundary, timezone documentation, cross-system partial sync. DB confirmed clean post-suite.
- **Task 247 manually closed**: `recurrence="none"` fix allowed it to complete correctly. Now `done, is_current=true, version=2, supersedes_id=385`.
- **Committed and pushed** to `main` (`06d9c84`).

## Today's Changes (June 28, 2026)

### T-702: Structured Active Anchor & Thread Summarization
**Status**: Completed
**Files**: `core/lib/conversation.py`, `core/webhook/dispatch.py`
**Details**:
- **Richer `active_anchor`**: Upgraded from bare `{id, name}` to structured JSONB with `type` (from `graph_nodes.type`), `last_action`, `last_task_id`, `last_project_id`, `last_org_id`, `last_summary_snippet` (from most recent memory), `last_mentioned_at`. Built `_build_rich_anchor()` helper (`dispatch.py:895-924`).
- **Thread summarization on overflow**: `get_history()` captures overflow pairs when history exceeds 5000 tokens, compresses them into an extractive summary (capped at 800 chars), stores on `conversation_threads.summary`. `get_thread_summary()` loads summary for injection into anaphora prompt. Lazy — first overflow only.
- **History window expanded**: `MAX_HISTORY_TOKENS` 2000 → 5000 (~5-8 exchanges).
- **Anaphora prompt enhanced**: Now receives `Active context` (name + type), `Last activity`, `Recent context` (last memory snippet), and `Earlier in conversation` (thread summary) — providing enough signal to resolve "what's the status on that?" without guessing.
- **All 36 cluster tests passing**: Ruff clean.

## Rhodey Audit (Jun 29, 2026)

### [COMPLETED] T-RHODE-001: X1 — Task cache TTL 30→300
**File**: `core/pulse/context.py:60`
**Status**: Completed
**Change**: `get_all_open_tasks()` cache TTL increased from 30s to 300s (5 min) to reduce Supabase load.

### [COMPLETED] T-RHODE-002: T4 — Skipped instances metadata on recurring tasks
**File**: `core/pulse/tools.py`
**Status**: Completed
**Change**: `skip_recurring_instance()` now stores skipped dates in `task.metadata.skipped_instances[]`. `metadata` added to select columns. `import json` added.

### [COMPLETED] T-RHODE-003: K1 — Abstractive thread summaries
**File**: `core/lib/conversation.py:136-178`
**Status**: Completed
**Change**: `_compress_to_summary()` now uses `call_llm_with_fallback_sync` for abstractive 2-3 sentence summary via Gemini. Fails back to extractive concatenation if LLM unavailable.

### [COMPLETED] T-RHODE-004: D5 — Rate limiter before LLM classify
**File**: `core/webhook/classify.py`
**Status**: Completed
**Change**: `SlidingWindowLimiter` (15 requests / 60s window, redis_key="rhodey:rate_limit:classify") added before the LLM call in `classify_with_llm()`. If estimated wait > 3s, returns `SAFE_HOLD_CLASSIFICATION`. Fail-open on Redis failure (falls through to normal LLM path).

### [PENDING] T-RHODE-005: B1 — Briefing prompt compression 4K→2K
**File**: `core/pulse/engine.py`
**Effort**: ~1h
**Change**: Inject a pre-compression step in `build_pulse_context()` — deduplicate project summaries, truncate individual memory excerpts to 1 sentence each, merge "upcoming tasks" / "recent tasks" / "overdue tasks" into a single compact block. Target: context assembly produces ≤2000 tokens.
**Deploy safe**: YES — purely additive prompt engineering

### [PENDING] T-RHODE-006: B3 — Briefing personalization
**File**: `core/pulse/engine.py`, `core/pulse/context.py`
**Effort**: ~3h
**Change**: Add a `user_profile` TTL cache with key signals:
- Day of week: Sunday = "reflect & plan" tone; Mon-Thu = "execution" tone; Fri = "wrap-up" tone
- Recent sentiment: Pull last 5 memories' sentiment scores, adjust briefing positivity/mitigation framing
- Active context: If `conversation_threads.active_anchor` has value, inject "You were discussing X — here's the update."
**Deploy safe**: YES — additive; no existing behavior changes

### [PENDING] T-RHODE-007: K3 — Proactive thread resumption
**File**: `core/pulse/sentinel.py`, `core/lib/conversation.py`
**Effort**: ~2h
**Change**: In the sentinel nudge, check `conversation_threads` for threads with `updated_at > 4h` and `updated_at < 48h` that have no completed workflow. Send a Telegram nudge: "📌 Anything update on [project/task]?" once per thread, with `last_nudged_at` cooldown on the thread record.
**Deploy safe**: YES — additive, only sends new messages

### [PENDING] T-RHODE-008: K4 — Workflow expiry nudge
**File**: `core/pulse/sentinel.py`, `core/webhook/workflows.py`
**Effort**: ~2h
**Change**: In the sentinel loop, check `conversation_workflows` for active workflows where `expires_at < now + 2h`. Send: "⏳ [workflow name] will expire soon. Still working on this?" with Yes/No inline keyboard.
**Deploy safe**: YES — additive

### [PENDING] T-RHODE-009: T5 — Delegation tracking dashboard
**File**: `core/pulse/context.py`, `api/index.py` (new endpoint)
**Effort**: ~4h
**Change**:
1. New `/api/delegations` endpoint: queries tasks with `direction='waiting_on'`, grouped by `committed_to` person. Returns summary + per-task detail.
2. Pulse context section: "👤 Delegations — waiting on [person] for [n] tasks" with per-person counts.
**Deploy safe**: YES — additive, read-only

### [PENDING] T-RHODE-010: S3 — Energy-aware scheduling
**File**: New module `core/skills/scheduling.py`
**Effort**: ~4h
**Change**:
1. Personality profiling: Track task completion times from `tasks.completed_at` to infer user's productive windows (morning creative vs afternoon admin).
2. Task ordering: Reorder the "Suggested schedule" section in pulse briefings — high-focus tasks during detected peak hours, admin during off-peak.
3. Sunday scheduling: On Sundays, generate a full-week suggested schedule.
**Deploy safe**: YES — additive, opt-in via feature flag

### [PENDING] T-RHODE-011: S5 — Follow-up auto-cancel
**File**: `core/webhook/handler.py`, `core/webhook/dispatch.py`
**Effort**: ~1d
**Change**: When a message comes in about a task with `direction='waiting_on'` and the inbound text indicates resolution ("got it", "yes", "they confirmed"), check if the `waiting_on` task should auto-cancel. Requires:
1. Inbound entity resolution to match `committed_to` name
2. Sentiment + keyword analysis for resolution signal
3. Prompt user: "✅ [person] confirmed [task] — cancel the waiting task?" with Yes/No
**Deploy safe**: NO — needs inbound infra (entity matching + workflow)
**Dependencies**: T-700 (workflows) stable, entity resolution stable

---

### [COMPLETED] T-PHASE9-001: Pre-flight context — use legacy pgvector path
**Files**: `core/context/pipeline.py`, `core/context/config.py`
**Fix**: PRE_FLIGHT_CONFIG now calls `search_memories_compat` with `use_associative=False`, routing to `match_memories_hybrid` RPC (pgvector on `memories.embedding`). Eliminates dependency on associative retrieval index — new memories findable immediately.
**Config**: `top_k=3→12`, `threshold=0.7→0.55`, removed dead `"emails"` fact source.
**Deploy safe**: YES — additive routing change

---

### [COMPLETED] T-PHASE9-002: Index queue — fire-and-forget replacement
**Files**: `core/retrieval/pipeline.py`, `core/pulse/sentinel.py`, `db/10_pending_index_jobs.sql`
**Fix**: `schedule_index_memory` no longer uses `asyncio.create_task` (killed on Vercel return). Instead inserts a synchronous `pending` row into `pending_retrieval_index_jobs`. New `process_pending_index_jobs(max_jobs=2)` sweeps in sentinel piggyback with atomic status claiming, 3-retry dead-letter lifecycle.
**Deploy safe**: YES — additive (old path still exists but defunct)

---

### [COMPLETED] T-PHASE9-003: Entity extraction via graph labels
**File**: `core/context/pipeline.py`
**Fix**: Memory entity extraction uses `known_labels_lower` dict from graph node labels (person/org/project) instead of `\b[A-Z][a-z]+\b` regex. Stops false positives ("Quick", "Friday") and preserves multi-word labels ("Armour Cyber").
**Deploy safe**: YES — isolated to PRE_FLIGHT path

---

### [COMPLETED] T-PHASE9-004: Backfill unindexed handover memories
**File**: Ad-hoc `supabase.table("pending_retrieval_index_jobs").insert(...)` calls
**Work**: Queued 4 pending jobs for memories 1092, 1093, 1110, 1115 at priority=1
**Verification**: Next sentinel run indexes them via `process_pending_index_jobs`

---

### [COMPLETED] T-PHASE9-005: Pre-flight context test suite
**Files**: `tests/sim/test_index_queue.py` (4 tests), `tests/sim/test_preflight_context.py` (2 tests)
**Coverage**: C1 (enqueue), C2 (process completes), C3 (dedupe), C4 (retry→dead_letter), P1 (routing assertion), P2 (entity extraction)
**Updated**: T2 in `test_context_registry.py`, 3 unit test mocks in `test_context_registry.py`

---

### [COMPLETED] T-PHASE10-001: `/why` Decision Audit
**Files**: `core/lib/decision_audit.py`, `core/webhook/why_handler.py`, `core/context/pipeline.py`, `core/webhook/dispatch.py`, `core/webhook/handler.py`, `db/16_decision_audit.sql`
**Fix**: Added structured decision audit logging (`audit_logs` table, `service='decision_audit'`) for 4 stages: classification, routing, context_registry, retrieval. Added conversational `/why` short-circuit in handler. Formats and sends a human-readable explanation of the last bot response.
**Tests**: 8 unit tests + 6 integration tests in `test_why.py`.
**Deploy safe**: YES — additive feature.

---

### [ACTION REQUIRED] T-RHODE-M1: Enable associative retrieval in production
**Action**: User to set `RETRIEVAL_ASSOCIATIVE_ENABLED=true` in Vercel env vars for both backends
**Verification**: After flip, run one manual query like "what about Equisoft?" — compare result quality vs before. Check Vercel logs for 4xx/5xx.
**Dependencies**: None — env var only
**Risk**: LOW — feature-flag gated; fail-open path exists

### [BLOCKED] T-RHODE-M2: Validate associative retrieval results
**Action**: After M1, run before/after query comparison
**Status**: Blocked on T-RHODE-M1
**QA**: Send 3-5 test queries and compare result relevance.

---

## Today's Changes (Jul 2, 2026)

### [COMPLETED] T-MEETILY-001: Desktop Meeting Capture — Meetily Integration
**Files**: `~/meetily-sync.sh`, `~/Library/LaunchAgents/com.meetily.drive.sync.plist`
**Status**: Completed
**Details**: Set up Meetily (Zackriya-Solutions/meetily) on MacBook for desktop meeting recording:
- Meetily records mic + system audio and saves to `~/Movies/meetily-recordings/`
- `rclone` installed and configured with `rhodey-calls:` remote pointing to `Crayon/Rhodey OS/Call Recordings` (folder ID: `1gc_1w33Di7W3JkONiYg_Ie2LBujs47ad`)
- `meetily-sync.sh` script finds all `.mp4` files in Meetily subfolders, renames to parent folder name (ensures uniqueness), and copies flat to Drive
- `com.meetily.drive.sync` launchd watcher runs the script every 2 minutes
- Only `.mp4` files are synced — `metadata.json` and `transcripts.json` stay local
- No code changes to Rhodey — existing `call_ingest.py` pipeline picks up new files from Drive

---

## Today's Changes (Jul 11, 2026)

### [COMPLETED] T-BATCH-001: Smart Batch Enrichment — Multi-Signal Collection
**Files**: `core/webhook/dispatch.py`, `core/prompts/workflow.py`
**Change**: `_run_post_capture_enrichment()` now collects ALL `calendar_event`/`deadline`/`task_imperative` signals instead of `break`ing after the first match. Creates one `batch` workflow with `{"signals": [...]}` payload. Followup message lists every item by number. `calendar_event` added as signal type with `reminder_at` ISO field. Enrichment prompt includes `Current time: {IST datetime}` for relative date resolution.

---

### [COMPLETED] T-BATCH-002: Per-Signal LLM Decision Parsing
**Files**: `core/prompts/workflow.py`, `core/webhook/workflows.py`
**Change**: `build_workflow_resume_prompt()` lists signals by index and asks for per-signal `confirm`/`decline`/`skip`. LLM handles partial approval and catch-all. Deterministic fast path confirms/declines all. `check_and_resume_workflow()` iterates confirmed indices and executes per-signal. `process_single_dump()` handles task creation; no duplicate `accumulate_action()`.

---

### [COMPLETED] T-BATCH-003: Title Fallback Chain
**Files**: `core/webhook/dispatch.py`, `core/webhook/workflows.py`
**Change**: Every signal execution path uses `task_title → proposed_title → title → "New Task"` instead of bare `payload.get("task_title", "New Task")`.

---



## Today's Changes (Jul 1, 2026)

### [COMPLETED] T-PHASE11-001: sync_organizations_to_graph_nodes()
**Files**: `core/skills/backfill_graph.py`
**Change**: New sync function that creates `type='organization'` graph nodes for all `organizations` table rows. Deletes and recreates wrong-type nodes (person→organization, cascading edges). Wired into `__main__` with post-sync count assertion.
**Deploy safe**: YES — runs only on Pulse trigger. Handles cascading edge deletion for wrong-type nodes.

---

### [COMPLETED] T-PHASE11-002: sync_projects_to_graph_nodes()
**Files**: `core/skills/backfill_graph.py`
**Change**: New sync function that creates `type='project'` graph nodes for all `projects` table rows without existing graph_node. Does NOT delete wrong-type nodes (label collision with orgs). Wired into `__main__`.
**Deploy safe**: YES — additive.

---

### [COMPLETED] T-PHASE11-003: Fix sync_people_to_graph_nodes() — skip orphaned entries
**Files**: `core/skills/backfill_graph.py`
**Change**: `sync_people_to_graph_nodes()` now skips people rows where `role` contains `[DELETED]`, `[CHANGED TO ORGANIZATION]`, or `[MERGED INTO`. These orphaned people entries will never have graph nodes recreated.
**Deploy safe**: YES — purely exclusionary.

---

### [COMPLETED] T-PHASE11-004: Exact guard pattern in resolve_canonical_label()
**Files**: `core/lib/graph_rules.py`
**Change**: `resolve_canonical_label()` now:
1. Checks `pending_graph_nodes` rejected entries before returning any person match.
2. Checks `people.role` for `[DELETED]`/`[CHANGED TO ORGANIZATION]`/`[MERGED INTO` — returns `is_rejected=True`.
3. Falls through to `organizations` table before `graph_nodes` (organizations take precedence over generic graph nodes).
4. New shared `normalize_label()` helper for consistent label normalization across all sync functions.
**Deploy safe**: YES — more restrictive matching prevents false positives.

---

### [COMPLETED] T-PHASE11-005: Clean up wrong-type and reappearing graph nodes
**Files**: Manual SQL (deleted 19 graph nodes, blocklisted 19 pending labels, marked 19 orphaned people rows)
**Change**: Four SQL operations:
1. Deleted wrong-type person nodes for Ashraya Chennai Central, Amico, Armour, Auditor (then sync recreated them as organizations).
2. Deleted 15 reappearing deleted nodes (Andrej, Boys, Broadleaf, CPA, Devil, Dilbert, etc.).
3. Blocklisted 19 deleted labels as `rejected` in `pending_graph_nodes`.
4. Marked 19 orphaned people rows with `[DELETED]` suffix in their role field.
**Verification**: Post-sync counts confirmed (105 person nodes, 29 org nodes with db_record_id, 22 project nodes). No dangling edges. Ruff clean.

---

### [COMPLETED] T-ROLE-001: ROLE_UPDATE intent — detect role attributions
**Files**: `core/prompts/classify.py`, `core/webhook/classify.py`, `core/webhook/dispatch.py`, `core/lib/people_utils.py`
**Change**: Added `ROLE_UPDATE` intent to the classification system so that messages like "Marcus Durai is the Pastor of Ashraya Chennai Central" are detected and routed to a dedicated handler that updates `people.role` instead of creating a task or note.
**Details**:
- Classify prompt: Added `ROLE_UPDATE` to intent list, added `person_name`, `role_title`, `org_name` JSON fields, and detection rules for role attribution patterns (including pronoun resolution via conversation history).
- `classify.py`: Added `ROLE_UPDATE` to `INTENT_OPTIONS` (shortcode `ru`) and `INTENT_THRESHOLDS` (high=0.75, low=0.5).
- `dispatch.py`: Added `handle_role_update()` — resolves person via `people` table (ILIKE), falls back to graph_nodes, creates new people entry if needed, updates `role` and `organization_name`, sends Telegram confirmation. Also creates SERVES_AT graph edge when org exists.
- `route_by_intent()`: Wired `ROLE_UPDATE` into handler_map and if/elif chain.
- `people_utils.py`: Added "pastor" to BLOCKLIST_PEOPLE to prevent entity extraction from creating a person node from the role title.
- **Data fixes**: pe6847 (Pastor → LEADS → ACC) rejected as role-title duplicate. pe6866 relationship updated from PASTOR to SERVES_AT for correct resolution. Marcus Durai people.role set to "Pastor of Ashraya Chennai Central".
**Deploy safe**: YES — additive intent. Existing tasks/notes continue to work unchanged.

---

### [COMPLETED] T-CLASSIFY-001: Classification context boundary — prevent bot receipt leakage
**Files**: `core/lib/conversation.py`, `core/prompts/classify.py`, `core/webhook/handler.py`, `tests/sim/test_thread_classification.py`
**Change**: Replaced raw `CONVERSATION HISTORY:` in classify input with `format_classify_context()` — a bounded context block containing thread summary + active entity + preceding user turn only. Bot responses excluded from classify context.
**Details**:
- `format_classify_context()` in `core/lib/conversation.py` — bounded block with THREAD SUMMARY, ACTIVE ENTITY, PRECEDING TURN (user-only)
- `_compress_to_classify_summary()` — separate topic-only summary via gemini-3.1-flash-lite, explicitly forbids action receipts
- `_store_thread_summary_if_missing()` — idempotent via `.is_('summary', 'null')` guard
- `_background_summary_check()` — non-blocking async job fired after bot response insert, fail-open
- Classify prompt: added PERSON QUERIES rule, tightened URL-ONLY regex, "NEVER use this receipt" guard
- Handler: `/note` path and main classify path both use `format_classify_context`
- 7 sim tests (S1-S7): URL + person query, summary present, empty history, anchor in context, pronoun continuation, multi-turn stripping, full end-to-end with real Supabase thread
- Cleanup audited: mock-session inserts blocked by UUID constraint; seeded threads tracked and deleted by UUID; zero orphaned rows verified post-run
**Deploy safe**: YES — additive only, `format_history_for_prompt` unchanged for response generation paths

---

### [COMPLETED] T-RESOURCE-001: Resource Clusters — List View + Dismiss Feature
**Files**: `db/20_resources_dismissed.sql`, `frontend/src/app/dashboard/clusters/clusters-shell.tsx`, `frontend/src/app/dashboard/clusters/page.tsx`, `frontend/src/app/api/resources/route.ts`, `frontend/src/app/api/resources/[id]/dismiss/route.ts`, `frontend/src/lib/resources/api.ts`, `frontend/src/lib/resources/types.ts`, `core/webhook/dispatch.py`, `core/agents/quick_process.py`, `core/pulse/engine.py`
**Change**: Two features on the Knowledge Base page:
1. **List view toggle**: New grid/list toggle in the header. List view is a flat table with Title, Hostname, Category, Cluster dropdown, Date, and Dismiss button per row.
2. **Resource dismiss**: `dismissed_at TIMESTAMPTZ` column added to `resources` table. Dismiss buttons in both the list view rows and the split-pane detail view. Dismissed resources are hidden from the UI (`.is('dismissed_at', null)` filter on all queries). URL dedup in the backend checks `dismissed_at` — if the same URL is re-submitted, Rhodey replies "Already seen this link and dismissed it. Skipping." instead of re-storing it.
**Deploy safe**: YES — additive migration + new API endpoint + hidden behind new UI toggle. Existing resources unaffected.
