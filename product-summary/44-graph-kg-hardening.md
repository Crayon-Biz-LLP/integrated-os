# Graph KG Hardening & Concept Fluidity

**Date**: Jun 14-16, 2026 | **Phase**: Pre-Phase 1, T-401 | **Commits**: ~28

## What

Four-layer architecture upgrade to the knowledge graph to eliminate junk nodes, enforce schema integrity, and add human-in-the-loop for all edges. Plus the LLM Layer Consolidation (T-402) that eliminated all duplicated infrastructure code.

## KG Hardening — 4 Layers

**Layer 1 — Schema + Guardrails**: Purged legacy node types (emotional_state, resource, task, practice, cluster). Added temporal tracking (`valid_from`, `valid_until`) and epistemic status (`epistemic_status`) to all edges. Replaced BANNED_RELATIONSHIPS with `VALID_EDGE_MATRIX` — a positive allowlist of exactly 16 valid edge types. No catch-all relationships allowed.

**Layer 2 — Context Salience**: Deployed `get_context_for()` — a bidirectional recursive CTE in Postgres that fetches neighborhood context for any entity. Built `assemble_context()` Python token-budgeted packer with `compute_css()` math model: $ln(1+count) \times e^{-\lambda t} \times W_{dist} \times W_{epi}$.

**Layer 3 — Active Reasoning**: Wired email triage and Morning Pulse to use `assemble_context()` instead of flat task dumps. Context is now entity-grounded and temporally weighted.

**Layer 4 — Clarifier Phase 2**: Similarity dedup checks with 85%+ auto-merge detection. If an extracted entity matches an existing pending node by ≥85%, the system proposes a 1-click merge before approval.

## Concept Fluidity (Synaptic Plasticity)

Added `concept` node type with `EVOKES`, `RELATES_TO`, `ASSOCIATED_WITH` edge types to the ontology. Built `concept_sweep_batch.py` that extracted abstract concepts from all 416 historical memories. All concept nodes flowed through HITL (pending table → `g{id}` approval). **Removed Jul 9, 2026** (Phase 20) — 997 concept nodes + 678 EVOKES edges purged from DB. Emotions moved to memory metadata.

## Entities Tab UI

- `graph_type_overrides` table for type corrections (e.g., changing a person node to organization)
- Rename, manual merge, and cascade delete capabilities in frontend
- Approve/reject actions on entity-table-list
- Type filtering dropdown for live/pending entities
- Auto-create pending nodes for edge-only labels

## Key Files

| File | Purpose |
|------|---------|
| `core/lib/graph_rules.py` | VALID_EDGE_MATRIX, integrity rules |
| `core/pulse/context.py` | assemble_context(), get_context_for() |
| `core/pulse/clarifier.py` | Clarifier Phase 2 — dedup + merge |
| `frontend/src/app/dashboard/graph/pending/` | Entities tab UI |
| `core/skills/backfill_graph.py` | Extraction pipeline hardening |

## Related Docs

- [LLM Layer Consolidation](44b-llm-layer-consolidation.md) (same period)
- [Graph Redesign & Dedup](45-graph-redesign-dedup.md) (Jun 25-26)
- [Context Registry & Truth Boundary](30-context-registry-truth-boundary.md)
