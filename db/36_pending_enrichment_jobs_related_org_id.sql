-- Migration 36: Add related_org_id to pending_enrichment_jobs
-- 
-- The enqueue_enrichment function passes related_org_id for
-- task→organization BELONGS_TO edge creation in the enrichment
-- pipeline, but the column was missing from the table schema.
-- This caused the enrichment insert to fail silently (caught by
-- try/except in enqueue_enrichment), meaning tasks created with
-- organization_id never got their enrichment jobs.
--
-- Fixed by: Adding the related_org_id TEXT column.
-- See also: core/lib/enrichment_queue.py → enqueue_enrichment()

ALTER TABLE pending_enrichment_jobs
    ADD COLUMN IF NOT EXISTS related_org_id TEXT;

-- Verify
SELECT column_name, data_type
FROM information_schema.columns
WHERE table_name = 'pending_enrichment_jobs'
  AND column_name = 'related_org_id';
