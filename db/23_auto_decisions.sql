-- Phase 16: Track auto-decisions + user verification
-- Adds columns to the decisions table so auto-approve/reject actions
-- are recorded distinctly and can be verified or rejected in the UI.

ALTER TABLE decisions ADD COLUMN IF NOT EXISTS auto_decided BOOLEAN DEFAULT false;
ALTER TABLE decisions ADD COLUMN IF NOT EXISTS verified_at TIMESTAMPTZ;

COMMENT ON COLUMN decisions.auto_decided IS 'True if Rhodey made this decision autonomously (not user-initiated)';
COMMENT ON COLUMN decisions.verified_at IS 'When the user verified this auto-decision was correct. NULL = unverified.';
