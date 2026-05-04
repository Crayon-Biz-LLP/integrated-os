-- Migration 009 v3: Add project_id column + Foreign Key
-- Run this in Supabase Dashboard > SQL Editor

-- ============================================
-- 1. ADD project_id COLUMN (if not exists)
-- ============================================

-- Tasks table
ALTER TABLE tasks 
ADD COLUMN IF NOT EXISTS project_id BIGINT;

-- Memories table (if exists)
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'memories' AND table_schema = 'public') THEN
        ALTER TABLE memories ADD COLUMN IF NOT EXISTS project_id BIGINT;
    END IF;
END $$;

-- ============================================
-- 2. DROP OLD CONSTRAINTS (if any)
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
-- 3. ADD FOREIGN KEY (SET NULL to preserve records)
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
-- 4. GRAPH_EDGES (if graph_nodes exists)
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
-- 5. VERIFY
-- ============================================

SELECT 
    conname as constraint_name,
    conrelid::regclass as table_name,
    confrelid::regclass as references_table
FROM pg_constraint
WHERE conrelid IN ('tasks'::regclass, 'memories'::regclass, 'graph_edges'::regclass)
AND contype = 'f';
