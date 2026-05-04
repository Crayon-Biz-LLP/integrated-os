-- Migration 009 v2: Add project_id column + CASCADE (CORRECTED)
-- Run this in Supabase Dashboard > SQL Editor

-- ============================================
-- STEP 1: Add project_id column if missing
-- ============================================

-- Tasks table (BIGINT to match projects.id)
ALTER TABLE tasks 
ADD COLUMN IF NOT EXISTS project_id BIGINT;

-- Memories table (BIGINT to match projects.id)  
ALTER TABLE memories 
ADD COLUMN IF NOT EXISTS project_id BIGINT;

-- ============================================
-- STEP 2: Drop old FK constraints (if any)
-- ============================================

DO $$
DECLARE
    r RECORD;
BEGIN
    -- Drop tasks FK
    FOR r IN 
        SELECT conname 
        FROM pg_constraint 
        WHERE conrelid = 'tasks'::regclass 
        AND contype = 'f'
    LOOP
        EXECUTE 'ALTER TABLE tasks DROP CONSTRAINT ' || r.conname;
    END LOOP;
    
    -- Drop memories FK
    FOR r IN 
        SELECT conname 
        FROM pg_constraint 
        WHERE conrelid = 'memories'::regclass 
        AND contype = 'f'
    LOOP
        EXECUTE 'ALTER TABLE memories DROP CONSTRAINT ' || r.conname;
    END LOOP;
END $$;

-- ============================================
-- STEP 3: Add FK with SET NULL (not CASCADE)
-- ============================================

-- Tasks -> Projects (SET NULL: keeps tasks if project deleted)
ALTER TABLE tasks
ADD CONSTRAINT tasks_project_id_fkey 
FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE SET NULL;

-- Memories -> Projects
ALTER TABLE memories
ADD CONSTRAINT memories_project_id_fkey 
FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE SET NULL;

-- ============================================
-- STEP 4: Graph Edges CASCADE (if graph_nodes exists)
-- ============================================

DO $$
BEGIN
    -- Check if graph_nodes table exists
    IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'graph_nodes' AND table_schema = 'public') THEN
        -- Drop old constraints
        ALTER TABLE graph_edges DROP CONSTRAINT IF EXISTS graph_edges_source_node_id_fkey;
        ALTER TABLE graph_edges DROP CONSTRAINT IF EXISTS graph_edges_target_node_id_fkey;
        
        -- Add CASCADE constraints
        ALTER TABLE graph_edges
        ADD CONSTRAINT graph_edges_source_node_id_fkey 
        FOREIGN KEY (source_node_id) REFERENCES graph_nodes(id) ON DELETE CASCADE;
        
        ALTER TABLE graph_edges
        ADD CONSTRAINT graph_edges_target_node_id_fkey 
        FOREIGN KEY (target_node_id) REFERENCES graph_nodes(id) ON DELETE CASCADE;
        
        RAISE NOTICE 'Added CASCADE constraints for graph_edges';
    ELSE
        RAISE NOTICE 'graph_nodes table does not exist - skipping';
    END IF;
END $$;

-- ============================================
-- VERIFICATION
-- ============================================

SELECT 
    conname as constraint_name,
    conrelid::regclass as table_name,
    confrelid::regclass as references_table,
    CASE confdeltype 
        WHEN 'a' THEN 'NO ACTION'
        WHEN 'c' THEN 'CASCADE'
        WHEN 'n' THEN 'SET NULL'
    END as on_delete
FROM pg_constraint
WHERE conrelid IN ('tasks'::regclass, 'memories'::regclass, 'graph_edges'::regclass)
AND contype = 'f'
ORDER BY conrelid::regclass::text;
