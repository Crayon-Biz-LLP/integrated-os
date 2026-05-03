-- Migration: Add embedding_status to memories table
-- Phase 1 hardening: Track embedding generation status

ALTER TABLE memories ADD COLUMN IF NOT EXISTS embedding_status TEXT DEFAULT 'success';

-- Add check constraint for valid values
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'memories_embedding_status_check'
    ) THEN
        ALTER TABLE memories ADD CONSTRAINT memories_embedding_status_check 
        CHECK (embedding_status IN ('pending', 'success', 'failed'));
    END IF;
END
$$;

-- Index for efficient querying of failed/pending embeddings
CREATE INDEX IF NOT EXISTS idx_memories_embedding_status 
ON memories(embedding_status) 
WHERE embedding_status != 'success';

-- Update existing rows: if embedding is not null, status = 'success', else 'pending'
UPDATE memories 
SET embedding_status = CASE 
    WHEN embedding IS NOT NULL THEN 'success'
    ELSE 'pending'
END
WHERE embedding_status IS NULL;

-- Add comment for documentation
COMMENT ON COLUMN memories.embedding_status IS 'Tracks embedding generation: pending, success, failed. Used for health monitoring.';
