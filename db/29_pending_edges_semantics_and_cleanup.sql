-- 1. Add approval_source column to distinguish how an edge reached its terminal status
ALTER TABLE pending_graph_edges ADD COLUMN approval_source TEXT NOT NULL DEFAULT 'pending';

-- Backfill approval_source based on existing data heuristics
UPDATE pending_graph_edges
SET approval_source = 'provenance'
WHERE source_text = 'insert_extracted_entities' AND status = 'approved';

UPDATE pending_graph_edges
SET approval_source = 'hitl'
WHERE status = 'approved' AND approval_source = 'pending';

UPDATE pending_graph_edges
SET approval_source = 'hitl'
WHERE status = 'rejected' AND approval_source = 'pending';

-- 2. Index the review path for fast UI/API listings
CREATE INDEX IF NOT EXISTS idx_pending_edges_review 
ON pending_graph_edges (status, created_at DESC) 
WHERE status IN ('pending', 'flagged');

-- 3. Create an archive table to prevent unbounded growth of the active review ledger
CREATE TABLE IF NOT EXISTS pending_graph_edges_archive (
    LIKE pending_graph_edges INCLUDING ALL
);

-- We don't add foreign keys to the archive table for source_node_id / target_node_id 
-- because we want the archive to persist even if the underlying nodes are deleted.
ALTER TABLE pending_graph_edges_archive DROP CONSTRAINT IF EXISTS pending_graph_edges_source_node_id_fkey;
ALTER TABLE pending_graph_edges_archive DROP CONSTRAINT IF EXISTS pending_graph_edges_target_node_id_fkey;
