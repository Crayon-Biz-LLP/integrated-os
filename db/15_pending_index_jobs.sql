-- Migration: pending_retrieval_index_jobs table for queued indexing work
-- Replaces fire-and-forget asyncio.create_task in schedule_index_memory
-- (which was killed on Vercel serverless return before LLM extraction completed).
--
-- The sentinel piggyback (process_pending_index_jobs) sweeps pending jobs
-- atomically, preventing double-processing and tracking retries.
--
-- A partial UNIQUE index prevents duplicate active jobs per memory_id.

CREATE TABLE IF NOT EXISTS pending_retrieval_index_jobs (
    id BIGINT PRIMARY KEY GENERATED ALWAYS AS IDENTITY,
    memory_id BIGINT NOT NULL,
    content TEXT NOT NULL,
    memory_type TEXT NOT NULL DEFAULT 'note',
    source TEXT,
    priority INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'processing', 'completed', 'dead_letter')),
    retry_count INTEGER DEFAULT 0,
    error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ
);

-- Only one active job per memory at a time
CREATE UNIQUE INDEX IF NOT EXISTS idx_pending_index_jobs_memory
    ON pending_retrieval_index_jobs (memory_id)
    WHERE status IN ('pending', 'processing');

-- Index for the sweep query: status + priority + created_at
CREATE INDEX IF NOT EXISTS idx_pending_index_jobs_status
    ON pending_retrieval_index_jobs (status, priority DESC, created_at ASC);
