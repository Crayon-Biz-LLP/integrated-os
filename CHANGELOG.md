# Changelog & Deprecation Log

Tracks significant changes, removals, and deprecations across the project. 
Updated whenever something is removed or fundamentally changes.

---

## 2026-07-28 — Learning Loop Fixes: Classifier Ingestion, Verification, Null Guard

### Fixed: Classifier corrections pipeline (broken architectural assumption)

**Root cause:** `ingest_feedback_overrides()` was reading from `audit_logs` (querying for `FEEDBACK_OVERRIDE` events), but all 53 user corrections landed in `subsystem_telemetry` via `emit_observation(outcome='corrected')`. The table stayed empty forever.

**Fix:** Rewrote `ingest_feedback_overrides()` to read from `subsystem_telemetry WHERE outcome='corrected'`. Added `_parse_json_field()` (handles Supabase's JSON encoding) and `_extract_pattern()` (builds text patterns from feature keywords).

**File:** `core/webhook/feedback_loop.py`

### Added: Verification feedback loop

**What:** Auto-decisions can now be confirmed via a "✅ Looks good — confirm N auto-decisions" button. Sets `verified_at` and calls `emit_observation(outcome='confirmed')` to reinforce pattern confidence through the existing telemetry pipeline.

**Files:** `core/pulse/decision_pulse.py` (button), `core/webhook/handler.py` (callback handler)

### Fixed: Null guard in compute_pattern_confidence

**Root cause:** `maybe_single_safe()` can return `None` during transient Supabase connection blips. The fallback loop in `compute_pattern_confidence()` called `row.data` without null-checking `row`, causing `'NoneType' object has no attribute 'data'`. Most visible for `classification` subsystem (0 patterns → full 9-iteration fallback chain runs every call).

**Fix:** Added `if row is None: continue` before accessing `row.data`.

**File:** `core/lib/telemetry.py`

### Changed: Auto-approve thresholds lowered

**Why:** Pattern data shows 100% accuracy on 911 entity_extraction patterns. Lowering the bar lets Rhodey act on what it already knows.

| Constant | Old | New |
|---|---|---|
| `CONFIDENCE_AUTO_APPLY` | 0.70 | **0.50** |
| `MIN_AUTO_APPROVE_OBSERVATIONS` | 5 | **3** |

**File:** `core/lib/telemetry.py`

---

## 2026-07-27 — Documentation Restructure

### Removed: 13 deprecated product-summary files

**Files deleted:** `06-telegram-intake.md`, `09-task-creation-paths.md`, `12-pulse-engine-overview.md`, `14-pulse-engine-agents-prompt.md`, `22-resilience-self-healing.md`, `24-use-cases.md`, `27-personal-capture-pipeline.md`, `32-resource-list-dismiss.md`, `40-process-input-refactoring.md`, `50-multi-intent-task-closure.md`, `52-unified-action-planner-holistic.md`, `56-enrichment-queue.md`, `57-architecture-cleanup-and-hardening.md`

**Why:** All had explicit LEGACY warnings pointing to replacement docs (51-action-planner-architecture.md, 58-final-architecture-overhaul.md, etc.). They described the old three-headed architecture (Webhook + Quick Process + Pulse sorter) which was replaced by the unified Action Planner (Parts 51-58).

**Replacement docs:** `51-action-planner-architecture.md`, `58-final-architecture-overhaul.md`, `99-architecture-reference.md`

### Moved: 22 session notes → session-notes/

**Why:** Product Summary should describe what the system IS, not what was DONE in each session. Session notes moved to `session-notes/`.

### Moved: 4 plans → plans/

**Why:** Plans and migration roadmaps don't belong in product documentation. Moved to `plans/`.

### Updated: Vercel→Modal in 6 product files

**Files:** `01-executive-summary.md`, `03-architecture-overview.md`, `04-backend-frontend.md`, `23-governance-security.md`, `25-whatsapp-ingest.md`, `99-lovable-product-brief.md`

**Why:** Backend migrated from Vercel to Modal on Jul 26, 2026. Files still said "runs on Vercel." Historical design-context phrases ("survives Vercel cold kills") preserved — they describe architecture principles, not current hosting.

**Replacement doc:** `14-infrastructure.md`

### Updated: PROJECT_UPDATE removed from speckit + product docs

**Files:** `03-architecture-overview.md`, `04b-intelligence-tiers.md`, `99-architecture-reference.md`, `.speckit/speckit.plan.md`

**Why:** PROJECT_UPDATE intent was removed from the codebase on Jul 24 (commit 2fd8a60) — it was redundant with NOTE + Action Planner. Rich context now flows through NOTE. 

### Trimmed: AGENTS.md session summaries (1800→110 lines)

**Why:** Session summaries were taking over the file. Moved to `session-notes/` for granular browsing. AGENTS.md now has a brief pointer instead.

---

## 2026-07-26 — Backend Platform Migration

### Deprecated: Vercel backend deployment

**Old:** Backend ran on Vercel serverless functions (60s timeout, `asyncio.create_task` killed on return)
**New:** Backend runs on Modal (300s timeout, `min_containers=1` for zero cold starts)
**Why:** Vercel's 60s timeout killed enrichment tasks. Modal provides 5x longer timeout and persistent containers.
**Replacement:** `infra/modal_app.py`, `product-summary/14-infrastructure.md`
**Commit:** `dfe3d8b`

---

## 2026-07-24 — PROJECT_UPDATE Intent Removed

### Removed: PROJECT_UPDATE intent

**Why:** PROJECT_UPDATE was introduced (Jul 20) to prevent rich status updates ("FC Madras compliance is completed") from being misclassified as COMPLETION and trapped in the "Which task?" disambiguation loop. However, it was redundant — NOTE + the Action Planner handled the same case better. The classifier was hardened (Part 50) with `secondary_actions` support, making PROJECT_UPDATE unnecessary.

**Replacement:** NOTE intent + Action Planner + `secondary_actions` array in classify.py
**Commit:** `2fd8a60`

---

## 2026-07-15 — Action Planner (Architecture Overhaul)

### Removed: process_single_dump pipeline (3 callers)

**Old files deleted:** `quick_process.py`, `process_input.py`, `ingest.py` (old version), `completion_handler.py` (old version)

**Why:** The old three-headed architecture (Webhook + Quick Process cron + Pulse Engine staging sorter) was replaced by a unified Action Pipeline (`plan_actions()` → `execute_planned_actions()`). All 6 former callers now route through the planner. ~700 lines of dead code eliminated.

**Replacement:** `core/actions/planner.py`, `core/actions/executor.py`
**Commits:** `a8eebc4`, `19412c9`, Parts 51-52

### Removed: ToolRegistry class

**Files:** `core/pulse/tools.py`, `core/pulse/llm.py`

**Why:** Remnants of the old agent-loop architecture. Pulse Engine now uses a single LLM call. ~180 lines deleted.

**Commit:** Part 61

---

## 2026-07-09 — Concept Node System Purged

### Removed: concept node type (knowledge graph)

**Why:** The `concept` node type (introduced Jun 15 with Synaptic Plasticity) was auto-creating too much noise. 997 concept nodes + 678 pending EVOKES edges purged from DB. Emotions now live on memory metadata instead of graph. Auto-approve system (`auto_approve.py`) deleted.

**Replacement:** Memory metadata (`sentiment_score`, `sentiment`) replaces concept nodes
**Commit:** Part 20

---

## 2026-07-06 — Health Monitor Consolidation

### Removed: 3 dead files + 2 GitHub Actions workflows

**Files deleted:** `core/agents/janitor_check.py`, `scripts/run_maintenance.py`, `core/pulse/maintenance.py`
**Workflows deleted:** `.github/workflows/janitor.yml`, `.github/workflows/maintenance.yml`

**Why:** Health monitoring was spread across 4 places. Consolidated into `core/pulse/pipeline.py` with a single `run_full_health_check()` and `scripts/run_health.py` CLI. Single `health.yml` workflow replaces 4.

**Replacement:** `scripts/run_health.py`, `.github/workflows/health.yml`
**Commit:** Part 57

---

## 2026-06-15 → 2026-07-09 — Semantic Changes

### Changed: CHURCH → ASHRAYA routing tag

**Why:** The routing tag was renamed from CHURCH to ASHRAYA to reflect the actual entity name. Changed across all Python and frontend code.

### Changed: Vercel `routes` → `rewrites`

**Why:** `routes` broke frontend deployment by routing all requests to api/index.py across both Vercel projects. Changed to `rewrites` which are scoped per project.

### Changed: Sequential DB → parallel `asyncio.gather`

**Why:** Briefing and query pipelines had 18 sequential DB queries. Refactored into 2-phase parallel fetch, reducing latency by ~40%.

---

## How to Update This Log

When removing or deprecating a feature:

1. Add an entry under the current date
2. Include: **What** was removed, **Why** it was removed, **Replacement** (if any), **Commit** or reference
3. Group related changes under the same date header
