-- Migration: Add pruning fields to memories table
-- Purpose: Enable importance-based decay and automated pruning
-- Run this in Supabase Dashboard > SQL Editor

-- Importance score (1-10 scale, 10 = most important)
ALTER TABLE memories 
ADD COLUMN IF NOT EXISTS importance_score INTEGER DEFAULT 5 CHECK (importance_score >= 1 AND importance_score <= 10);

-- Last accessed timestamp (for usage tracking)
ALTER TABLE memories 
ADD COLUMN IF NOT EXISTS last_accessed_at TIMESTAMPTZ DEFAULT NOW();

-- Supersedes ID (points to old contradicted memory) - use UUID to match memories.id
ALTER TABLE memories 
ADD COLUMN IF NOT EXISTS supersedes_id UUID;

-- Pruning metadata
ALTER TABLE memories 
ADD COLUMN IF NOT EXISTS pruned BOOLEAN DEFAULT FALSE;
ALTER TABLE memories 
ADD COLUMN IF NOT EXISTS pruned_at TIMESTAMPTZ;
ALTER TABLE memories 
ADD COLUMN IF NOT EXISTS pruned_reason TEXT;
ALTER TABLE memories 
ADD COLUMN IF NOT EXISTS superseded_by UUID;

-- Index for pruning queries
CREATE INDEX IF NOT EXISTS idx_memories_pruning 
ON memories(importance_score, last_accessed_at) 
WHERE pruned = FALSE;

CREATE INDEX IF NOT EXISTS idx_memories_supersedes 
ON memories(supersedes_id) WHERE supersedes_id IS NOT NULL;

-- Pruning function (call via cron or app)
CREATE OR REPLACE FUNCTION prune_old_memories()
RETURNS INTEGER AS $$
DECLARE
    pruned_count INTEGER;
BEGIN
    -- Prune memories where importance_score < 3 AND last_accessed_at > 90 days
    WITH to_prune AS (
        UPDATE memories 
        SET pruned = TRUE, 
            pruned_at = NOW(),
            pruned_reason = 'importance_decay',
            metadata = COALESCE(metadata, '{}'::jsonb) || '{"pruned": true, "pruned_reason": "importance_decay"}'::jsonb
        WHERE importance_score < 3 
          AND last_accessed_at < NOW() - INTERVAL '90 days'
          AND pruned = FALSE
        RETURNING id
    )
    SELECT COUNT(*) INTO pruned_count FROM to_prune;
    
    RETURN pruned_count;
END;
$$ LANGUAGE plpgsql;

-- Comments
COMMENT ON COLUMN memories.importance_score IS 'Importance 1-10 (10=critical). Used for pruning decisions.';
COMMENT ON COLUMN memories.last_accessed_at IS 'Last time memory was read/used. For decay calculations.';
COMMENT ON COLUMN memories.supersedes_id IS 'Points to older memory (UUID) that this one contradicts/supersedes.';
COMMENT ON COLUMN memories.pruned IS 'Marked for pruning (soft delete)';
COMMENT ON COLUMN memories.superseded_by IS 'Newer memory (UUID) that supersedes this one';
