# 22. Resilience & Self-Healing Infrastructure

Integrated-OS is built with the expectation that everything will fail eventually. Every subsystem has fallbacks, retries, and recovery mechanisms.

## Temporal Lineage & Versioning

The most fundamental resilience pattern. Instead of updating records in-place, the system creates new versions and marks old ones as `is_current=False`. This is enforced at the **database level** via PostgreSQL `BEFORE UPDATE` triggers, not in application code.

### How It Works

Active tables: `tasks` and `canonical_pages` have database triggers (`trg_temporal_task_update`, `trg_temporal_canonical_pages_update`). When any code path (Python webhook, Next.js API, direct SQL) runs an `UPDATE`:

1. The `BEFORE UPDATE` trigger fires on the old row.
2. It inserts the old state as a new row with `is_current = false` and the same version number.
3. The actual `UPDATE` on the active row increments `version` by 1 and sets `supersedes_id` to the ID of the archived row.

The active row's **primary key never changes** — ensuring Google Calendar sync mappings, React keys in the frontend, and graph edge references remain intact.

The `memories` table has the same columns (`is_current`, `version`, `supersedes_id`, `superseded_by`) correctly typed as `int8`, ready for trigger deployment.

### Key Implementation Detail

The trigger uses `pg_trigger_depth() = 0` to prevent cascading re-entry — if the trigger's INSERT of the historical record fires any other triggers, they are ignored.

### Drift Detection

The `detect_drift()` RPC counts how many times a project has been updated in the last N hours — if it's 3+ times in 48 hours, it flags as a potential bottleneck.

## Dead Letter Queue

Failed operations don't crash the system — they go to the `dead_letter_queue` table with retry tracking via `write_dlq()` in `core/lib/audit_logger.py`.

### Schema

| Column | Purpose |
|--------|---------|
| `source_table` | Which table had the error (raw_dumps) |
| `source_id` | The record's ID (UUID) |
| `content` | The raw content that failed (truncated to 2000 chars) |
| `failure_reason` | The error description (truncated to 1000 chars) |
| `retry_count` | Incremented on each retry attempt |
| `resolved` | Boolean, set to TRUE when Danny resolves via `/dlq resolve <id>` |

### Usage

Writes to the DLQ happen in `handle_confident_note()` when embedding fails:
```python
from core.lib.audit_logger import write_dlq
write_dlq('raw_dumps', str(dump_id), text, 'Embedding failed or returned null vector')
```

Each DLQ write also logs to `system_audit_logs` via `log_audit()` for full traceability.

## Zombie Recovery

If a processing step marks a dump as 'processing' but crashes before completing, the system would have a zombie — a record stuck in 'processing' forever.

The zombie recovery function (`db.py:43-54`) runs at the start of every Pulse and every Quick Process cycle:

```python
ten_mins_ago = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
supabase.table('raw_dumps') \
    .update({"status": "pending"}) \
    .in_('status', ['processing', 'processing_completion']) \
    .lt('created_at', ten_mins_ago) \
    .execute()
```

Any dump stuck in `processing` **or** `processing_completion` for more than 10 minutes is automatically reset to `pending` and re-processed. The `processing_completion` status is set by the completion handler when a completion is in-flight — extending zombie recovery to cover it prevents orphaned completions from blocking the queue indefinitely.

## Pipeline Heartbeat & Health Checks

### Heartbeat

After every successful Pulse run, the engine writes `pulse_last_success` to `core_config`:
```python
supabase.table('core_config').upsert({
    "key": "pulse_last_success", 
    "content": datetime.now(timezone.utc).isoformat()
}).execute()
```

### Health Diagnostics

The Janitor workflow (`core/agents/janitor_check.py`) checks 6 things every ~3 hours:

1. **Stuck raw_dumps**: Dumps in 'pending' or 'staged' for >2 hours
2. **Stuck processing**: Dumps in 'processing' for >10 minutes (sends Telegram alert)
3. **Null embeddings**: Memories from the last 7 days with null embeddings
4. **Heartbeat freshness**: If Pulse hasn't run in >24 hours, sends a warning
5. **Failed queue retry**: Picks up failed operations and retries with exponential backoff
6. **LLM degradations**: Counts `audit_logs` entries with `service='llm', level='WARNING'` in the last hour

### Health Dashboard

The frontend Health module shows these diagnostics in real time, plus failed queue items and error logs from `audit_logs`.

## Janitor Workflow (4x Daily)

The Janitor (`core/agents/janitor_check.py`) is a scheduled health maintenance workflow that runs every ~3 hours via `janitor.yml`. It performs 6 checks:

### Check 1: Stuck Raw Dumps
Queries `raw_dumps` for entries in 'pending' or 'staged' status for more than 2 hours. These are logged to `audit_logs` and notified via Telegram if the count exceeds 5.

### Check 2: Zombie Processing
Same mechanism as the zombie recovery check (10-minute threshold), but runs independently of the Pulse cycle. If a dump is stuck in 'processing' for >10 minutes, a Telegram alert is sent.

### Check 3: Null Embeddings
Queries `memories` from the last 7 days where `embedding IS NULL`. Attempts to regenerate embeddings for these entries. If generation fails, the entries are queued to `failed_queue`.

### Check 4: Heartbeat Freshness
Checks `pulse_last_success` from `core_config`. If more than 24 hours since last successful Pulse run, sends a Telegram warning.

### Check 5: Failed Queue Retry
Picks up failed operations from `failed_queue` and retries them. For `embedding` and `memory_insert` operations, applies exponential backoff (increments `retry_count`, skips if retried within the last hour).

### Check 6: LLM Degradations
Queries `audit_logs` for entries with `service='llm', level='WARNING'` in the last hour. Counts degradation events (429s, timeouts, fallback activations). If count > 0, reports the count and summary in the janitor's Telegram notification.

### Notification Strategy
- Critical failures (heartbeat stale, zombie detected): Telegram alert
- Routine diagnostics (stuck dumps, null embeddings): Audit log only
- Supressed if the system is in a known degraded state (avoids alert fatigue)

## Orphan Cleanup Workflow (Weekly)

The orphan cleanup (`core/agents/cleanup_orphans.py`) runs every Sunday via `cleanup.yml`. It performs garbage collection on 3 categories of stale data:

### Stale Raw Dumps
Raw dumps in 'completed' or 'staged' status that are more than 30 days old are purged. This prevents unbounded growth of the `raw_dumps` table.

### Stale Processed Updates
Entries in `processed_updates` older than 72 hours are purged. The dedup window only needs 72 hours — beyond that, the same `update_id` will never be redelivered by Telegram.

### Stale Graph Edges
Graph edges with `weight` less than 0.1 that are more than 90 days old are removed. This prunes weak or outdated connections from the knowledge graph without affecting strong relationships.

### Safety Mechanisms
- Dry-run mode: all operations log what would be deleted without executing
- Batch limits: maximum 100 records deleted per run
- All deletions logged to `audit_logs`

## Duplicate Guard System

The duplicate guard (`core/lib/duplicate_guard.py`) prevents task duplication through three tiers:

### Normalization Pipeline

```python
1. normalize_title(): strip punctuation, lowercase
2. extract_core(): keep words >3 chars, remove stopwords and currency symbols
3. _extract_discriminators(): extract quarters (Q1-Q4), years, amounts
```

### Three Tiers

| Result | Threshold | Action |
|--------|-----------|--------|
| `block` | ≥80% core word overlap + ≥1 content word | Skip insertion. If superset, auto-merge with existing task title |
| `flag` | 50-80% overlap | Insert with `possible_duplicate=true` flag and `duplicate_of_title` reference |
| `clear` | <50% overlap | Insert normally |

### Smart Discriminator Extraction

Prevents false positives on similar-but-different tasks:

```python
# Task A: "Prepare Q3 pricing for Qhord"
# Task B: "Prepare Q4 pricing for Qhord"
# Discriminators: {Q3} vs {Q4} — NOT duplicates
if new_disc and ex_disc and new_disc != ex_disc:
    continue  # Not duplicates — different quarter/year/code
```

## Rate Limiting

A distributed sliding window rate limiter backed by Upstash Redis protects the Gemini API free tier across all concurrent serverless instances:

```python
class SlidingWindowLimiter:
    def __init__(self, max_calls: int, per_seconds: int = 60):
        self.max_calls = max_calls  # 12 calls
        self.per_seconds = per_seconds  # 60 seconds
        self.timestamps = []
```

Set to 12 RPM (leaving 3 RPM headroom from the 15 RPM free tier limit). Supports both sync and async contexts.

## Idempotency at Every Layer

| Layer | Mechanism |
|-------|-----------|
| Telegram webhook | `processed_updates` table (UNIQUE on update_id) |
| Raw dump processing | 60-second time window dedup (same content + source) |
| Task creation | `dedup_key` = MD5(title + project_id) |
| Pulse runs | `request_id` in metadata prevents duplicate processing |
| Email approvals | `danny_decision` check before processing |
| Raw dump request_id check | Index on `raw_dumps.metadata->>'request_id'` |
| Task completion | Status already done/cancelled check prevents duplicates |
| Graph edges | `upsert` with `on_conflict="label"` for task nodes |
| Google sync | Google event ID stored in DB, used for patch (not re-create) |

## Google Calendar Ghost-Event Auto-Heal

When `sync_to_calendar` attempts to patch a Google Calendar event whose `google_event_id` references an event that was externally deleted, the Google API returns a 404. The handler (June 2026):

1. Nulls `google_event_id` in the DB *before* re-provisioning — so if re-provision fails, the DB is clean rather than pointing to a ghost event.
2. Creates a fresh Google Calendar event and persists the new ID.

**Error discrimination is strict**: only 404 triggers the heal path. `429`, `403`, and `500` errors are re-raised immediately — they never null a valid stored ID on transient failures.

**File**: `core/services/google_service.py` — `sync_to_calendar()`

## Partial Batch Sync Visibility

When the completion handler closes multiple tasks in a single `execute_completion_closure` call and some Google sync steps fail, failures are **collected and surfaced to Telegram** rather than swallowed silently (June 2026):

- Failed task IDs and titles are accumulated during the batch.
- If any fail, the dump status is set to `partially_synced` and a Telegram message is sent listing the failed tasks (e.g. `"⚠️ Synced 2/3 tasks. Failed: Buy groceries (id=42)"`).
- `partially_synced` dumps are included in the Pulse's claim scope, allowing the pulse to retry the Google sync on the next scheduled run.

This is Option B (collect + notify) rather than transaction rollback — chosen because the Supabase Python client has limited transaction support and visibility beats atomicity for this use case.

**File**: `core/webhook/completion_handler.py` — `execute_completion_closure()`

## Distributed Trace IDs

Every HTTP request entering the system gets a unique `trace_id` via a `contextvars.ContextVar`:

| Entry Point | Where Set |
|-------------|-----------|
| `/api/webhook` | `api/index.py:webhook` — Telegram message |
| `/api/pulse` | `api/index.py:pulse` — Briefing trigger |
| `/api/whatsapp-ingest` | `api/index.py:whatsapp_ingest` — WhatsApp notification |

This trace ID is carried through all downstream `audit_log_sync` calls, enabling end-to-end request correlation across Supabase `audit_logs` entries. If a webhook message causes an LLM degradation or database error, the trace ID ties the symptom back to the originating request.

## Graceful Degradation Chain

When anything fails, the system degrades gracefully instead of crashing:

| Failure | Fallback |
|---------|----------|
| LLM call fails | Empty arrays for new_tasks/projects/people; raw text briefing |
| LLM JSON parse fails | Default `ai_data` with `{"new_tasks": [], "new_projects": [], "briefing": ""}` |
| Embedding fails | Zero vector `[0] * 768` + queued to failed_queue |
| Calendar sync fails | Logged as non-critical, task still created in DB |
| Google Tasks sync fails | Logged as non-critical, task synced on next run |
| Import fails (dispatch.py) | No-op inline function in Action Planner |
| Versioned update fails | Falls back to direct in-place update |
| DB query fails | Returns `[]` or `{}` defaults |
| Batch enrichment / cluster discovery / Google sync / heartbeat fails | Individual phase failure logged, remaining post-processing phases continue |

## LLM Control Layer & Prompt Mutation

When an LLM hallucinates markdown instead of JSON, or forgets a required key, the system does not crash or silently drop the task. It leverages a native **Control Layer** in `core/pulse/llm.py`:

1. **Intercept & Validate**: The output is run through Pydantic (e.g., `PulseOutput`).
2. **Prompt Mutation**: On failure, the system automatically rewrites the prompt for the next attempt, injecting the exact validation error to teach the LLM what it did wrong.
3. **Jittered Backoff**: The system gracefully sleeps using an async jittered exponential backoff before the next attempt, protecting API quotas.
4. **Fallback Cascading**: If all mutated retries fail on the primary model, it cleanly cascades to the next fallback model (Gemini -> Gemma -> OpenRouter).

## JSONB Data Handling Integrity

PostgreSQL `JSONB` and `TEXT` columns accessed via the Python Supabase client can sometimes return `NULL` as a Python `None`, or stringified JSON rather than parsed dictionaries, leading to subtle runtime crashes.

### Defensive Parsing Patterns

To prevent `NoneType` and `AttributeError` crashes, the system enforces the following patterns when interacting with `metadata` or `content` columns:

1. **Null Fallback for `get()`:**
   Instead of `meta = node.get('metadata', {})` (which evaluates to `None` if the column explicitly contains `NULL`), the system uses the OR-fallback pattern:
   ```python
   meta = node.get('metadata') or {}
   ```

2. **Stringified JSON Checks:**
   Supabase occasionally returns JSON payloads as strings (`'{"key": "value"}'`) rather than Python dicts. The system always type-checks and parses:
   ```python
   if isinstance(meta, str):
       try:
           meta = json.loads(meta)
       except Exception:
           meta = {}
   ```

3. **Safe String-to-Dict Deserialization:**
   When loading a JSON string from a column that might be null, the system protects `json.loads` from `None` values:
   ```python
   # Bad: json.loads(row.get('content', '[]')) -> throws TypeError if content is None
   # Good:
   existing = json.loads(row.get('content') or '[]')
   ```

## Memory Versioning Integrity

Two code paths (`dispatch.py:_enrich_memory_entities()` and `completion_handler.py`) were updating memories in place without archiving the previous state. Fixed by adding **`version_memory_for_update()`** in `core/services/db.py` — reads current memory, inserts archived copy with `is_current=false`, returns update dict with bumped `version` + `supersedes_id`. Both enrichment paths now call this before `.update()`.

This follows the same temporal lineage pattern as the task/canonical-pages triggers, but in application code per the architecture decision.

## Deletion / Index Cleanup Integrity

The undo delete path (`commands.py`) now runs **`cleanup_memory_retrieval_index()`** (`core/retrieval/cleanup.py`) before deleting — cascading removal of `retrieval_passages`, `retrieval_memory_bundle_links`, `retrieval_index_runs` for the given `memory_id`. A daily orphan sweep via `sweep_orphan_retrieval_entries()` catches any orphans from code paths that bypassed cleanup (runs once per 20h via Sentinel piggyback).

## Raw Dump Lifecycle Cleanup

Raw dumps could get stuck in `staged` or `pending` status due to exceptions in the processing pipeline. The **Sentinel** (every 5min) now piggybacks a cleanup step: marks `staged`/`pending` raw_dumps older than 24h as `abandoned`. This prevents permanent table debt accumulation without a dedicated janitor run.

A migration cleaned existing stale rows in production.
