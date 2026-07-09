# 36. normalized_label Column Fix

## Problem

Migration `db/21_case_insensitive_label.sql` dropped `UNIQUE(label)` on `graph_nodes` and replaced it with a functional unique index on `LOWER(TRIM(label))`. PostgREST's `on_conflict="label"` requires a constraint on the bare column — functional indexes cannot be used as conflict targets. Every graph_node upsert returned HTTP 400, silently breaking all 19 write sites.

## Fix

Added `normalized_label TEXT UNIQUE` column computed as `LOWER(TRIM(label))`. This gives PostgREST a real column with a real constraint that `ON CONFLICT` can target, while preserving the case-insensitive dedup intent.

### Migration (`db/22_normalized_label.sql`)

1. `ADD COLUMN normalized_label TEXT` (nullable)
2. Backfill: `UPDATE SET normalized_label = LOWER(TRIM(label))`
3. `CREATE UNIQUE INDEX unique_graph_nodes_normalized_label`
4. `ALTER COLUMN normalized_label SET NOT NULL`
5. `DROP INDEX unique_label_lower` (redundant functional index)

### Code Changes

- **Shared helper**: `normalize_label()` in `core/lib/graph_rules.py` (already existed) — `label.strip().lower()`
- **10 upsert sites**: Added `normalized_label` to data dict, changed `on_conflict="label"` → `on_conflict="normalized_label"`
- **9 insert sites**: Added `normalized_label` to data dict
- **12 files** touched across `core/pulse/`, `core/skills/`, `core/lib/`, `core/webhook/`, `scripts/`, `tests/`

### CI Guard

`scripts/check_graph_nodes_normalized_label.py` — greps all prod `.py` for `graph_nodes` insert/upsert missing `normalized_label`. Exits code 1 on violation.
