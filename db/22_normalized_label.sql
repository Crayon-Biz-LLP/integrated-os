-- db/22_normalized_label.sql
-- Replace the functional unique_label_lower index with a real normalized_label column
-- that PostgREST's on_conflict can target. Preserves case-insensitive dedup.

-- Step 1: Add nullable column
ALTER TABLE graph_nodes ADD COLUMN normalized_label TEXT;

-- Step 2: Backfill existing rows
UPDATE graph_nodes SET normalized_label = LOWER(TRIM(label)) WHERE normalized_label IS NULL;

-- Step 3: Create unique index (run CONCURRENTLY outside this file in production)
CREATE UNIQUE INDEX unique_graph_nodes_normalized_label ON graph_nodes (normalized_label);

-- Step 4: Enforce NOT NULL after backfill
ALTER TABLE graph_nodes ALTER COLUMN normalized_label SET NOT NULL;

-- Step 5: Drop the old functional index (superseded by normalized_label)
DROP INDEX IF EXISTS unique_label_lower;
