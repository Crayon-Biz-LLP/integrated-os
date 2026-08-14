-- ============================================================
-- Migration 98: Retire the clarifier question flow (plans/73)
--
-- The graph clarification loop (core/clarifier.py) is retired. Pending
-- nodes/edges no longer get flipped to 'awaiting_clarification' and asked
-- about via a Telegram-only surface; they stay in the Quick Confirmation
-- queue as ordinary HITL cards.
--
-- This migration:
--   1. Reverts any legacy 'awaiting_clarification' rows on pending_nodes
--      and pending_graph_edges back to 'pending' so they reappear in the
--      queue instead of lingering in limbo.
--   2. Resolves any open clarification_feedback rows (preserves history;
--      answering them is no longer possible since the surface is gone).
--   3. Drops 'awaiting_clarification' from the pending_nodes status CHECK
--      constraint (added by migration 45) — the status is no longer
--      reachable from the formal state machine.
--
-- pending_graph_edges has no status CHECK constraint (its formal statuses
-- pending/approved/rejected/expired are governed by the application state
-- machine in core/lib/state_machines.py), so no constraint change there.
-- ============================================================

-- 1. Revert legacy pending_nodes awaiting_clarification → pending
UPDATE pending_nodes
SET status = 'pending'
WHERE status = 'awaiting_clarification';

-- 2. Revert legacy pending_graph_edges awaiting_clarification → pending
UPDATE pending_graph_edges
SET status = 'pending'
WHERE status = 'awaiting_clarification';

-- 3. Resolve open clarification_feedback rows (surface retired)
UPDATE clarification_feedback
SET resolved_at = now()
WHERE resolved_at IS NULL;

-- 4. Drop 'awaiting_clarification' from the pending_nodes status CHECK
ALTER TABLE pending_nodes DROP CONSTRAINT IF EXISTS pending_nodes_status_check;

ALTER TABLE pending_nodes ADD CONSTRAINT pending_nodes_status_check
    CHECK (status IN (
        'pending', 'approved', 'rejected',
        'awaiting_details',
        'flagged', 'merged', 'merge_proposed'
    ));

-- Verify
SELECT 'pending_nodes awaiting_clarification remaining: ' || count(*) AS leftover
FROM pending_nodes WHERE status = 'awaiting_clarification';
SELECT 'pending_graph_edges awaiting_clarification remaining: ' || count(*) AS leftover
FROM pending_graph_edges WHERE status = 'awaiting_clarification';
SELECT 'open clarification_feedback remaining: ' || count(*) AS leftover
FROM clarification_feedback WHERE resolved_at IS NULL;
