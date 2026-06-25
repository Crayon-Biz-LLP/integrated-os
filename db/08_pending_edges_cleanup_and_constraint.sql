-- 1. Archive duplicate pending edges, keeping the newest one
WITH dupes AS (
  SELECT 
    id,
    ROW_NUMBER() OVER(
      PARTITION BY source_node_id, relationship, target_node_id 
      ORDER BY created_at DESC
    ) as rk
  FROM pending_graph_edges
  WHERE source_node_id IS NOT NULL 
    AND target_node_id IS NOT NULL
)
UPDATE pending_graph_edges
SET status = 'archived'
WHERE id IN (
  SELECT id FROM dupes WHERE rk > 1
) AND status != 'archived';

-- 2. Add partial UNIQUE constraint
CREATE UNIQUE INDEX IF NOT EXISTS idx_unique_pending_edge 
ON pending_graph_edges (source_node_id, relationship, target_node_id) 
WHERE status = 'pending';
