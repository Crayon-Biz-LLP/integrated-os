-- Migration: Add is_archived column to memories and canonical_pages
-- Purpose: Soft-delete/archival for memory lifecycle management
-- Run this in Supabase Dashboard > SQL Editor

-- Add is_archived to memories
ALTER TABLE memories 
ADD COLUMN IF NOT EXISTS is_archived BOOLEAN DEFAULT FALSE;

-- Add is_archived to canonical_pages
ALTER TABLE canonical_pages 
ADD COLUMN IF NOT EXISTS is_archived BOOLEAN DEFAULT FALSE;

-- Add archived_at timestamp
ALTER TABLE memories 
ADD COLUMN IF NOT EXISTS archived_at TIMESTAMPTZ;

ALTER TABLE canonical_pages 
ADD COLUMN IF NOT EXISTS archived_at TIMESTAMPTZ;

-- Add archive_reason
ALTER TABLE memories 
ADD COLUMN IF NOT EXISTS archive_reason TEXT;

ALTER TABLE canonical_pages 
ADD COLUMN IF NOT EXISTS archive_reason TEXT;

-- Index for archived records
CREATE INDEX IF NOT EXISTS idx_memories_is_archived 
ON memories(is_archived) WHERE is_archived = TRUE;

CREATE INDEX IF NOT EXISTS idx_canonical_pages_is_archived 
ON canonical_pages(is_archived) WHERE is_archived = TRUE;

-- Comments
COMMENT ON COLUMN memories.is_archived IS 'Soft-delete flag for archived memories';
COMMENT ON COLUMN memories.archived_at IS 'When memory was archived';
COMMENT ON COLUMN memories.archive_reason IS 'Why memory was archived (pruned, compacted, superseded)';
COMMENT ON COLUMN canonical_pages.is_archived IS 'Soft-delete flag for archived pages';
