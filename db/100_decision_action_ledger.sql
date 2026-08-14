-- ============================================================
-- Migration 100: Decision action ledger (per-item undo, Layer 1+2)
--
-- Adds a metadata JSONB column to `decisions` so a manual approve/reject
-- can carry the ledger of what its plan actually executed:
--
--   {"actions": [
--       {"operation": "close_task",  "target_id": "3167", "title": "..."},
--       {"operation": "create_task", "target_id": "3321", "title": "..."}
--   ]}
--
-- `target_id` is the id needed to reverse each action (the created id for
-- creates, the target id for closures) — see executor.compensate_action.
-- The undo endpoint reads this ledger to reverse side effects; rows without
-- a ledger still get their decision reversed and the message re-pended.
-- ============================================================

ALTER TABLE decisions ADD COLUMN IF NOT EXISTS metadata JSONB;

-- Verify: should return one row (the new column)
SELECT column_name, data_type FROM information_schema.columns
WHERE table_name = 'decisions' AND column_name = 'metadata';
