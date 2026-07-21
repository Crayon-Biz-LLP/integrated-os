-- ============================================================
-- Migration 45: Add awaiting_clarification and merge_proposed
--                  to pending_nodes status CHECK constraint
--
-- Two statuses were missing from the original CHECK constraint:
--   - awaiting_clarification: set by clarifier (core/clarifier.py) for
--     duplicate/disambiguation detection
--   - merge_proposed: set by process_graph_pending_decision (graph.py)
--     when find_similar_node returns a match during approval
--
-- Both were silently rejected by the DB, blocking approve/reject and
-- merge proposal workflows.
-- ============================================================

-- Step 1: Drop the old CHECK constraint on pending_nodes.status
ALTER TABLE pending_nodes DROP CONSTRAINT IF EXISTS pending_nodes_status_check;

-- Step 2: Re-add it with both missing statuses included
ALTER TABLE pending_nodes ADD CONSTRAINT pending_nodes_status_check
    CHECK (status IN (
        'pending', 'approved', 'rejected',
        'awaiting_details', 'awaiting_clarification',
        'flagged', 'merged', 'merge_proposed'
    ));

-- Verify
SELECT
    'pending_nodes statuses: ' || string_agg(DISTINCT status, ', ') AS statuses
FROM pending_nodes;
