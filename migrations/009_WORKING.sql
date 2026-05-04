-- Migration 009 WORKING: Add column + Foreign Key
-- Run this in Supabase Dashboard > SQL Editor

-- ============================================
-- STEP 1: Add project_id column (BIGINT to match projects.id)
-- ============================================

-- Tasks table
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS project_id BIGINT;

-- Memories table (if exists)
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'memories' AND table_schema = 'public') THEN
        ALTER TABLE memories ADD COLUMN IF NOT EXISTS project_id BIGINT;
    END IF;
END $$;

-- Verify columns exist
SELECT 
    table_name, 
    column_name, 
    data_type 
FROM information_schema.columns 
WHERE table_name IN ('tasks', 'memories') 
AND column_name = 'project_id'
AND table_schema = 'public';

-- ============================================
-- STEP 2: Drop old FK constraints (if any)
-- ============================================

DO $$
DECLARE
    r RECORD;
BEGIN
    -- Tasks
    FOR r IN SELECT conname FROM pg_constraint WHERE conrelid = 'tasks'::regclass AND contype = 'f' LOOP
        EXECUTE 'ALTER TABLE tasks DROP CONSTRAINT ' || r.conname;
    END LOOP;
    
    -- Memories
    FOR r IN SELECT conname FROM pg_constraint WHERE conrelid = 'memories'::regclass AND contype = 'f' LOOP
        EXECUTE 'ALTER TABLE memories DROP CONSTRAINT ' || r.conname;
    END LOOP;
END $$;

-- ============================================
-- STEP 3: Add Foreign Key with SET NULL
-- ============================================

-- Tasks -> Projects
ALTER TABLE tasks
ADD CONSTRAINT tasks_project_id_fkey 
FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE SET NULL;

-- Memories -> Projects
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'memories' AND table_schema = 'public') THEN
        ALTER TABLE memories
        ADD CONSTRAINT memories_project_id_fkey 
        FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE SET NULL;
    END IF;
END $$;

-- ============================================
-- STEP 4: Graph Edges (if graph_nodes exists)
-- ============================================

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'graph_nodes' AND table_schema = 'public') THEN
        ALTER TABLE graph_edges DROP CONSTRAINT IF EXISTS graph_edges_source_node_id_fkey;
        ALTER TABLE graph_edges DROP CONSTRAINT IF EXISTS graph_edges_target_node_id_fkey;
        
        ALTER TABLE graph_edges
        ADD CONSTRAINT graph_edges_source_node_id_fkey 
        FOREIGN KEY (source_node_id) REFERENCES graph_nodes(id) ON DELETE CASCADE;
        
        ALTER TABLE graph_edges
        ADD CONSTRAINT graph_edges_target_node_id_fkey 
        FOREIGN KEY (target_node_id) REFERENCES graph_nodes(id) ON DELETE CASCADE;
    END IF;
END $$;

-- ============================================
-- VERIFICATION: Should show new constraints
-- ============================================

SELECT 
    conname as constraint_name,
    conrelid::regclass as table_name,
    confrelid::regclass as references_table
FROM pg_constraint
WHERE conrelid IN ('tasks'::regclass, 'memories'::regclass, 'graph_edges'::regclass)
AND contype = 'f'
ORDER BY conrelid::regclass::text;
