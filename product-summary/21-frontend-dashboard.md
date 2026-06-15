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
Grid cards with org_tag badges, status, keyword context. Detail sheet with full project info. Stats: active project count, org_tag distribution.

### Emails Module
Inbox table with sender, subject, classification badges (actionable/FYI/ignored). Draft list with status tracking. Pending tasks with approve/reject buttons. Email detail sheet. Comprehensive filters.

### Calendar Module
Four views: month, week, day, agenda. Unified Google Calendar + Outlook Calendar events. Calendar stats (event counts, upcoming blocks).

### People Module
Grid cards with strategic weight, role, source tracking. Person detail sheet. Filters for role, source, strategic weight.

### Resources Module
Library grid with title, URL, category, cluster assignment. Cluster group view. View toggle (grid/list). Stats by category/cluster.

### Memories Module — Knowledge Graph Visualization

The standout feature. Three components:

**EgoGraph** (`EgoGraph.tsx`): D3.js force-directed graph for a single page's ego network. 7 node types with distinct colors. Labels on edges showing relationship type. 300 simulation ticks.

**FullGraph** (`FullGraph.tsx`): Full interactive D3.js force-directed graph with:
- Zoom (0.2x-4x scale)
- Drag nodes with force reheat
- Hover effects (node enlargement, edge highlight)
- Click to open NodeFlyout detail panel
- Dark background (#09090b)
- 250-tick simulation with auto-stop

**NodeFlyout** (`NodeFlyout.tsx`): Slide-in panel showing node details with:
- Color-coded type indicator
- Linked canonical pages
- All connections listed with relationship type and direction arrows

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
| Projects | Active count, org_tag distribution |
| Resources | Count by category, cluster-linked vs. unlinked |
| Calendar | Event counts, upcoming blocks |
| Dashboard | Aggregate snapshot across all domains |
