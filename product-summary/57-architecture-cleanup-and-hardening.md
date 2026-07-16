# Part 57: Architecture Cleanup & Hardening

## Problem
After the comprehensive architecture overhaul (Parts 51-56), the codebase had accumulated dead files, redundant health monitors, ad-hoc IST timezone constructions, and prompt drift risk. These were leftover debts from a codebase that had been rebuilt under pressure.

## What Was Done

### 3 Dead Files Deleted
| File | Reason |
|---|---|
| `core/lib/process_input.py` | Zero callers. Was part of old `process_single_dump` pipeline fully replaced by Action Planner. |
| `core/services/pipeline_service.py` | Zero callers. Its `check_pipeline_health()` was a duplicate of the real one in `pulse/pipeline.py`. |
| `core/pulse/maintenance.py` | Zero callers. All functions (`run_index_queue`, `run_memory_sweep`, etc.) had no callers. |

### 1 Shared Helper Added
**`core/lib/time_utils.py`** — Added `now_ist()` function and `IST_TIMEZONE` constant:
- `now_ist()` returns `datetime.now(IST)` where IST = UTC+05:30
- `IST_TIMEZONE` constant for callers that need the timezone object
- Both are reusable across the entire codebase

### 11 IST Timezone Constructions Migrated
Replaced ad-hoc `timezone(timedelta(hours=5, minutes=30))` with shared `IST_TIMEZONE` / `now_ist()` across 6 files:
- `classify.py` (1 replacement + import cleanup)
- `multimodal.py` (1 replacement + import cleanup)
- `practices.py` (1 replacement)
- `handler.py` (3 replacements)
- `dispatch.py` (6 replacements + variable shadowing fix)
- `commands.py` (2 replacements + variable shadowing fix)

### Health Monitor Consolidation (4→1)
| Before | After |
|---|---|
| 2 GHA workflows (janitor.yml + maintenance.yml) | 1 workflow (health.yml) ✅ |
| 2 health check files (pipeline.py + janitor_check.py) | 1 file (pipeline.py) ✅ |
| 2 CLI scripts (run_maintenance.py + janitor_check.py) | 1 script (run_health.py) ✅ |

**Created:**
- `core/pulse/pipeline.py` — Expanded with DLQ items, recent errors, LLM degradation checks. Original `check_pipeline_health()` preserved (backward compat). New `run_full_health_check()` returns dict for CLI.
- `scripts/run_health.py` — CLI entry point. Sends Telegram alert only if issues found. Business hours filter.
- `.github/workflows/health.yml` — Runs every 2 hours weekdays, twice on weekends.

**Deleted:**
- `core/agents/janitor_check.py` — merged into pipeline.py
- `scripts/run_maintenance.py` — was already broken (imported deleted module)
- `.github/workflows/janitor.yml` — replaced by health.yml
- `.github/workflows/maintenance.yml` — replaced by health.yml

### Orphaned Import Fix
`api/index.py` imported `process_maintenance` from the deleted `core.pulse.maintenance`. Relocated to `run_full_health_check` from `core.pulse.pipeline`. Route renamed from `/api/maintenance` to `/api/health`.

### Prompt Audit (Before vs After Action Planner)
Comprehensive comparison of every prompt file before and after the Action Planner refactoring. Confirmed **zero intelligence lost** — all rules, examples, and guardrails from the old `process_single_dump` / `Staging Area Sorter` / `ToolRegistry` prompts are present in the new architecture.

### Critical Fixes
- **`dedupe_pending.py`**: Removed aliased `.select('id, label, node_type as type')` which caused SQL syntax error (`node_typeastype`). Changed to `.select('id, label, node_type')` with `node['node_type']` access.
- **`tools.py`**: Addressed `memories.update()` bypass CI guard — now permitted for non-content fields (e.g., `expires_at`) since DB triggers handle temporal versioning.

## Key Files
- `core/lib/time_utils.py` — now_ist() helper + IST_TIMEZONE constant
- `core/pulse/pipeline.py` — Consolidated health check (expanded)
- `scripts/run_health.py` — CLI health check entry point
- `.github/workflows/health.yml` — Single health monitor workflow
- `api/index.py` — /api/health endpoint replaces /api/maintenance

## Net Effect
- **Deleted**: 7 files (4 dead modules + 3 health/graph GHA workflow files)
- **Created**: 3 files (run_health.py, health.yml + product-summary)
- **Modified**: 7 files (time_utils.py, 6 IST callers)
- **2 GHA workflows eliminated**: janitor.yml + maintenance.yml → health.yml
