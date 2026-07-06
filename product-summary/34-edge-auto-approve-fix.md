# Edge Auto-Approve Subsystem Mismatch Fix + Decision Backfill

## Overview

Two related changes to Rhodey's pattern learning and auto-decision pipeline:

1. **Edge auto-approve subsystem mismatch fix** — The Decision Pulse auto-approve code queried `compute_pattern_confidence(features, "graph_edges")`, but every edge observation (both normal Telegram approvals and batch backfill) was recorded under `"entity_extraction"`. Zero patterns existed under `"graph_edges"` — edges never auto-approved, regardless of confidence.
2. **Historical decision + observation backfill** — 1,107 approved edges and 136 batch-created nodes had never been recorded in `decisions` or `subsystem_telemetry`, so Rhodey's pattern learner had no data from those approvals.

## Fix Details

### 1. Subsystem Mismatch Fix (`core/pulse/engine.py`)

Three changes:

| Line | Before | After |
|------|--------|-------|
| 399 | `compute_pattern_confidence(features, "graph_edges")` | `compute_pattern_confidence(features, "entity_extraction")` |
| 388 | SELECT `id, source_label, target_label, relationship` | + `source_type, target_type` |
| 398 | `features = {"relationship": row["relationship"]}` | + `"source_type", "target_type"` |

Also fixed `_score_row()` (Telegram display tags) to use the correct subsystem and include granular features. Without source_type/target_type, EVOKES would hit the coarse pattern (1,195 total, 187 corrections, 69% effective confidence) instead of the granular pattern (person→concept: 220/220, 85% effective).

### 2. Decision & Observation Backfill

- `scripts/backfill_edge_decisions.py` — 1,107 approved edges → 1,107 decisions + 1,107 observations + pattern count updates
- `scripts/backfill_node_decisions.py` — 136 batch-created nodes (source_text='batch') → 136 decisions + 136 observations + pattern count updates

Also fixed: `decisions` table was missing `GRANT INSERT, SELECT, UPDATE ON decisions TO service_role` — the service role key couldn't write decisions at all (pre-existing).

### 3. Pattern Counts After Backfill

| Pattern | Observations | Correct | Confidence | Effective* | Auto-approves? |
|---|---|---|---|---|---|
| EVOKES person→concept | 220 | 220 | 100% | 0.85 | Yes |
| ASSOCIATED_WITH person→concept | 180 | 180 | 100% | 0.85 | Yes |
| KNOWS person→person | 72 | 72 | 100% | 0.85 | Yes |
| DISCUSSED_WITH person→person | 34 | 34 | 100% | 0.85 | Yes |
| WORKS_AT person→organization | 33 | 33 | 100% | 0.85 | Yes |
| FAMILY_OF person→person | 20 | 20 | 100% | 0.85 | Yes |
| ASSOCIATED_WITH project→concept | 15 | 15 | 100% | 0.85 | Yes |
| WORKS_FOR person→organization | 13 | 13 | 100% | 0.85 | Yes |
| MET_WITH person→person | 12 | 12 | 100% | 0.85 | Yes |
| FRIEND_OF person→person | 10 | 10 | 100% | 0.85 | Yes |
| INTERESTED_IN | 14/18 | 14 | 78% | 0.63 | No (suggest) |
| Node: person (batch) | 113 | 113 | 100% | 0.85 | Yes |
| Node: organization (batch) | 22 | 22 | 100% | 0.85 | Yes |
| Node: concept (pending) | 10 | 10 | 100% | 0.85 | Yes |
| Node: person (pending) | 34 | 34 | 100% | 0.85 | Yes |

\* *Effective = confidence minus compression penalty (0.15). Auto-approve threshold is 0.70.*

## Key Decision

- **Backfill with `auto_decided=True`**: Historical decisions were recorded as auto-decided (not manually reviewed), which is truthful — the batch script approved them programmatically. Pattern learner treats confirmed+auto_decided the same as confirmed+manual for confidence counting.

## Key Files
- `core/pulse/engine.py` — Subsystem mismatch fix (3 changes)
- `scripts/backfill_edge_decisions.py` — Edge decision + observation backfill (NEW)
- `scripts/backfill_node_decisions.py` — Node decision + observation backfill (NEW)

## Impact
- 10 edge patterns + 4 node patterns now at 100% confidence → Rhodey auto-approves silently
- Only borderline patterns (INTERESTED_IN with 78%→63% effective) still ask for review
- 1,243 historical decisions recorded for audit trail + undo support
- `decisions` table now writable by service_role (was missing GRANT)
