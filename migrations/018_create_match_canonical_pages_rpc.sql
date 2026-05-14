-- Migration 018: Create match_canonical_pages RPC
-- Mirrors match_memories pattern for canonical_pages table
-- Enables semantic vector search on canonical (master) pages

DROP FUNCTION IF EXISTS public.match_canonical_pages(query_embedding jsonb, match_threshold double precision, match_count integer);

CREATE OR REPLACE FUNCTION public.match_canonical_pages(
    query_embedding jsonb,
    match_threshold double precision,
    match_count integer
)
RETURNS TABLE(id bigint, title text, content text, similarity double precision, updated_at timestamptz)
LANGUAGE plpgsql
AS $function$
DECLARE
    q_vec vector(768);
BEGIN
    q_vec := query_embedding::text::vector(768);
    RETURN QUERY
    SELECT
        cp.id,
        cp.title,
        cp.content,
        1 - (cp.embedding <=> q_vec) AS similarity,
        cp.updated_at
    FROM canonical_pages cp
    WHERE cp.embedding IS NOT NULL
        AND (cp.embedding <=> q_vec) IS NOT NULL
        AND (cp.embedding <=> q_vec) < 2
        AND (1 - (cp.embedding <=> q_vec)) > match_threshold
    ORDER BY similarity DESC
    LIMIT match_count;
END;
$function$;
