-- Phase 1: Replace ILIKE with indexed Postgres full-text search.
-- Adds search_vector column, GIN index, auto-population trigger,
-- and search_phrase_nodes RPC function.

-- ============================================================
-- 1. Add search_vector column
-- ============================================================
ALTER TABLE public.retrieval_phrase_nodes
    ADD COLUMN IF NOT EXISTS search_vector tsvector;

-- ============================================================
-- 2. Populate existing rows
-- ============================================================
UPDATE public.retrieval_phrase_nodes
SET search_vector = to_tsvector('simple', normalized_text)
WHERE search_vector IS NULL;

-- ============================================================
-- 3. GIN index for fast full-text search
-- ============================================================
CREATE INDEX IF NOT EXISTS idx_rpn_search_vector
    ON public.retrieval_phrase_nodes USING GIN (search_vector);

-- ============================================================
-- 4. Trigger to auto-populate on INSERT/UPDATE
-- ============================================================
CREATE OR REPLACE FUNCTION public.update_phrase_search_vector()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    NEW.search_vector := to_tsvector('simple', NEW.normalized_text);
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_phrase_search_vector ON public.retrieval_phrase_nodes;
CREATE TRIGGER trg_phrase_search_vector
    BEFORE INSERT OR UPDATE OF normalized_text
    ON public.retrieval_phrase_nodes
    FOR EACH ROW
    EXECUTE FUNCTION public.update_phrase_search_vector();

-- ============================================================
-- 5. Search function — accepts tsquery text, returns ranked nodes
-- ============================================================
CREATE OR REPLACE FUNCTION public.search_phrase_nodes(
    query_text TEXT,
    result_limit INT DEFAULT 30
)
RETURNS TABLE (
    id BIGINT,
    normalized_text TEXT,
    display_text TEXT,
    node_type TEXT,
    rank REAL
)
LANGUAGE plpgsql STABLE
AS $$
BEGIN
    IF query_text IS NULL OR length(trim(query_text)) = 0 THEN
        RETURN;
    END IF;

    RETURN QUERY
    SELECT rpn.id,
           rpn.normalized_text,
           rpn.display_text,
           rpn.node_type,
           ts_rank_cd(rpn.search_vector, to_tsquery('simple', query_text))::REAL AS rank
    FROM public.retrieval_phrase_nodes rpn
    WHERE rpn.search_vector @@ to_tsquery('simple', query_text)
    ORDER BY rank DESC
    LIMIT result_limit;
END;
$$;
