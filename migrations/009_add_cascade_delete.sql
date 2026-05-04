-- Migration: Add ON DELETE CASCADE to foreign keys
-- Purpose: Prevent orphaned records when parent is deleted
-- Run this in Supabase Dashboard > SQL Editor

-- ============================================
-- 1. TASKS table - project_id foreign key
-- ============================================

-- Drop existing constraint (find actual name first)
DO $$
DECLARE
    constraint_name text;
BEGIN
    -- Find the foreign key constraint on tasks.project_id
    SELECT conname INTO constraint_name
    FROM pg_constraint
    WHERE conrelid = 'tasks'::regclass
    AND contype = 'f'
    AND conkey @> ARRAY[(SELECT attnum FROM pg_attribute WHERE attrelid = 'tasks'::regclass AND attname = 'project_id')]
    LIMIT 1;
    
    IF constraint_name IS NOT NULL THEN
        EXECUTE 'ALTER TABLE tasks DROP CONSTRAINT ' || constraint_name;
    END IF;
END $$;

-- Add new CASCADE constraint
ALTER TABLE tasks
ADD CONSTRAINT tasks_project_id_fkey 
FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE;

-- ============================================
-- 2. MEMORIES table - project_id foreign key
-- ============================================

-- Drop existing constraint
DO $$
DECLARE
    constraint_name text;
BEGIN
    SELECT conname INTO constraint_name
    FROM pg_constraint
    WHERE conrelid = 'memories'::regclass
    AND contype = 'f'
    AND conkey @> ARRAY[(SELECT attnum FROM pg_attribute WHERE attrelid = 'memories'::regclass AND attname = 'project_id')]
    LIMIT 1;
    
    IF constraint_name IS NOT NULL THEN
        EXECUTE 'ALTER TABLE memories DROP CONSTRAINT ' || constraint_name;
    END IF;
END $$;

-- Add new CASCADE constraint
ALTER TABLE memories
ADD CONSTRAINT memories_project_id_fkey 
FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE;

-- ============================================
-- 3. GRAPH_EDGES table - source_node_id foreign key
-- ============================================

-- Drop existing constraint
DO $$
DECLARE
    constraint_name text;
BEGIN
    SELECT conname INTO constraint_name
    FROM pg_constraint
    WHERE conrelid = 'graph_edges'::regclass
    AND contype = 'f'
    AND conkey @> ARRAY[(SELECT attnum FROM pg_attribute WHERE attrelid = 'graph_edges'::regclass AND attname = 'source_node_id')]
    LIMIT 1;
    
    IF constraint_name IS NOT NULL THEN
        EXECUTE 'ALTER TABLE graph_edges DROP CONSTRAINT ' || constraint_name;
    END IF;
END $$;

-- Add new CASCADE constraint
ALTER TABLE graph_edges
ADD CONSTRAINT graph_edges_source_node_id_fkey 
FOREIGN KEY (source_node_id) REFERENCES graph_nodes(id) ON DELETE CASCADE;

-- ============================================
-- 4. GRAPH_EDGES table - target_node_id foreign key
-- ============================================

-- Drop existing constraint
DO $$
DECLARE
    constraint_name text;
BEGIN
    SELECT conname INTO constraint_name
    FROM pg_constraint
    WHERE conrelid = 'graph_edges'::regclass
    AND contype = 'f'
    AND conkey @> ARRAY[(SELECT attnum FROM pg_attribute WHERE attrelid = 'graph_edges'::regclass AND attname = 'target_node_id')]
    LIMIT 1;
    
    IF constraint_name IS NOT NULL THEN
        EXECUTE 'ALTER TABLE graph_edges DROP CONSTRAINT ' || constraint_name;
    END IF;
END $$;

-- Add new CASCADE constraint
ALTER TABLE graph_edges
ADD CONSTRAINT graph_edges_target_node_id_fkey 
FOREIGN KEY (target_node_id) REFERENCES graph_nodes(id) ON DELETE CASCADE;

-- ============================================
-- COMMENTS
-- ============================================

COMMENT ON CONSTRAINT tasks_project_id_fkey ON tasks IS 
'Foreign key to projects with CASCADE delete - prevents orphaned tasks';

COMMENT ON CONSTRAINT memories_project_id_fkey ON memories IS 
'Foreign key to projects with CASCADE delete - prevents orphaned memories';

COMMENT ON CONSTRAINT graph_edges_source_node_id_fkey ON graph_edges IS 
'Foreign key to graph_nodes with CASCADE delete - prevents orphaned edges';

COMMENT ON CONSTRAINT graph_edges_target_node_id_fkey ON graph_edges IS 
'Foreign key to graph_nodes with CASCADE delete - prevents orphaned edges';
