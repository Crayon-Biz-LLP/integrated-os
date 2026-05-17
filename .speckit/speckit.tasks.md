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

