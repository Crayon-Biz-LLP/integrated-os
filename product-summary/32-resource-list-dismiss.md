# Resource Clusters — List View + Dismiss

## What

Two UI features on the Knowledge Base (`/dashboard/clusters`) page:

1. **List view toggle** — switch between the existing bento-grid and a flat table (Title, Hostname, Category, Cluster, Date, Actions)
2. **Resource dismiss** — mark a resource as "read / done / not interested" so it hides from the UI and prevents re-storage of the same URL in future

## Implementation

### List view

A `viewMode` state toggle (`'grid' | 'list'`) in `clusters-shell.tsx`. The header has a button group switching between `LayoutGrid` and `List` icons. The list view is a responsive HTML table with inline cluster assignment dropdown and dismiss button per row.

### Dismiss

**Frontend**: 
- `POST /api/resources/[id]/dismiss` sets `dismissed_at` on the resource
- Dismiss buttons in both: (a) list view row actions, (b) split-pane detail view
- All API queries filter with `.is('dismissed_at', null)` so dismissed resources are hidden entirely

**Backend**:
- `resources.dismissed_at TIMESTAMPTZ` column (migration `db/20_resources_dismissed.sql`)
- URL dedup in `dispatch.py`, `quick_process.py`, `engine.py` checks `dismissed_at` — if set, skips re-storage and replies "Already seen this link and dismissed it." in Telegram

### Files changed

| File | Change |
|------|--------|
| `db/20_resources_dismissed.sql` | Migration: `ALTER TABLE resources ADD COLUMN dismissed_at TIMESTAMPTZ` |
| `frontend/src/app/dashboard/clusters/clusters-shell.tsx` | List view component + dismiss in both views |
| `frontend/src/app/dashboard/clusters/page.tsx` | Pass `dismissed_at` through, filter `.is('dismissed_at', null)` |
| `frontend/src/app/api/resources/route.ts` | Filter `.is('dismissed_at', null)` |
| `frontend/src/app/api/resources/[id]/dismiss/route.ts` | NEW — PATCH handler to set `dismissed_at` |
| `frontend/src/lib/resources/api.ts` | Add `dismissResource()` |
| `frontend/src/lib/resources/types.ts` | Add `dismissed_at` to `Resource` |
| `core/webhook/dispatch.py` | Check `dismissed_at` in URL dedup, reply message |
| `core/agents/quick_process.py` | Check `dismissed_at` in URL dedup |
| `core/pulse/engine.py` | Check `dismissed_at` in URL dedup |

### Key Decisions

- **Hidden, not soft-deleted**: Dismissed resources stay in DB with `dismissed_at` timestamp. They're filtered out of the UI and API queries but function as a dedup key for re-insertion.
- **Web UI only**: Dismiss is available from the cluster page only, not from Telegram inline flows, per user preference.
- **Informative Telegram reply**: When a dismissed URL is re-submitted, Rhodey says "Already seen this link and dismissed it. Skipping." instead of silently ignoring.
