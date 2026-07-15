-- Migration 40: Cleanup duplicate graph nodes
-- Fixes close_task_edges() trigger crash and removes duplicate nodes accumulated
-- before the unique_graph_nodes_normalized_label_type index existed

-- Step 1: Fix close_task_edges() trigger — add is_current=true guard
-- Without this, closing a task with multiple archived graph nodes crashes:
-- "more than one row returned by a subquery"
CREATE OR REPLACE FUNCTION close_task_edges()
RETURNS TRIGGER AS $$
BEGIN
  IF NEW.status IN ('done', 'cancelled') 
     AND OLD.status NOT IN ('done', 'cancelled') THEN
    UPDATE graph_edges
    SET valid_until = now()
    WHERE (source_node_id = (
             SELECT id FROM graph_nodes 
             WHERE db_record_id = NEW.id::text AND type = 'task' AND is_current = true
           )
        OR target_node_id = (
             SELECT id FROM graph_nodes 
             WHERE db_record_id = NEW.id::text AND type = 'task' AND is_current = true
           ))
    AND relationship IN ('BLOCKS', 'DEPENDS_ON')
    AND valid_until IS NULL;
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- Step 2: Link orphan current task nodes that have metadata->>task_id
-- These were created by write_graph_edges_for_task() without setting db_record_id
UPDATE graph_nodes
SET db_record_id = metadata->>'task_id'
WHERE type = 'task'
  AND db_record_id IS NULL
  AND is_current = true
  AND metadata->>'task_id' IS NOT NULL;

-- Step 3: Link orphan current memory nodes that have metadata->>memory_id
UPDATE graph_nodes
SET db_record_id = metadata->>'memory_id'
WHERE type = 'memory'
  AND db_record_id IS NULL
  AND is_current = true
  AND metadata->>'memory_id' IS NOT NULL;

-- Step 4: Delete archived duplicate task nodes (is_current=false, 0 edges)
DELETE FROM graph_nodes
WHERE type = 'task'
  AND is_current = false;

-- Step 5: Delete archived duplicate memory nodes (is_current=false, 0 edges)
DELETE FROM graph_nodes
WHERE type = 'memory'
  AND is_current = false;

-- Step 6: Delete edgeless orphan task nodes from backfill_graph
-- These have no task_id link and no edges pointing to them
DELETE FROM graph_nodes
WHERE type = 'task'
  AND db_record_id IS NULL
  AND is_current = true
  AND metadata->>'source' = 'backfill_graph'
  AND NOT EXISTS (
    SELECT 1 FROM graph_edges e
    WHERE e.source_node_id = graph_nodes.id OR e.target_node_id = graph_nodes.id
  );
