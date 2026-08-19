-- Fix 1: created_entity_id should be text (can be an int task id or uuid note id)
ALTER TABLE public.document_items 
  ALTER COLUMN created_entity_id TYPE TEXT USING created_entity_id::text;

-- Fix 2: relax the enrichment job type check to include 'doc_enrich'
ALTER TABLE public.pending_enrichment_jobs 
  DROP CONSTRAINT IF EXISTS pending_enrichment_jobs_job_type_check;

ALTER TABLE public.pending_enrichment_jobs 
  ADD CONSTRAINT pending_enrichment_jobs_job_type_check 
  CHECK (job_type = ANY (ARRAY['task_graph'::text, 'note_enrich'::text, 'doc_enrich'::text]));

-- Fix 3: relax the target type check to include 'document'
ALTER TABLE public.pending_enrichment_jobs 
  DROP CONSTRAINT IF EXISTS pending_enrichment_jobs_target_type_check;

ALTER TABLE public.pending_enrichment_jobs 
  ADD CONSTRAINT pending_enrichment_jobs_target_type_check 
  CHECK (target_type = ANY (ARRAY['task'::text, 'note'::text, 'document'::text]));
