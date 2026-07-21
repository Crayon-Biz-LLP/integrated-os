-- ============================================================
-- Migration 45: Add awaiting_clarification to pending_nodes status CHECK constraint
--
-- The clarifier (core/clarifier.py) sets status to 'awaiting_clarification'
-- when it detects potential duplicates or issues needing disambiguation.
-- This status was never added to the CHECK constraint, causing silent
-- DB constraint violations that broke the approve/reject flow.
-- ============================================================

-- Step 1: Drop the old CHECK constraint on pending_nodes.status
ALTER TABLE pending_nodes DROP CONSTRAINT IF EXISTS pending_nodes_status_check;

-- Step 2: Re-add it with awaiting_clarification included
ALTER TABLE pending_nodes ADD CONSTRAINT pending_nodes_status_check
    CHECK (status IN (
        'pending', 'approved', 'rejected',
        'awaiting_details', 'awaiting_clarification', 'flagged', 'merged'
    ));

-- Verify
SELECT
    'pending_nodes statuses: ' || string_agg(DISTINCT status, ', ') AS statuses
FROM pending_nodes;
