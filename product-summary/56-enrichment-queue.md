# 56 - Enrichment Queue & Final Architecture Gap Closure

## Overview
The final critical gap from the comprehensive architecture audit: enrichment (graph edges, entity extraction, embedding) was running as fire-and-forget `asyncio.create_task()` calls — which Vercel kills silently when the serverless function returns. Tasks and notes were created in the DB but **never enriched**. Same class of bug as the 61% passage→phrase_node link gap from the original associative retrieval rollout.

This was the **last remaining critical item** from the 31-item architectural audit. All 31 items are now resolved.

## Root Cause
The Action Planner refactoring replaced `process_single_dump()` (which ran enrichment synchronously inline) with `create_task_direct()` / `create_note_direct()`. These new functions used `asyncio.create_task()` for enrichment:

```python
# Old pattern (broken on Vercel):
loop.create_task(_enrich_task_for_graph(task_id, title, project_id))
```

This is a **fire-and-forget** pattern. Vercel's serverless runtime kills all outstanding async tasks when the HTTP response is returned. The enrichment (graph edges, entity extraction, embeddings) **never ran** — the task/note was created in the DB but invisible to the knowledge graph and retrieval system.

## Fix: Queue-Based Enrichment (Same Pattern as pending_retrieval_index_jobs)

### New Table: `pending_enrichment_jobs`
Created by migration `db/42_pending_enrichment_jobs.sql`:
- `job_type`: `task_graph` or `note_enrich`
- `target_type` / `target_id`: polymorphic reference to tasks or memories
- `content`: text to extract entities from
- `related_id`: project_id for tasks, source for notes
- `status`: pending → processing → completed/failed/dead_letter
- `retry_count`: up to 3 before dead_letter escalation
- Atomic claim via `claim_pending_enrichment_job()` RPC (pg_advisory-based, prevents double-processing on concurrent sentinel runs)
- Dedup via partial unique index on `(job_type, target_id, target_type) WHERE status IN ('pending', 'processing')`

### New Module: `core/lib/enrichment_queue.py`
- `enqueue_enrichment()`: Synchronous DB insert (~5ms), always survives Vercel cold kills. Uses SELECT-then-INSERT pattern (same as `schedule_index_memory`) to verify no duplicate pending/processing job exists.
- `process_pending_enrichment()`: Fetches pending jobs, claims atomically via RPC, dispatches by job_type:
  - `task_graph`: `write_graph_edges_for_task()` + `extract_and_link_entities()`
  - `note_enrich`: `extract_and_link_entities()` + `get_embedding()` + memory row update

### Consumer: Sentinel Piggyback (P6)
The sentinel now calls `process_pending_enrichment(max_jobs=3)` every cycle, with a 4-minute throttle to prevent over-processing. Jobs are typically processed within 5 minutes of creation.

### What Changed in `create_task_direct()`
```python
# BEFORE (broken — killed by Vercel):
loop.create_task(_enrich_task_for_graph(task_id, title, project_id))

# AFTER (survives — synchronous queue insert):
from core.lib.enrichment_queue import enqueue_enrichment
enqueue_enrichment(job_type="task_graph", target_type="task",
                   target_id=task_id, content=title, related_id=project_id)
```

### What Changed in `create_note_direct()`
```python
# BEFORE (broken):
loop.create_task(asyncio.to_thread(schedule_index_memory, ...))
loop.create_task(_enrich_note_for_graph(memory_id, content, source))

# AFTER (survives):
schedule_index_memory(memory_id, content, "note", source)  # queue insert
enqueue_enrichment(job_type="note_enrich", target_type="note",
                   target_id=memory_id, content=content, related_id=source)
```

## Additional Gaps Closed This Session

### Calendar Conflict Check Restored
The old `_run_task_syncs()` called `check_conflict()` before `sync_to_calendar()`. This was lost during the refactoring. Restored in `create_task_direct()` — non-blocking, logs conflicts but still creates the event.

### DLQ Write on Creation Failure
Both `create_task_direct()` and `create_note_direct()` now write to `dead_letter_queue` when creation fails, matching the old `handle_confident_task()` / `handle_confident_note()` behavior.

### `expires_at` Restored for Notes
`create_note_direct()` now calls `compute_expires_at()` to set `memories.expires_at` based on time-sensitive phrases ("today", "tomorrow", "this Monday") in the note content.

### dedup_key Hash Ordering Fix
The dedup_key normalization (MD5 hash to 16 chars for `varchar(16)` column compat) was running **after** the dedup check — the check searched for the raw 17-char key while the insert stored the 16-char hash. Moved normalization before the check.

## E2E Verification Results
A comprehensive 25-check end-to-end test was run against the live Supabase database:

| Category | Checks | Passed | Failed | Notes |
|----------|--------|--------|--------|-------|
| Task creation | 8 | 7 | 1 | 1 test assertion bug (expected raw dedup_key vs hashed) |
| Note creation | 8 | 7 | 1 | 1 expected (RETRIEVAL_INDEXING_ENABLED=false) |
| State machine guards | 3 | 3 | 0 | — |
| Validate operation | 4 | 4 | 0 | — |
| DLQ write | 1 | 1 | 0 | — |
| Compensation rollback | 1 | 1 | 0 | — |
| **Total** | **25** | **23** | **2** | Both failures are test-expected, not code bugs |

## Files Changed
- `core/pulse/tools.py` — create_task_direct + create_note_direct: queue enrichment, conflict check, expires_at, DLQ, dedup_key hash
- `core/lib/enrichment_queue.py` (NEW) — queue module with enqueue + processors
- `core/pulse/sentinel.py` — P6 piggyback for enrichment queue
- `db/42_pending_enrichment_jobs.sql` (NEW) — migration: table, indexes, RPC
