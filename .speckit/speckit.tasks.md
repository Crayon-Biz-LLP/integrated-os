# Rhodey OS — Task Backlog (Spec Kit Format)
> Ordered by priority. Dependencies listed. Each task is self-contained.

---

## Tier 0 — STOP THE BLEEDING (Do This Week)

### T-001: Add try/except to handle_confident_note()
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

### T-002: Create system_audit_logs table
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

### T-003: Create dead_letter_queue table
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

### T-004: Add log_audit() utility function
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

### T-005: Replace print() errors with log_audit() calls
**File**: `core/webhook/handler.py`, `core/pulse/engine.py`
**Depends on**: T-004
**Risk**: Low
**Deploy safe**: YES — purely additive, no logic change

Scan for every `except` block and add `log_audit()` call before existing handling.

---

## Tier 1 — PIPELINE INTEGRITY (Week 2)

### T-006: Introduce 'staged' → 'processed' state machine in raw_dumps
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

### T-008: Janitor heartbeat GitHub Action
**File**: `.github/workflows/janitor.yml`
**Depends on**: T-006
**Risk**: Low — read-only queries + Telegram alert
**Deploy safe**: YES

Schedule: `cron: '*/30 * * * *'` with IST business hours filter in Python.
Alert format: `⚠️ Rhodey Janitor: {n} records stalled in pipeline. Check raw_dumps.`

---

## Tier 2 — MEMORY HARDENING (Month 1)

### T-009: Temporal Lineage on tasks table
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
1. When a task's content/status changes materially → INSERT new row with `is_current = TRUE`, `version = old_version + 1`
2. Set old row: `is_current = FALSE`, `superseded_by = new_row_id`
3. Never DELETE task rows

---

### T-010: Temporal Lineage on canonical_pages
**File**: Supabase migration
**Depends on**: T-009 stable
**Risk**: Medium — additive
**Deploy safe**: YES

Same `is_current` + `version` pattern. `brain_synth.py` writes a new page version instead of overwriting.

---

### T-011: Idempotency guard on raw_dumps insert
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


## Today's Changes (June 12, 2026)

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
