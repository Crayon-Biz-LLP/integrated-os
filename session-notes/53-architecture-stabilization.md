# 53 - Architecture Stabilization: DB-Backed State & Formal State Machines

## Overview
Following the Action Planner unification (Phase 51-52), this phase formalizes the architecture with DB-backed state management, formal state machines, and unified contracts across all subsystems. The pattern is consistent: **move state from Python memory into the database** so it survives cold starts, is queryable, and has transactional guarantees.

## The Pattern
| What | Before (app-level, in-memory) | After (DB-level) |
|------|------|-----|
| Temporal versioning | `version_memory_for_update()` in Python | DB triggers (`trg_temporal_task_update`, etc.) |
| Clarification state | `pending_graph_clarifications` dict (lost on cold start) | `pending_graph_clarifications` table |
| Active sessions | `active_sessions` dict in `graph.py` | `pending_graph_clarifications` table with `pending_type='session'` |
| Status transitions | ad-hoc `if status == X` scattered everywhere | `state_machines.py` — single source of truth, guard function |
| Node creation/merge | `pending_graph_nodes` mixed concerns | `pending_nodes` + `merge_proposals` tables, explicit schema |
| Ingest contracts | per-channel duplicate classify/persist | single `ingest()` with DB-backed pipeline |
| URL quarantine | inline check in handler.py | `url_filter.py` — single source of truth |

## Components

### 1. DB-Backed Clarification State (`core/lib/clarification_state.py`)
Replaces two in-memory data structures that were wiped on every Vercel cold restart:
- `pending_graph_clarifications` dict in `handler.py` (step tracking for Telegram clarification dialogs)
- `active_sessions` dict in `graph.py` (NLP correction sessions)

Both now use the `pending_graph_clarifications` table (`db/37_graph_clarifications.sql`) with:
- `chat_id`, `pending_id`, `pending_type` ('node' | 'edge' | 'session')
- `step` tracking + `context_json` for arbitrary session state
- 5-minute `expires_at` for automatic cleanup
- `get_active_clarification()`, `set_clarification()`, `resolve_clarification()`

### 2. Formal State Machines (`core/lib/state_machines.py` — 468 lines)
Single source of truth for all valid status transitions across 16 tables. Each table has:
- A set of valid statuses
- A dict of allowed transitions (from_status → set(to_statuses))
- `guard_is_valid_transition(table, from_status, to_status)` → bool

Tables covered: raw_dumps, tasks, memories, messages, pending_nodes, merge_proposals, pending_graph_edges, graph_nodes, graph_edges, conversations, conversation_threads (workflows), decisions, email_drafts, pending_retrieval_index_jobs, pending_graph_clarifications, agent_queue, call_recordings, retrieval_index_runs.

### 3. pending_nodes / merge_proposals Split (`core/lib/node_tables.py`)
The old `pending_graph_nodes` table served two purposes — node creation approvals AND merge proposals. Split into:
- **`pending_nodes`**: Node creation requests (person, org, project, etc.) with status workflow: pending → approved/rejected/flagged/merged.
- **`merge_proposals`**: Merge target→source proposals with `confidence`, `auto_approved` tracking.

Backfill script (`scripts/archive/backfill_pending_graph_nodes.py`) migrated 381 rows. Old table dropped via `db/35_drop_pending_graph_nodes.sql`.

### 4. Unified Ingestion Pipeline (`core/lib/ingest.py`)
Single `ingest()` contract for all channels:
```python
result = await ingest(
    text="Message content",
    source="whatsapp",
    classification="actionable",  # or "fyi", "ignored"
    summary="Who sent it, what they want",
    suggested_title="Verb-first action" or None,
    suggested_project="SOLVSTRAT" or None,
    channel_specific_data={...}
)
```
The caller handles fetching + classifying; `ingest()` handles persisting. Eliminates per-channel duplicate classify/persist logic.

### 5. URL Quarantine Module (`core/lib/url_filter.py`)
Single `check_and_quarantine_url()` at every ingress point. Returns `URLQuarantineResult` with: `is_url`, `url`, `action` ('inserted' | 'dismissed' | 'skipped_dedup' | 'none'), `message`. Extracted from inline checks in `handler.py` that were being copied across channels.

### 6. DLQ Consumer (`core/skills/dlq_consumer.py`)
Phase C of the pipeline overhaul. `process_dlq()` sweeps `audit_logs` (service='dlq'), retries with exponential backoff (30s, 2min, 5min), escalates to Telegram alert on permanent failure.

### 7. Shared Email Classify Prompt (`core/prompts/email_classify.py`)
Single `build_email_classify_prompt()` for both Gmail (personal) and Outlook (work) classification. Prevents prompt drift — mailbox-specific context injected via parameters. Companion test: `tests/unit/test_email_classify_prompt.py` (112 lines, no DB/Gemini).

## Key Files
- `core/lib/state_machines.py` — Formal state machines (468 lines, 16 tables)
- `core/lib/node_tables.py` — pending_nodes / merge_proposals abstraction
- `core/lib/clarification_state.py` — DB-backed clarification state
- `core/lib/ingest.py` — Unified ingestion pipeline
- `core/lib/url_filter.py` — URL quarantine single source of truth
- `core/prompts/email_classify.py` — Shared email classify prompt
- `core/skills/dlq_consumer.py` — DLQ consumer
- `db/34_pending_nodes_merge_proposals_deleted_at.sql` — Schema migration
- `db/35_drop_pending_graph_nodes.sql` — Drop legacy table
- `db/37_graph_clarifications.sql` — Clarification state table
- `tests/sim/test_validation_refactor.py` — 19 validation tests
