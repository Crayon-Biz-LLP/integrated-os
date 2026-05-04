-- Migration: Add is_current for temporal lineage
-- Purpose: Enable version history - track how thoughts/projects evolve over time
-- Run this in Supabase Dashboard > SQL Editor

-- Add is_current to memories (default TRUE for existing rows)
ALTER TABLE memories 
ADD COLUMN IF NOT EXISTS is_current BOOLEAN DEFAULT TRUE;

-- Add version tracking
ALTER TABLE memories 
ADD COLUMN IF NOT EXISTS version INTEGER DEFAULT 1;

ALTER TABLE memories 
ADD COLUMN IF NOT EXISTS supersedes_id BIGINT;  -- Points to previous version

-- Add is_current to tasks
ALTER TABLE tasks 
ADD COLUMN IF NOT EXISTS is_current BOOLEAN DEFAULT TRUE;

ALTER TABLE tasks 
ADD COLUMN IF NOT EXISTS version INTEGER DEFAULT 1;

ALTER TABLE tasks 
ADD COLUMN IF NOT EXISTS supersedes_id BIGINT;

-- Add is_current to projects  
ALTER TABLE projects 
ADD COLUMN IF NOT EXISTS is_current BOOLEAN DEFAULT TRUE;

ALTER TABLE projects 
ADD COLUMN IF NOT EXISTS version INTEGER DEFAULT 1;

ALTER TABLE projects 
ADD COLUMN IF NOT EXISTS supersedes_id BIGINT;

-- Indexes for version queries
CREATE INDEX IF NOT EXISTS idx_memories_version 
ON memories(is_current) WHERE is_current = TRUE;

CREATE INDEX IF NOT EXISTS idx_tasks_version 
ON tasks(is_current) WHERE is_current = TRUE;

CREATE INDEX IF NOT EXISTS idx_projects_version 
ON projects(is_current) WHERE is_current = TRUE;

CREATE INDEX IF NOT EXISTS idx_memories_supersedes 
ON memories(supersedes_id) WHERE supersedes_id IS NOT NULL;

-- Temporal query function: Get state at a specific time
CREATE OR REPLACE FUNCTION get_memory_at_time(
    memory_id BIGINT,
    query_time TIMESTAMPTZ
)
RETURNS TABLE (
    id BIGINT,
    content TEXT,
    version INTEGER,
    created_at TIMESTAMPTZ,
    metadata JSONB
) AS $$
BEGIN
    RETURN QUERY
    SELECT m.id, m.content, m.version, m.created_at, m.metadata
    FROM memories m
    WHERE m.id = memory_id 
       OR m.supersedes_id = memory_id
    ORDER BY m.version DESC
    LIMIT 1;
END;
$$ LANGUAGE plpgsql;

-- Drift detection: Count updates in time window
CREATE OR REPLACE FUNCTION detect_drift(
    project_name TEXT,
    hours_window INTEGER DEFAULT 48
)
RETURNS TABLE (
    update_count BIGINT,
    first_update TIMESTAMPTZ,
    last_update TIMESTAMPTZ
) AS $$
BEGIN
    RETURN QUERY
    SELECT 
        COUNT(*) as update_count,
        MIN(created_at) as first_update,
        MAX(created_at) as last_update
    FROM memories
    WHERE metadata->>'project' = project_name
      AND created_at > NOW() - (hours_window || ' hours')::INTERVAL
      AND metadata->>'type' = 'project_goal_update';
END;
$$ LANGUAGE plpgsql;

-- Comments
COMMENT ON COLUMN memories.is_current IS 'TRUE for current version, FALSE for historical versions';
COMMENT ON COLUMN memories.version IS 'Version number (increments on each update)';
COMMENT ON COLUMN memories.supersedes_id IS 'Points to previous version (creates version chain)';
COMMENT ON COLUMN tasks.is_current IS 'TRUE for current version, FALSE for historical';
COMMENT ON COLUMN projects.is_current IS 'TRUE for current version, FALSE for historical';
