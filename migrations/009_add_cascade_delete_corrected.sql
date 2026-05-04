-- Migration 009 (CORRECTED): Add project_id column + ON DELETE CASCADE
-- Run this in Supabase Dashboard > SQL Editor

-- ============================================
-- 1. CHECK IF COLUMNS EXIST FIRST
-- ============================================

DO $$
DECLARE
    col_exists boolean;
BEGIN
    -- Check tasks.project_id
    SELECT EXISTS (
        SELECT 1 FROM information_schema.columns 
        WHERE table_name = 'tasks' 
        AND column_name = 'project_id'
        AND table_schema = 'public'
    ) INTO col_exists;
    
    IF NOT col_exists THEN
        RAISE NOTICE 'Column tasks.project_id does not exist - need to add it';
    ELSE
        RAISE NOTICE 'Column tasks.project_id exists';
    END IF;
    
    -- Check memories.project_id
    SELECT EXISTS (
        SELECT 1 FROM information_schema.columns 
        WHERE table_name = 'memories' 
        AND column_name = 'project_id'
        AND table_schema = 'public'
    ) INTO col_exists;
    
    IF NOT col_exists THEN
        RAISE NOTICE 'Column memories.project_id does not exist - need to add it';
    ELSE
        RAISE NOTICE 'Column memories.project_id exists';
    END IF;
END $$;

-- ============================================
-- 2. ADD project_id COLUMN IF MISSING
-- ============================================

-- Add to tasks table (BIGINT to match projects.id)
ALTER TABLE tasks 
ADD COLUMN IF NOT EXISTS project_id BIGINT;

-- Add to memories table (BIGINT to match projects.id)  
ALTER TABLE memories 
ADD COLUMN IF NOT EXISTS project_id BIGINT;

-- ============================================
-- 3. DROP EXISTING FOREIGN KEY CONSTRAINTS (if any)
-- ============================================

DO $$
DECLARE
    constraint_name text;
BEGIN
    -- Tasks table
    SELECT conname INTO constraint_name
    FROM pg_constraint
    WHERE conrelid = 'tasks'::regclass
    AND contype = 'f'
    AND conkey @> ARRAY[(SELECT attnum FROM pg_attribute WHERE attrelid = 'tasks'::regclass AND attname = 'project_id')]
    LIMIT 1;
    
    IF constraint_name IS NOT NULL THEN
        EXECUTE 'ALTER TABLE tasks DROP CONSTRAINT ' || constraint_name;
        RAISE NOTICE 'Dropped constraint % on tasks', constraint_name;
    END IF;
    
    -- Memories table
    SELECT conname INTO constraint_name
    FROM pg_constraint
    WHERE conrelid = 'memories'::regclass
    AND contype = 'f'
    AND conkey @> ARRAY[(SELECT attnum FROM pg_attribute WHERE attrelid = 'memories'::regclass AND attname = 'project_id')]
    LIMIT 1;
    
    IF constraint_name IS NOT NULL THEN
        EXECUTE 'ALTER TABLE memories DROP CONSTRAINT ' || constraint_name;
        RAISE NOTICE 'Dropped constraint % on memories', constraint_name;
    END IF;
END $$;

-- ============================================
-- 4. ADD FOREIGN KEY CONSTRAINTS WITH CASCADE
-- ============================================

-- Tasks -> Projects
ALTER TABLE tasks
ADD CONSTRAINT tasks_project_id_fkey 
FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE SET NULL;

-- Note: Using SET NULL instead of CASCADE
-- This preserves tasks even if project is deleted (just sets project_id to NULL)

-- Memories -> Projects  
ALTER TABLE memories
ADD CONSTRAINT memories_project_id_fkey 
FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE SET NULL;

-- ============================================
-- 5. GRAPH_EDGES FOREIGN KEYS (if nodes exist)
-- ============================================

-- Check if graph_nodes table exists and has id column
DO $$
DECLARE
    nodes_exist boolean;
BEGIN
    SELECT EXISTS (
        SELECT 1 FROM information_schema.tables 
        WHERE table_name = 'graph_nodes' AND table_schema = 'public'
    ) INTO nodes_exist;
    
    IF NOT nodes_exist THEN
        RAISE NOTICE 'graph_nodes table does not exist - skipping graph_edges FK';
        RETURN;
    END IF;
    
    -- Drop existing constraints if any
    BEGIN
        ALTER TABLE graph_edges DROP CONSTRAINT IF EXISTS graph_edges_source_node_id_fkey;
        ALTER TABLE graph_edges DROP CONSTRAINT IF EXISTS graph_edges_target_node_id_fkey;
    END;
    
    -- Add CASCADE constraints
    BEGIN
        ALTER TABLE graph_edges
        ADD CONSTRAINT graph_edges_source_node_id_fkey 
        FOREIGN KEY (source_node_id) REFERENCES graph_nodes(id) ON DELETE CASCADE;
        
        ALTER TABLE graph_edges
        ADD CONSTRAINT graph_edges_target_node_id_fkey 
        FOREIGN KEY (target_node_id) REFERENCES graph_nodes(id) ON DELETE CASCADE;
        
        RAISE NOTICE 'Added CASCADE constraints for graph_edges';
    EXCEPTION WHEN OTHERS THEN
        RAISE NOTICE 'Failed to add graph_edges constraints: %', SQLERRM;
    END;
END $$;

-- ============================================
-- 6. VERIFICATION
-- ============================================

SELECT 
    conname as constraint_name,
    conrelid::regclass as table_name,
    confrelid::regclass as references_table,
    CASE confdeltype 
        WHEN 'a' THEN 'NO ACTION'
        WHEN 'r' THEN 'RESTRICT'
        WHEN 'c' THEN 'CASCADE'
        WHEN 'n' THEN 'SET NULL'
        WHEN 'd' THEN 'SET DEFAULT'
    END as on_delete
FROM pg_constraint
WHERE conrelid IN ('tasks'::regclass, 'memories'::regclass, 'graph_edges'::regclass)
AND contype = 'f'
ORDER BY conrelid::regclass::text;
