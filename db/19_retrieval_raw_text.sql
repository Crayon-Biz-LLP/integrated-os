-- Phase 3: Chunk Enrichment — separate raw text from enriched (embedded) text.
-- Enriched text (with metadata prefix) lives in `text` and is what gets embedded.
-- Raw user content lives in `raw_text` and is what gets displayed to users.

-- 1. Add raw_text column
ALTER TABLE public.retrieval_passages
    ADD COLUMN IF NOT EXISTS raw_text TEXT;

-- 2. Backfill: existing rows have raw content in `text`, no enrichment prefix yet.
--    Copy current `text` → `raw_text` so downstream display code can use raw_text.
UPDATE public.retrieval_passages
SET raw_text = text
WHERE raw_text IS NULL;

-- 3. Recreate the unique idempotency index to use raw_text + passage_index + index_version.
--    The old index used source_fingerprint which is a content hash of the raw text.
--    With enrichment, the same raw text gets a different `text` value (prefixed),
--    so we keep source_fingerprint on raw content and add raw_text for display.
--    No index change needed — source_fingerprint is already computed from raw content.
--    But we add a NOT NULL constraint now that backfill is done.
ALTER TABLE public.retrieval_passages
    ALTER COLUMN raw_text SET NOT NULL;
