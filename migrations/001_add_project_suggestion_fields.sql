-- Migration: Add project suggestion fields to email_pending_tasks
-- Date: 2026-04-30
-- Purpose: Allow non-authoritative project suggestions for email pending tasks

ALTER TABLE email_pending_tasks 
ADD COLUMN IF NOT EXISTS suggested_project_id BIGINT REFERENCES projects(id),
ADD COLUMN IF NOT EXISTS project_confidence FLOAT CHECK (project_confidence >=0 AND project_confidence <=1),
ADD COLUMN IF NOT EXISTS project_mapping_reason TEXT;

-- Add comment to clarify these are suggestions, not authoritative
COMMENT ON COLUMN email_pending_tasks.suggested_project IS 'AI-suggested project name (non-authoritative)';
COMMENT ON COLUMN email_pending_tasks.suggested_project_id IS 'AI-suggested project ID (non-authoritative, from shared helper)';
COMMENT ON COLUMN email_pending_tasks.project_confidence IS 'Confidence score 0-1 for the project suggestion';
COMMENT ON COLUMN email_pending_tasks.project_mapping_reason IS 'Reason for the project mapping (for audit/debugging)';
