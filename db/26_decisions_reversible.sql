-- Phase 18: Decision reversibility guard
-- Tracks whether an auto-decision can be safely undone.
-- Irreversible decisions (like project archival, bulk deletes) prevent
-- the reject/undo endpoint from attempting to reverse the DB action.

ALTER TABLE decisions ADD COLUMN IF NOT EXISTS reversible BOOLEAN DEFAULT true;

COMMENT ON COLUMN decisions.reversible IS 'False if the action cannot be safely undone (e.g., archival, deletion, irreversible state change).';
