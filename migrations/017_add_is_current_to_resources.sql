-- Migration: Add is_current column to resources table
-- Purpose: Enable versioned record pattern for resources (matching tasks, memories, projects, etc.)
-- Run this in Supabase Dashboard > SQL Editor

-- Add is_current column (matches existing pattern in tasks/memories/projects)
ALTER TABLE resources 
ADD COLUMN IF NOT EXISTS is_current BOOLEAN DEFAULT TRUE;

-- Add version column for versioned updates
ALTER TABLE resources 
ADD COLUMN IF NOT EXISTS version INTEGER DEFAULT 1;

-- Add supersedes_id for tracking version lineage
ALTER TABLE resources 
ADD COLUMN IF NOT EXISTS supersedes_id INTEGER;

-- Index for active resources query
CREATE INDEX IF NOT EXISTS idx_resources_is_current 
ON resources(is_current) WHERE is_current = TRUE;

-- Comments
COMMENT ON COLUMN resources.is_current IS 'Flag indicating if this is the current version (for immutable versioning)';
COMMENT ON COLUMN resources.version IS 'Version number for tracking updates';
COMMENT ON COLUMN resources.supersedes_id IS 'ID of the previous version this record supersedes';
