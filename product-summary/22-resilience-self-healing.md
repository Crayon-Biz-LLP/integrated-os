# 22. Resilience & Self-Healing Infrastructure

Integrated-OS is built with the expectation that everything will fail eventually. Every subsystem has fallbacks, retries, and recovery mechanisms.

## Temporal Lineage & Versioning

The most fundamental resilience pattern. Instead of updating records in-place, the system creates new versions and marks old ones as `is_current=False`.

### How It Works

For tasks, memories, projects, and canonical_pages:

```python
# 1. Fetch current record
current = supabase.table(table_name).select('*').eq('id', record_id).execute()

# 2. Build new record with incremented version
new_record = {**old_record_fields, **update_data, 
              'version': old_version + 1,
              'is_current': True,
              'supersedes_id': record_id}

# 3. Insert new, mark old as superseded
supabase.table(table_name).insert(new_record).execute()
supabase.table(table_name).update({"is_current": False}).eq('id', record_id).execute()
```

### Time-Travel Queries

The `get_memory_at_time()` RPC walks the `supersedes_id` chain to reconstruct what a record looked like at any point in the past:

```sql
-- Walk the supersedes chain to find the version active at query_time
WITH RECURSIVE version_chain AS (
    SELECT * FROM memories WHERE id = memory_id
    UNION ALL
    SELECT m.* FROM memories m
    JOIN version_chain vc ON m.id = vc.supersedes_id
    WHERE m.created_at <= query_time
)
SELECT * FROM version_chain ORDER BY created_at DESC LIMIT 1;
```

### Drift Detection

The `detect_drift()` RPC counts how many times a project has been updated in the last N hours — if it's 3+ times in 48 hours, it flags as a potential bottleneck.

### Fallback to Direct Update

If versioned_update fails (e.g., the `supersedes_id` column doesn't exist), the system gracefully falls back to a direct in-place update:

```python
except Exception as e:
    audit_log_sync("db", "WARNING", f"Versioned update failed, falling back: {e}")
    supabase.table(table_name).update(fallback_data).eq('id', record_id).execute()
```

## Dead Letter Queue

Failed operations don't crash the system — they go to the `failed_queue` table with automatic retry.

### Schema

| Column | Purpose |
|--------|---------|
| `source_table` | Which table had the error (memories, raw_dumps, graph_edges) |
| `source_id` | The record's ID |
| `operation` | What failed (embedding, graph_extract, memory_insert) |
| `error_message` | The error description (truncated to 500 chars) |
| `retry_count` | Incremented on each retry attempt |
| `last_retry_at` | When it was last retried |

### Retry with Exponential Backoff

Retries happen in two places:

1. **Pipeline service retry** (`pipeline_service.py:91-113`): Increments `retry_count`, updates `last_retry_at`, attempts re-processing
2. **Pipeline health retry** (`pipeline.py:106-189`): Handles `embedding` and `memory_insert` operations with exponential backoff

### Operations That Use the DLQ

- Failed embedding generation → queued as `operation='embedding'`
- Failed memory insert → queued as `operation='memory_insert'`

## Zombie Recovery

If a processing step marks a dump as 'processing' but crashes before completing, the system would have a zombie — a record stuck in 'processing' forever.

The zombie recovery function (`db.py:43-54`) runs at the start of every Pulse and every Quick Process cycle:

```python
ten_mins_ago = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
supabase.table('raw_dumps') \
    .update({"status": "pending"}) \
    .eq('status', 'processing') \
    .lt('created_at', ten_mins_ago) \
    .execute()
```

Any dump stuck in 'processing' for more than 10 minutes is automatically reset to 'pending' and re-processed.

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

The Janitor workflow (`core/agents/janitor_check.py`) checks 4 things every ~3 hours:

1. **Stuck raw_dumps**: Dumps in 'pending' or 'staged' for >2 hours
2. **Stuck processing**: Dumps in 'processing' for >10 minutes (sends Telegram alert)
3. **Null embeddings**: Memories from the last 7 days with null embeddings
4. **Heartbeat freshness**: If Pulse hasn't run in >24 hours, sends a warning

### Health Dashboard

The frontend Health module shows these diagnostics in real time, plus failed queue items and error logs from `audit_logs`.

## Janitor Workflow (4x Daily)

The Janitor (`core/agents/janitor_check.py`) is a scheduled health maintenance workflow that runs every ~3 hours via `janitor.yml`. It performs 5 checks:

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

A sliding window rate limiter protects the Gemini API free tier:

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
| Task completion | Status already done/cancelled check prevents duplicates |
| Graph edges | `upsert` with `on_conflict="label"` for task nodes |
| Google sync | Google event ID stored in DB, used for patch (not re-create) |

## Graceful Degradation Chain

When anything fails, the system degrades gracefully instead of crashing:

| Failure | Fallback |
|---------|----------|
| LLM call fails | Empty arrays for new_tasks/projects/people; raw text briefing |
| LLM JSON parse fails | Default `ai_data` with `{"new_tasks": [], "new_projects": [], "briefing": ""}` |
| Embedding fails | Zero vector `[0] * 768` + queued to failed_queue |
| Calendar sync fails | Logged as non-critical, task still created in DB |
| Google Tasks sync fails | Logged as non-critical, task synced on next run |
| Import fails (dispatch.py) | No-op inline function replaces quick_process |
| Versioned update fails | Falls back to direct in-place update |
| DB query fails | Returns `[]` or `{}` defaults |

## LLM Control Layer & Prompt Mutation

When an LLM hallucinates markdown instead of JSON, or forgets a required key, the system does not crash or silently drop the task. It leverages a native **Control Layer** in `core/pulse/llm.py`:

1. **Intercept & Validate**: The output is run through Pydantic (e.g., `PulseOutput`).
2. **Prompt Mutation**: On failure, the system automatically rewrites the prompt for the next attempt, injecting the exact validation error to teach the LLM what it did wrong.
3. **Jittered Backoff**: The system gracefully sleeps using an async jittered exponential backoff before the next attempt, protecting API quotas.
4. **Fallback Cascading**: If all mutated retries fail on the primary model, it cleanly cascades to the next fallback model (Gemini -> Gemma -> OpenRouter).
