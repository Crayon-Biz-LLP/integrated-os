-- 72_focal_snooze.sql
-- Adds snoozed_until to tasks + pending tables so focal-card deferrals
-- ("Not now") persist and hide the item from briefings, the focal queue,
-- and the pulse engine until the deferral expires. "Not now" must not
-- silently resurrect on the next load.

ALTER TABLE tasks ADD COLUMN IF NOT EXISTS snoozed_until TIMESTAMPTZ DEFAULT NULL;
ALTER TABLE pending_nodes ADD COLUMN IF NOT EXISTS snoozed_until TIMESTAMPTZ DEFAULT NULL;
ALTER TABLE pending_graph_edges ADD COLUMN IF NOT EXISTS snoozed_until TIMESTAMPTZ DEFAULT NULL;
ALTER TABLE merge_proposals ADD COLUMN IF NOT EXISTS snoozed_until TIMESTAMPTZ DEFAULT NULL;

-- Indexes for the common "active only" scans
CREATE INDEX IF NOT EXISTS idx_tasks_snoozed_until ON tasks (snoozed_until) WHERE snoozed_until IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_pending_nodes_snoozed ON pending_nodes (snoozed_until) WHERE snoozed_until IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_pending_edges_snoozed ON pending_graph_edges (snoozed_until) WHERE snoozed_until IS NOT NULL;
