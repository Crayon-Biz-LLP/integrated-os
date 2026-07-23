-- Backfill: set is_current=false on all merged graph_nodes.
--
-- Before the code fix in execute_graph_node_merge(), merged nodes only
-- got canonical_id set but kept is_current=true. This migration fixes
-- existing rows so all downstream queries (briefs, Live tab, graph)
-- correctly exclude merged entities via their existing is_current=true filter.
--
-- Run this in Supabase SQL Editor.

UPDATE graph_nodes
SET is_current = false
WHERE canonical_id IS NOT NULL
  AND is_current = true;

-- Verify
SELECT COUNT(*) AS remaining_merged_current
FROM graph_nodes
WHERE canonical_id IS NOT NULL
  AND is_current = true;
