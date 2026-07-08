-- Step 1: Fix SENTINEL rows — each needs a unique source_label so the full
-- pending_graph_edges unique index doesn't block multiple SENTINEL rows.
-- We use source_text (which is already unique per memory) as the discriminator.
UPDATE pending_graph_edges
SET source_label = '__SENTINEL__:' || COALESCE(source_text, 'unknown')
WHERE relationship = 'SENTINEL'
  AND (source_label IS NULL OR source_label = '' OR source_label LIKE '__SENTINEL__:%' = FALSE);

-- Step 2: Drop the old partial unique index on pending_graph_edges
-- (WHERE relationship != 'SENTINEL') — PostgREST couldn't use it with ON CONFLICT.
DROP INDEX IF EXISTS idx_pending_edges_unique_triple;

-- Step 3: Create a full (non-partial) unique index matching ON CONFLICT column order.
-- Column order: source_label, target_label, relationship (matches the code's on_conflict).
-- SENTINEL rows now have unique source_labels so they won't collide.
CREATE UNIQUE INDEX IF NOT EXISTS idx_pending_edges_triple
ON pending_graph_edges (source_label, target_label, relationship);

-- Step 4: Drop the expression unique index on graph_nodes (lower(trim(label))).
-- PostgreSQL can't match ON CONFLICT (label) against an expression index.
DROP INDEX IF EXISTS unique_label_lower;

-- Step 5: Add a bare-column unique constraint ON graph_nodes.label.
-- This is what graph_nodes.upsert(on_conflict="label") needs.
ALTER TABLE graph_nodes ADD CONSTRAINT graph_nodes_label_key UNIQUE (label);

-- Step 6: Add a non-unique index for fast case-insensitive lookups
-- (replaces the dropped expression index's query performance, without uniqueness).
CREATE INDEX IF NOT EXISTS idx_graph_nodes_label_lower ON graph_nodes (lower(label));
