-- db/08_tasks_dedup_unique.sql
-- Adds a partial UNIQUE index on tasks.dedup_key for running/active rows.
-- Matches the app-level dedup predicate in tools.py:150.
--
-- EXECUTION: Run the DROP and CREATE as separate standalone statements
-- outside any transaction block. Do not wrap them in BEGIN/COMMIT.
-- If rerunning, first inspect for an existing idx_tasks_dedup_unique
-- index, drop it concurrently if needed, then recreate it concurrently.
--
-- STEP ORDER:
--   1. DROP INDEX CONCURRENTLY IF EXISTS  — handles all prior states:
--      - index absent  → no-op (CONCURRENTLY + IF EXISTS)
--      - index valid   → drops it (brief window without guard;
--                         200-row table makes Step 2 sub-second;
--                         app-level SELECT check covers gap)
--      - index INVALID → drops it (common after a failed CONCURRENTLY)
--   2. CREATE UNIQUE INDEX CONCURRENTLY  — fresh creation
--
-- DO NOT combine these into a single DO block or BEGIN/COMMIT.
-- CONCURRENTLY requires its own transaction per PostgreSQL rules.
--
-- No existing data conflicts: all current duplicate dedup_key rows
-- differ on is_current (only one live row per key).

-- Step 1: Clean up any prior index (valid, invalid, or absent).
DROP INDEX CONCURRENTLY IF EXISTS idx_tasks_dedup_unique;

-- Step 2: Create the unique index on non-done, non-cancelled, live rows.
CREATE UNIQUE INDEX CONCURRENTLY idx_tasks_dedup_unique
  ON tasks(dedup_key)
  WHERE status NOT IN ('done', 'cancelled') AND is_current = true;
