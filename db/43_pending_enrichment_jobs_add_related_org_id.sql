-- Pending Enrichment Jobs: Add related_org_id column
--
-- The Python code in core/lib/enrichment_queue.py already writes
-- related_org_id to the insert_data dict when creating enrichment jobs
-- for tasks with an organization_id. But the column was never added to
-- the table schema in db/42_pending_enrichment_jobs.sql.
--
-- This means enrichment jobs for tasks WITH an organization reference
-- silently fail to be created (enqueue_enrichment hits a column-not-found
-- error and returns False). The task→org BELONGS_TO edge is never queued
-- for enrichment.
--
-- Fix: Add the missing column, then existing code will work.

ALTER TABLE pending_enrichment_jobs
ADD COLUMN IF NOT EXISTS related_org_id BIGINT;

-- Index for filtering by org (useful for debugging/admin)
CREATE INDEX IF NOT EXISTS idx_pending_enrichment_jobs_org
    ON pending_enrichment_jobs (related_org_id)
    WHERE related_org_id IS NOT NULL;
