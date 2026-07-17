# 58. Final Architecture Overhaul (Jul 14-17, 2026)

## Root Cause

The codebase had accumulated 3 concurrent processing architectures that called each other, shared state via raw_dumps with 10+ status values, and had no single owner. 47 out of 200 commits (~24%) were quick fixes layered on pressure-built architecture. Every layer had its own dedup, its own error handling, and its own silent failure paths.

The core problem was **architectural fractalization**: each new feature added a new processing path rather than routing through existing ones.

## What Changed

### 1. Unified Action Planner (Parts 51-52)
Replaced the 3-headed architecture (Legacy Dispatch + Quick Process cron + Pulse Engine staging sorter) with a single typed Action pipeline:

- **`core/actions/planner.py`**: Single LLM call resolves user intent into typed `Action` objects (create_task, close_task, reschedule, cancel_recurring, etc.)
- **`core/actions/executor.py`**: Executes actions through `create_task_direct()`, `create_note_direct()`, `update_task_status()` — direct DB operations, no legacy piping
- **`core/pulse/tools.py`**: `create_task_direct()` and `create_note_direct()` — deterministic entity resolution BEFORE creation, enrichment queue after
- **All 6 former `process_single_dump` callers eliminated**

**Files deleted**: `core/agents/quick_process.py` (545 lines), `core/lib/process_input.py`, `core/prompts/ingest.py`, `.github/workflows/quick_process.yml`

### 2. Pulse Engine Redesign (Parts 52-57)
What was a 1500-line `engine.py` with agent loop + staging sorter + 5 Pydantic models is now 6 focused modules:

- `briefing.py` — Single LLM call, write-behind pattern
- `decision_pulse.py` — AI-free pending approvals
- `models.py` — Clean data contracts (dead fields removed)
- `pipeline.py` — Consolidated health monitor
- `run_logger.py` — Pulse run tracking
- `sentinel.py` — Meeting alarms + 7 piggybacks (isolated)

Staging Area Sorter removed. PulseOutput dead fields (completed_task_ids, new_tasks, etc.) removed — LLM no longer instructed to generate ignored operations.

### 3. Intelligence Layer Consolidation (Part 57-58)

| Issue | Fix |
|---|---|
| `context_salience.py` duplicate context engine | Removed — context_registry is the single source |
| `brain_synth_v2.py` 5 duplicate RPCs | Consolidated — uses associative retrieval |
| Two entity extraction algorithms per query | Single `entity_resolver.py` pass |
| `adaptive_briefing_learner()` template noise | Cleaned |
| 61% passage→phrase_node link gap | Batch refactor of `build_triple_graph()` |

### 4. Enrichment Queue (Part 56)
Fire-and-forget enrichment (`asyncio.create_task` for graph edges) replaced with queue-based pattern:

- `pending_enrichment_jobs` table with `claim_pending_enrichment_job()` RPC
- Atomic claim + 3-retry dead-letter lifecycle
- Survives Vercel cold kills
- Sentinal piggyback processes every ~5 min

### 5. Persistence & State Hardening (Parts 53-57)

| What | Before | After |
|---|---|---|
| Clarification state | In-memory dict (lost on cold start) | DB-backed `pending_graph_clarifications` table |
| State machines | Ad-hoc status values per table | Formal `state_machines.py` — 16 tables, documented transitions |
| pending_graph_nodes | Single table (mixed creation + merge) | `pending_nodes` + `merge_proposals` |
| Ingestion pipeline | Per-channel duplicate classify/persist | Unified `ingest()` contract + `url_filter.py` |
| Webhook timeout | `asyncio.wait_for(55)` hack | `pending_webhook_jobs` queue |
| Health monitors | 4 redundant monitors (pipeline_service, maintenance, janitor) | 1 consolidated `run_full_health_check()` |
| Dead files | process_input.py, pipeline_service.py, maintenance.py, janitor_check.py | All deleted |

### 6. DB Schema & CI Fixes (Parts 54-57)

- `close_task_edges()` trigger: added `is_current = true` guard (crashed on tasks with archived graph node versions)
- Graph node duplicate cleanup: 26 orphan nodes deleted, 432 linked via `db_record_id`
- `pending_enrichment_jobs.related_org_id` column added (task→org BELONGS_TO edges now work)
- WhatsApp JSON parse hardening at `whatsapp_ingest.py:78`
- Push notification `device_tokens` table created (was missing, Flutter client already registered)
- `dedup_key` hash ordering bug fixed (normalized before check, not after)
- `now_ist()` helper created, 11 ad-hoc timezone constructions migrated

### 7. 4W1H Root Cause Enforcement (Part 55)

Every commit now requires a `Root Cause:` line, enforced by `.githooks/commit-msg` hook. AGENTS.md Step 10 mandates 4W1H documentation (Why/What/Where/When/How) before committing fixes.

### 8. UAT Validation (Part 58)

22 automated scenarios across all 4 primary layers, validated against LIVE_DB:

| Tier | Scenarios | What's Tested |
|---|---|---|
| Ingestion | S1-S4 | Task creation with org, entity resolution, notes, URL quarantine |
| Processing | S5-S11, S18-S21 | Task closure, dedup, batch workflow, enrichment queue, graph enrichment E2E, reschedule, recurring lifecycle, Google sync (mocked) |
| Intelligence | S11-S13, S18 | Enrichment queue, memory retrieval, DLQ recovery, graph edges/nodes |
| Presentation | S14-S17, S22 | Pulse briefing, Decision Pulse, Sentinel, Health check, Briefing quality |

**All 22 passing**. Test data self-cleaning via `[UAT]` prefix + `cleanup_uat_rows()`.

## Architecture (Final)

```
┌──────────────────────────────────────────────────────────────┐
│                    INGESTION LAYER                           │
│  Telegram │ WhatsApp │ Email │ Outlook │ Teams │ Calls       │
│  → classify() → url_filter() → plan_actions()               │
│  Unified ingest() contract for all channels                 │
├──────────────────────────────────────────────────────────────┤
│                    PROCESSING LAYER                          │
│  Action Planner → Executor → create_*_direct / update_*     │
│  Entity linker (resolve BEFORE creation)                    │
│  Enrichment queue (pending_enrichment_jobs)                 │
│  DLQ consumer │ State machine guards │ Compensate on fail   │
├──────────────────────────────────────────────────────────────┤
│                    INTELLIGENCE LAYER                        │
│  Associative retrieval (7 signals + PPR)                   │
│  Knowledge graph (HITL for all edges)                       │
│  Context registry (6 strategies, entity-grounded)           │
│  Brain synthesis / Pattern detection / Memory clustering    │
├──────────────────────────────────────────────────────────────┤
│                    PRESENTATION LAYER                        │
│  Pulse Engine (single LLM call, write-behind)              │
│  Decision Pulse (AI-free, pending approvals)                │
│  Sentinel (meeting alarms + piggybacks)                     │
│  Health monitor (consolidated)                              │
├──────────────────────────────────────────────────────────────┤
│                    PERSISTENCE / INTEGRATION                 │
│  16 state machines │ DB-backed state │ Temporal lineage     │
│  Google Calendar/Tasks sync │ Telegram │ FCM Push          │
└──────────────────────────────────────────────────────────────┘
```

## Key Files

| File | Purpose |
|---|---|
| `core/actions/planner.py` | Universal Action Planner — single LLM resolution |
| `core/actions/executor.py` | Typed action execution with validation |
| `core/pulse/tools.py` | create_task_direct, create_note_direct, entity resolver |
| `core/lib/enrichment_queue.py` | Queue-based enrichment (Vercel-safe) |
| `core/lib/state_machines.py` | Formal state machines (16 tables) |
| `core/lib/ingest.py` | Unified ingestion pipeline |
| `core/lib/url_filter.py` | URL quarantine single source of truth |
| `core/lib/node_tables.py` | pending_nodes / merge_proposals abstraction |
| `core/lib/clarification_state.py` | DB-backed clarification state |
| `core/pulse/briefing.py` | Refactored Pulse Engine (single LLM call) |
| `core/pulse/pipeline.py` | Consolidated health check |
| `tests/uat/run_uat.py` | 22-scenario E2E UAT suite |
| `scripts/run_health.py` | CLI health check entry point |
