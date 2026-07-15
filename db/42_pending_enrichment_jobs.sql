-- Pending Enrichment Jobs Table
-- Converts fire-and-forget enrichment (graph edges, entity extraction, embedding)
-- into a queue-based pattern that survives Vercel cold kills.
--
-- Jobs are enqueued synchronously during create_task_direct / create_note_direct.
-- The sentinel piggyback processes them in batches with atomic claim + retry.
--
-- Job types:
--   task_graph      → write_graph_edges_for_task + extract_and_link_entities for a task
--   note_enrich     → extract_and_link_entities + get_embedding + update sentiment/entities_mentioned for a note

CREATE TABLE IF NOT EXISTS pending_enrichment_jobs (
    id              SERIAL PRIMARY KEY,
    job_type        TEXT NOT NULL CHECK (job_type IN ('task_graph', 'note_enrich')),
    target_type     TEXT NOT NULL CHECK (target_type IN ('task', 'note')),
    target_id       INTEGER NOT NULL,
    content         TEXT NOT NULL,
    related_id      TEXT,          -- project_id for task jobs, source for note jobs
    status          TEXT NOT NULL DEFAULT 'pending'
                        CHECK (status IN ('pending', 'processing', 'completed', 'failed', 'dead_letter')),
    retry_count     INTEGER NOT NULL DEFAULT 0,
    error           TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    started_at      TIMESTAMPTZ,
    completed_at    TIMESTAMPTZ
);

-- Index for atomic claim: find pending jobs, order by priority (created_at ASC)
CREATE INDEX IF NOT EXISTS idx_pending_enrichment_jobs_claim
    ON pending_enrichment_jobs (status, created_at ASC)
    WHERE status = 'pending';

-- Index for dead letter escalation
CREATE INDEX IF NOT EXISTS idx_pending_enrichment_jobs_retry
    ON pending_enrichment_jobs (status, retry_count)
    WHERE status = 'failed';

-- Prevent duplicate queue entries for the same target
CREATE UNIQUE INDEX IF NOT EXISTS idx_pending_enrichment_jobs_dedup
    ON pending_enrichment_jobs (job_type, target_id, target_type)
    WHERE status IN ('pending', 'processing');

-- Grant access
GRANT ALL ON pending_enrichment_jobs TO service_role;
GRANT USAGE ON SEQUENCE pending_enrichment_jobs_id_seq TO service_role;

-- Add RPC for atomic claim (prevents double-processing on concurrent sentinel runs)
CREATE OR REPLACE FUNCTION claim_pending_enrichment_job(job_id INTEGER)
RETURNS SETOF pending_enrichment_jobs
LANGUAGE plpgsql
SECURITY DEFINER
AS $$
BEGIN
    RETURN QUERY
    UPDATE pending_enrichment_jobs
    SET status = 'processing',
        started_at = NOW(),
        retry_count = retry_count + 1
    WHERE id = job_id
      AND status = 'pending'
    RETURNING *;
END;
$$;

GRANT EXECUTE ON FUNCTION claim_pending_enrichment_job TO service_role;
