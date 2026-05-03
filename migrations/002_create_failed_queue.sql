-- Migration: Create failed_queue table for Phase-2 hardening
-- Tracks failed operations for retry with exponential backoff.

CREATE TABLE IF NOT EXISTS failed_queue (
    id BIGSERIAL PRIMARY KEY,
    source_table TEXT NOT NULL,  -- 'memories', 'raw_dumps', 'graph_edges'
    source_id TEXT NOT NULL,     -- ID of the failed record
    operation TEXT NOT NULL,      -- 'embedding', 'graph_extract', 'memory_insert'
    error_message TEXT,
    retry_count INTEGER DEFAULT 0,
    last_retry_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    metadata JSONB DEFAULT '{}'::jsonb
);

-- Index for efficient polling (find items that need retry)
CREATE INDEX IF NOT EXISTS idx_failed_queue_retry 
ON failed_queue(retry_count, last_retry_at);

-- Index for cleanup (old failed items)
CREATE INDEX IF NOT EXISTS idx_failed_queue_created 
ON failed_queue(created_at);

-- Index for items that need immediate retry (last_retry_at IS NULL)
CREATE INDEX IF NOT EXISTS idx_failed_queue_null_retry 
ON failed_queue(created_at) 
WHERE last_retry_at IS NULL;

-- Add comment for documentation
COMMENT ON TABLE failed_queue IS 'Stores failed operations for retry with exponential backoff. Part of Phase-2 hardening.';
COMMENT ON COLUMN failed_queue.source_table IS 'Table where the operation failed: memories, raw_dumps, graph_edges';
COMMENT ON COLUMN failed_queue.source_id IS 'ID of the record that failed (stored as TEXT for flexibility)';
COMMENT ON COLUMN failed_queue.operation IS 'Type of operation that failed: embedding, graph_extract, memory_insert';
COMMENT ON COLUMN failed_queue.retry_count IS 'Number of retry attempts (max 5 before manual review)';
COMMENT ON COLUMN failed_queue.last_retry_at IS 'Timestamp of last retry attempt (NULL if never retried)';
