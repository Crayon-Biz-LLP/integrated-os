# 21. Frontend Dashboard

## Technology Stack

| Layer | Technology |
|-------|-----------|
| Framework | Next.js 16 App Router |
| UI Library | React 19, shadcn/ui, Radix UI primitives |
| Styling | Tailwind v4 with OKLCH color tokens |
| Icons | Lucide React (single library, no mixing) |
| Data Viz | D3.js (force-directed graph) |
| Auth | Supabase SSR (server + client) |
| Data Fetching | SWR with 30-second dedup interval |
| Toasts | Sonner (background feedback only) |
| Theme | next-themes (light + dark parity) |

## 11 Dashboard Modules

### Dashboard Home
**File**: `frontend/src/app/dashboard/page.tsx` + `dashboard-shell.tsx`

The main landing page shows:
- **StatsCards**: 4-card grid (Open Tasks, Due Today, Overdue, Pending Emails) with color-coded indicators
- **WhatToDoNow**: Unified view of overdue tasks, due-today tasks, pending email decisions, and calendar events — with one-click "Done" and Yes/No buttons, relative date formatting
- **QuickChat**: Chat UI with 30-second auto-refresh, auto-scroll, direct integration with `/api/send-message`
- **PulseBriefings**: Last 3 AI briefings with smart metadata parsing and "Read more" for long content
- **RecentTasks**: SWR auto-refreshing (30s), sorted by due date, limited to top 5, red left border for overdue

### Tasks Module
Filterable table with priority badges, project context, estimated minutes. Detail sheet with project change dialog. One-click status toggle. Project change dropdown.

### Projects Module
Grid cards with organization_name badges, status, keyword context. Detail sheet with full project info. Stats: active project count, organization_name distribution.

### Emails Module
Inbox table with sender, subject, classification badges (actionable/FYI/ignored). Draft list with status tracking. Pending tasks with approve/reject buttons. Email detail sheet. Comprehensive filters.

### Calendar Module
Four views: month, week, day, agenda. Unified Google Calendar + Outlook Calendar events. Calendar stats (event counts, upcoming blocks).

### People Module
Grid cards with strategic weight, role, source tracking. Person detail sheet. Filters for role, source, strategic weight.

### Resources Module
Library grid with title, URL, category, cluster assignment. Cluster group view. View toggle (grid/list). Stats by category/cluster.

### Memories Module — Knowledge Graph Visualization

Two pages serve memory exploration:

**Memories List** (`/dashboard/memories`): Standard table/listing of all memories with search/filter.

**Brain Graph** (`/dashboard/memories/graph`): Split-pane, Danny-centered interactive brain view.

#### Split-Pane Architecture

```
┌──────────────────────────────────────────────────┐
│  Toolbar: Danny button | node/edge count | Dev   │
│  zoom controls | sidebar toggle | Memories link  │
├────────────────┬─────────────────────────────────┤
│                │                                 │
│  Episode       │  NeuralDisc (PixiJS v8 WebGL)   │
│  Stream        │                                 │
│                │  - Danny-centered ego graph     │
│  (collapsible) │  - 2-hop neighborhood           │
│                │  - Hover highlights connections  │
│  w-80 (320px)  │  - Zoom: mouse wheel → cursor   │
│  or hidden     │  - Pan: background drag          │
│                │  - Click node → load neighbor     │
│                │  - Click bg → return to Danny     │
│                │  - Zoom controls (+/-/Fit)        │
│                │  - Breathing glow + edge particles│
│                │  - Reduced motion support         │
└────────────────┴─────────────────────────────────┘
```

**Left Pane — Episode Stream** (`EpisodeStream.tsx`):
Groups graph-linked memories into episodes rather than displaying raw chronological fragments. Clustering uses 3 signals:
1. **Shared non-root entity** (memories about the same person/project, not Danny himself)
2. **Same source/thread** (same metadata source within 1h)
3. **Same memory_type** (within 30min)

Each episode card shows: title, human-readable summary, entity badges (color-coded by type), memory count, relative timestamp. Click to expand reveals raw memories beneath. Collapsible via toolbar button.

**Right Pane — NeuralDisc** (`NeuralDisc.tsx`):
PixiJS v8 interactive force-directed graph rendered via WebGL:
- Danny is the permanent root anchor — graph loads centered on him on page boot
- Nodes colored by type: person (blue), organization (teal), project (purple), cluster (pink), task (amber), concept (grey), emotional_state (red)
- D3.js `forceSimulation` runs 300 ticks to compute layout, positions rendered as PixiJS Graphics circles
- Hover: highlights connected nodes/edges, dims non-connected, shows labels
- Labels shown for center, directly hovered, and active-connected nodes
- Breathing glow animation on center node, particle traversal on connected edges
- Background click returns to Danny-centered view
- Node click loads 1-hop neighborhood and filters episode stream
- Zoom/pan: mouse wheel zoom toward cursor, background drag to pan, +/-/Fit buttons
- WebGL context loss detection and recovery via component key remount
- GPU crash button in dev mode for testing context loss

**Performance & Stability**:
- All callback props (onNodeClick, onBackgroundClick, onDiagnostics, onContextRestored) stored in refs to prevent identity changes from triggering PIXI scene rebuilds
- Render effect dependency array: `[layoutData, hoveredNodeId, contextLost, enableEffects, prefersReducedMotion]` — removed `onNodeClick`, `onBackgroundClick`, `onDiagnostics`, `nodes`, `centerNodeId` from deps
- Layout computed once per data change (D3.js tick), then positions reused across hover passes
- Debug counters: render count, scene build count, layout count, diagnostics call count — logged every 5s in dev
- AbortController + sequence guard pattern for all fetch operations to prevent stale responses

**Backend API**:
| Endpoint | Purpose |
|----------|---------|
| `GET /api/graph/ego?depth=2&cap=80` | Danny-centered ego graph (resolve root via core_config, parallel batched node queries) |
| `GET /api/graph/neighborhood?node_id=X` | 1-hop ego network from any node |
| `GET /api/graph/resolve-memory?memory_id=X` | Resolve memory → primary entity via highest-weight MENTIONS edge |
| `GET /api/episodes/stream?node_id=X&limit=40` | Clustered memory episodes, optionally filtered by entity |

### Health Module
Pipeline health status, failed queue items with retry counts, memory embedding stats, error logs from audit_logs.

### Messages Module
Message history with direction (incoming/outgoing), status, source filters, content display. Supports filtering by message_type.

### Decisions Module
**Files**: `frontend/src/app/dashboard/decisions/` (shell + page), `frontend/src/components/decisions/graph-pending-list.tsx`

Approval hub for pending graph items:
- **Graph Edges tab** — Lists pending edges from backfill extraction with inline editing (Approve/Edit/Reject). Badge count shows total pending. Edit mode lets user change source_label, target_label, and relationship type before approving. Backend POST to `/api/graph-edge-action`.
- **Graph Nodes tab** (planned) — Will show pending person/organization/project nodes for approval.

## Design System (DESIGN.md)

A 607-line design specification governs all UI decisions:

### Core Principles
- "An operating system for capture, memory, task execution, email triage, briefings, reflections, and operator clarity"
- Optimize for truth, trust, speed, and calm focus
- Premium enough for daily use, credible enough to demo — without becoming a decorative startup dashboard

### Key Rules
- **OKLCH color space**: Muted teal brand accent, dark/light mode parity
- **No hardcoded colors**: Always use Tailwind token classes
- **Card-premium utility**: Consistent surfaces with shadow + hover lift
- **One scroll region per page**: No nested scrolling
- **Empty states required**: Every module must handle the empty case — never blank white space
- **Status vocabulary**: Consistent badge variants across all 7 modules
- **Anti-patterns list**: 14 banned patterns (hardcoded colors, gradient buttons, toast-for-errors, nested sheets, raw Supabase IDs in UI, etc.)

### Stats Components

8 domain-specific stats components:

| Module | What It Tracks |
|--------|---------------|
| Tasks | Open / Due Today / Overdue / Completed counts |
| Emails | Volume by classification, pending tasks, pending drafts |
| Health | Pipeline health, failed queue count, memory stats |
| People | Total count, strategic weight distribution |
| Projects | Active count, organization_name distribution |
| Resources | Count by category, cluster-linked vs. unlinked |
| Calendar | Event counts, upcoming blocks |
| Dashboard | Aggregate snapshot across all domains |
