-- Migration 51: Rename project_creation_signals → org_creation_signals
-- Phase 2: Projects → Organizations cleanup
-- Drops the old project_creation_signals table and creates org_creation_signals
-- with org_name column instead of project_name

BEGIN;

-- Rename the table
ALTER TABLE IF EXISTS project_creation_signals RENAME TO org_creation_signals;

-- Rename the column
ALTER TABLE IF EXISTS org_creation_signals RENAME COLUMN project_name TO org_name;

COMMIT;
