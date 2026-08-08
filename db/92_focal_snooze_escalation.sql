-- 92_focal_snooze_escalation.sql
-- Adds a per-item snooze counter + feedback slot so the focal-card
-- "Not now" deferral escalates instead of flat 7-day snoozing:
--   1st tap  → 1 day (quiet)
--   2nd tap  → 3 days (quiet)
--   3rd tap  → 7 days behind a warning + feedback gate
--   4th+ tap → 7 days (cap, quiet — the warning fired once)
-- The counter resets ONLY when the item is completed (see _complete_task
-- in api/index.py); deferral expiry does NOT reset it, so repeated
-- deferrals of the same item keep escalating.

ALTER TABLE tasks ADD COLUMN IF NOT EXISTS snooze_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS snooze_feedback TEXT DEFAULT NULL;

ALTER TABLE pending_nodes ADD COLUMN IF NOT EXISTS snooze_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE pending_nodes ADD COLUMN IF NOT EXISTS snooze_feedback TEXT DEFAULT NULL;

ALTER TABLE pending_graph_edges ADD COLUMN IF NOT EXISTS snooze_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE pending_graph_edges ADD COLUMN IF NOT EXISTS snooze_feedback TEXT DEFAULT NULL;

ALTER TABLE merge_proposals ADD COLUMN IF NOT EXISTS snooze_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE merge_proposals ADD COLUMN IF NOT EXISTS snooze_feedback TEXT DEFAULT NULL;
