-- Migration 014: Add Practice Node Support
-- Purpose: Performance indexes and initialization for passive practice detection
-- Run this in Supabase Dashboard > SQL Editor

-- 1. Index on graph_nodes(type) for fast practice node lookups
--    Used by: detect_practices(), pulse briefing queries
CREATE INDEX IF NOT EXISTS idx_graph_nodes_type 
ON graph_nodes(type);

-- 2. GIN index on graph_nodes(metadata) for JSONB queries
--    Used by: filtering practices by status, entity, declared flag
CREATE INDEX IF NOT EXISTS idx_graph_nodes_metadata 
ON graph_nodes USING GIN (metadata jsonb_path_ops);

-- 3. Initialize dismissed_practice_variants in core_config
--    Stores text patterns for practices Danny has dismissed
INSERT INTO core_config (key, content)
VALUES ('dismissed_practice_variants', '[]')
ON CONFLICT (key) DO NOTHING;

-- 4. Comments for clarity
COMMENT ON INDEX idx_graph_nodes_type IS 'Fast lookup for practice/project/person nodes by type';
COMMENT ON INDEX idx_graph_nodes_metadata IS 'JSONB GIN index for graph_nodes metadata queries';

-- 5. Verify
-- SELECT * FROM pg_indexes WHERE tablename = 'graph_nodes';
-- SELECT * FROM core_config WHERE key = 'dismissed_practice_variants';
