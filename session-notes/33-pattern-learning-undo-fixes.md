# Auto-Decision Feedback Loop & Pattern Learning Fixes

## Overview

Four fixes applied to close the auto-decision feedback loop and harden the pattern learning subsystem:

1. **Telegram Undo Buttons** — Auto-processed items now have inline `↩️ Undo` buttons in the Decision Pulse message, so you can reverse auto-approvals directly from Telegram without using the Web UI.
2. **Configurable Cross-Subsystem Blend** — The 70/30 blend ratio in `compute_composite_confidence()` is now module-level constants instead of hardcoded floats.
3. **Entity Type-Weighted Overlap Bonus** — The entity overlap bonus in `deliberate()` now varies by entity type (person=0.15, org=0.10, project=0.08) instead of a coarse 0.10/0.05 split.
4. **Missing Import Fixes** — `maybe_single_safe` was missing from imports in `patterns.py` and `pattern_extractor.py`, causing 8 pre-existing test failures.

## Fix 1: Inline Undo Buttons (Telegram)

### Problem
Auto-approved items had no inline "Undo" in Telegram. When Decision Pulse auto-approves items and sends the digest line "Auto-processed: 3 items", there's no way to reverse them without going to the Web UI or manually running SQL. The 30-minute gap between Decision Pulse runs meant a wrong auto-approval could sit unnoticed.

### Solution
Three inline keyboard buttons are appended below the remaining-pending-items keyboard:
- `↩️ Undo N channel` — reverses channel item approvals
- `↩️ Undo N node` — reverses graph node approvals  
- `↩️ Undo N edge` — reverses graph edge approvals

When tapped, `process_callback_query` in `handler.py`:
1. Queries `decisions` table for `auto_decided=true`, `status=active`, `verified_at=null` within last 30 min, filtered by exact `decision_type` (`channel_approval`, `graph_node_approval`, `graph_edge_approval`)
2. Calls `reverse_decision()` on each matching decision
3. Reverts the DB action: channel messages → `danny_decision=null`, graph nodes/edges → `status=pending`
4. Sends Telegram confirmation: "↩️ Undone 3 auto-processed channel items"

### Key Design Decisions
- **Decisions table as source of truth**: The undo query looks up the decisions table rather than passing item IDs through callback data. This is more robust because it survives cold starts and doesn't require maintaining in-memory state.
- **Precise `decision_type` filter**: Each undo target queries its exact decision type (`channel_approval`, `graph_node_approval`, `graph_edge_approval`) to prevent cross-type interference.
- **30-minute window**: Only auto-decisions from the last 30 minutes are reversible via this mechanism. Older items must be undone via the Web UI's auto-decisions tab.
- **Cascade gap**: Undoing a graph node does NOT cascade to concept/edge auto-creations from `auto_approve.py` cascade. This is acceptable for v1 — the cascade decisions (`concept_auto_creation`, `edge_auto_creation`) would need separate undo handling.

### Files Changed
- `core/pulse/engine.py` — Added undo keyboard rows after the pending-items keyboard
- `core/webhook/handler.py` — Added `undo_auto_channels/graph/edge` callback handling

---

## Fix 2: Configurable Cross-Subsystem Blend

### Problem
The 70/30 blend ratio in `compute_composite_confidence()` was hardcoded:
```python
composite_conf = primary["confidence"] * 0.70 + best_cross * 0.30
```
This meant tuning the blend required editing production code.

### Solution
Extracted into module-level constants at the top of `decision_features.py`:
```python
CROSS_SUBSYSTEM_BLEND_PRIMARY = 0.70
CROSS_SUBSYSTEM_BLEND_CROSS = 0.30
CROSS_SIGNAL_MIN_CONFIDENCE = 0.3
CROSS_COMPOSITE_BOOST_DELTA = 0.10
```

The docstring now references these constants so they're discoverable.

### Files Changed
- `core/lib/decision_features.py` — Added 4 constants, updated docstring

---

## Fix 3: Entity Type-Weighted Overlap Bonus

### Problem
`deliberate()` in `planner_critic.py` used a coarse entity overlap bonus:
- `0.10` if label is TASK/PROJECT_UPDATE/approve/create
- `0.05` otherwise

This meant "Equisoft" in text got the same overlap bonus for an edge approval as for a classification — no entity type differentiation.

### Solution
Added `_resolve_entity_type()` helper and replaced hardcoded values with a type-aware bonus map:
- **person**: `0.15` — people mentioned in decision context are highly relevant
- **organization**: `0.10` — org context shapes routing
- **project**: `0.08` — project context is useful but secondary
- **default**: `0.05` — base for unknown entity types

The `_resolve_entity_type()` function uses a **single bulk DB query** (not up to 15 sequential queries) to check graph_nodes types.

### Files Changed
- `core/lib/planner_critic.py` — Added `_resolve_entity_type()`, updated entity overlap logic, removed unused imports

---

## Fix 4: Missing Import Fixes

### Problem
Two files used `maybe_single_safe()` but only imported `get_supabase`:
- `core/pulse/patterns.py` — line 86 uses `maybe_single_safe` in `detect_completion_patterns()`
- `core/lib/pattern_extractor.py` — line 52 uses `maybe_single_safe` in `detect_drift()`

This caused 8 unit test failures (NameError) that were pre-existing but blocked CI.

### Solution
Added `maybe_single_safe` to the import line in both files.

### Additional Cleanup
- Removed unused `_auto_shortcodes` variable in `engine.py`
- Removed unused `decision_type` variable in `handler.py` 
- Removed unused `typing.Any`/`typing.Callable` imports in `planner_critic.py`
- Fixed ambiguous variable name `l` → `lb` in `planner_critic.py`

### Test Results
- **75 passed, 2 failed** (the 2 failures are a pre-existing mock setup issue in `test_pattern_extractor.py`, not the import bug)
- The 8 pre-existing NameError failures are fully resolved

### Files Changed
- `core/pulse/patterns.py` — Added `maybe_single_safe` import
- `core/lib/pattern_extractor.py` — Added `maybe_single_safe` import
- `core/lib/planner_critic.py` — Cleanup lint issues
- `core/pulse/engine.py` — Removed dead code
- `core/webhook/handler.py` — Removed unused variable
