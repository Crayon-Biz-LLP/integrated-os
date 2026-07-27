# Part 69: Documentation Restructure — Product Summary, Session Notes, Plans, AGENTS.md

**Date:** Jul 27, 2026

## Summary

Complete restructuring of project documentation from a single flat directory (69 files all in `product-summary/`) into three cleanly separated folders with clear ownership. Plus accuracy fixes across all documents.

## Changes

### 1. Three-Folder Documentation Layout

| Folder | Purpose | Files |
|--------|---------|-------|
| `product-summary/` | What the system IS — capabilities, features, architecture | 35 files |
| `session-notes/` | What was DONE — chronological development history | 22 files + README |
| `plans/` | What we PLAN — future migration and test plans | 4 files + README |

**Files moved:**
- 22 session notes → `session-notes/` (25b, 33-pattern, 34-edge, 35-app-redesign, 36-graph, 37-batch, 41-diagnostic, 42-temporal, 43-apk, 44-kg, 44b-llm, 45-graph-dedup, 46-role, 47-classification, 49-rhodey, 53-stabilization, 54-hardening, 55-root-cause, 59-post-uat, 60-document, 61-optimization, 62-thread)
- 4 plans → `plans/` (33-meta-cognitive, 63-uat-plan, 67-modal-migration, 68-asyncpg-rpc)

### 2. 13 Deprecated Files Removed

Files with explicit LEGACY warnings pointing to newer replacement docs:
`06-telegram-intake.md`, `09-task-creation-paths.md`, `12-pulse-engine-overview.md`, `14-pulse-engine-agents-prompt.md`, `22-resilience-self-healing.md`, `24-use-cases.md`, `27-personal-capture-pipeline.md`, `32-resource-list-dismiss.md`, `40-process-input-refactoring.md`, `50-multi-intent-task-closure.md`, `52-unified-action-planner-holistic.md`, `56-enrichment-queue.md`, `57-architecture-cleanup-and-hardening.md`

### 3. New Documents Created

- `product-summary/14-infrastructure.md` — Modal deployment, cron jobs, env vars, architecture diagram
- `product-summary/15-recent-enhancements.md` — Pipeline bug fixes (reminder_at, google_task_id, missing awaits)
- `session-notes/README.md` — Index of all session notes
- `plans/README.md` — Index of all plans with status
- `CHANGELOG.md` — Deprecation log tracking all removals and why

### 4. Accuracy Fixes (8 files)

**Vercel→Modal replacements (6 files):**
- `01-executive-summary.md`: "Vercel free tier" → "Modal"
- `03-architecture-overview.md`: Hosting table, API Layer desc, flow diagram
- `04-backend-frontend.md`: Serverless deployment desc
- `23-governance-security.md`: Env vars storage → Modal secrets
- `25-whatsapp-ingest.md`: Endpoint references
- `99-lovable-product-brief.md`: 3 Vercel refs

**PROJECT_UPDATE removals (3 files):**
- `03-architecture-overview.md`: Removed from flow diagram
- `04b-intelligence-tiers.md`: Removed from intent list (with historical note)
- `99-architecture-reference.md`: Removed from classifier rules table
- `.speckit/speckit.plan.md`: Removed from architecture diagram

### 5. AGENTS.md Trimmed

**1800 lines → 110 lines.** The massive "Session Anchored Summary" section was replaced with a brief pointer to `session-notes/`. Root Cause procedure, Engineering Standards, and all core content preserved.

### 6. Speckit Files Updated

- `.speckit/speckit.specify.md`: Vercel→Modal, PROJECT_UPDATE removed
- `.speckit/speckit.plan.md`: PROJECT_UPDATE removed from architecture diagram

## Key Decisions

1. **Three-folder layout**: Product docs stay in product-summary/. Session notes go to session-notes/. Plans go to plans/. Clear separation of concerns.
2. **Historical context preserved**: Phrases like "survives Vercel cold kills" and "Vercel-safe" kept as-is — they describe architectural design principles, not current hosting.
3. **CHANGELOG.md at root**: Single deprecation log for all removals, with date + why + replacement + commit. Follows standard open-source convention.
4. **AGENTS.md kept lean**: Session summaries in AGENTS.md were a growing problem. Now they live in session-notes/ where they belong. AGENTS.md is the agent guide, not a dev journal.

## Commits

| Hash | Message |
|------|---------|
| `9b1ec41` | docs: restructure documentation — product-summary, session-notes, plans, AGENTS.md |
| `db8b70a` | docs: fix 9 outdated files — Vercel→Modal, remove PROJECT_UPDATE references |
| `36d7020` | docs: remove PROJECT_UPDATE from speckit.plan.md architecture diagram |
| `2424786` | docs: create CHANGELOG.md — deprecation log for feature removals |

## File Summary

```
product-summary/         35 files (was 69)
session-notes/           23 files (including README)
plans/                    5 files (including README)
CHANGELOG.md              1 file (new)
```
