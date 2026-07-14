# Graph Redesign & Dedup (Phase 23)

**Date**: Jun 25-26, 2026 | **Commits**: ~12 | **Phase**: 23

## What

Complete overhaul of the graph visualization and data quality. Three coordinated panes replaced the legacy single-pane graph. A 4-layer dedup algorithm cleaned up duplicate and stale graph nodes. Clarification loops unified across Telegram and frontend.

## 3-Pane Graph Intelligence Surface

**Left Pane — Structural Graph Context**: Relation labels and edge hierarchy. Shows the edge types connecting visible nodes with aggregated counts per relationship.

**Center Pane — Focus Modes**: Ranked labels with memory panel. Click a node to see its neighborhood and associated memories.

**Right Pane — Responsive**: Collapsible/resizable — toggle between 320px and full-width graph view.

## 2.5D Spherical NeuralDisc

Upgraded the flat 2D NeuralDisc to a Fibonacci sphere layout:
- **True 3D rendering**: Wireframe sphere with depth cues (node opacity falls off by z-depth)
- **Orbiting labels**: Entity names float on the sphere surface, rotating with camera
- **Node click → rich pane**: Clicking a node opens context in the right pane
- **PixiJS v8 WebGL**: GPU-accelerated force simulation with link force, repulsion, and surface constraint

## 4-Layer Graph Dedup

Two-track duplicate cleanup (`scripts/backfill_graph_dedup.py`):

| Layer | Method | Result |
|-------|--------|--------|
| 1 — Exact label | Case-insensitive exact match | Direct duplicate elimination |
| 2 — Normalized ILIKE | `lower(trim())` comparison | Broader match surface |
| 3 — Fuzzy trigram | `pg_trgm` similarity ≥ threshold | Approximate string matching |
| 4 — Manual review | Queue for human decision | Edge cases and ambiguous merges |

Executed actual node merges with edge consolidation — not just soft deletes.

## Clarification Loop Unification

- Unified feedback loops across both Telegram and Decisions UI
- Added missing API proxy route for frontend
- Fixed recurring task bug, memory titles, graph loading with Redis
- Cleaned stale files and applied task dedup migration

## Key Files

| File | Purpose |
|------|---------|
| `frontend/src/app/dashboard/graph/` | Three-pane layout, NeuralDisc 2.5D |
| `scripts/backfill_graph_dedup.py` | 4-layer dedup + merge execution |
| `frontend/src/app/dashboard/graph/neural-disc/` | WebGL 3D force simulation |
| `api/index.py` | Graph proxy routes |

## Related Docs

- [Graph KG Hardening](44-graph-kg-hardening.md)
- [Frontend Dashboard](21-frontend-dashboard.md)
