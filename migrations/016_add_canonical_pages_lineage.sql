-- Migration 016: Add Temporal Lineage to canonical_pages
-- Purpose: Enable version history for Master Pages
-- Run this in Supabase Dashboard > SQL Editor

-- Add is_current
ALTER TABLE canonical_pages
ADD COLUMN IF NOT EXISTS is_current BOOLEAN DEFAULT TRUE;

-- Add version tracking
ALTER TABLE canonical_pages
ADD COLUMN IF NOT EXISTS version INTEGER DEFAULT 1;

-- supersedes_id points to the previous version (creates version chain)
ALTER TABLE canonical_pages
ADD COLUMN IF NOT EXISTS supersedes_id BIGINT;

-- Index for current-version queries
CREATE INDEX IF NOT EXISTS idx_canonical_pages_is_current
ON canonical_pages(is_current) WHERE is_current = TRUE;

-- Index for version chain lookups
CREATE INDEX IF NOT EXISTS idx_canonical_pages_supersedes
ON canonical_pages(supersedes_id) WHERE supersedes_id IS NOT NULL;

-- Comments
COMMENT ON COLUMN canonical_pages.is_current IS 'TRUE for current version, FALSE for historical';
COMMENT ON COLUMN canonical_pages.version IS 'Version number (increments on each update)';
COMMENT ON COLUMN canonical_pages.supersedes_id IS 'Points to previous version (creates version chain)';
