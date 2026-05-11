-- Migration 013: Fix match_memories RPC
-- Issues fixed:
--   1. query_embedding now accepts jsonb (Python lists from supabase-py)
--   2. Added explicit vector(768) cast to ensure correct dimension
--   3. Removed broken UNION ALL with resources table (NaN poisoning)
--   4. Added NaN guard: (embedding <=> q_vec) < 2 filters zero-vector embeddings
--   5. Returns created_at for application-level date filtering
--   6. Removed unused filter parameter (date filtering done at app layer)
-- Drop old overloads first
DROP FUNCTION IF EXISTS public.match_memories(query_embedding vector, match_threshold double precision, match_count integer);
DROP FUNCTION IF EXISTS public.match_memories(query_embedding jsonb, match_threshold double precision, match_count integer, filter jsonb);
-- Create the fixed function
CREATE OR REPLACE FUNCTION public.match_memories(
    query_embedding jsonb,
    match_threshold double precision,
    match_count integer
)
RETURNS TABLE(id bigint, content text, metadata jsonb, similarity double precision, created_at timestamptz)
LANGUAGE plpgsql
AS $function$
DECLARE
    q_vec vector(768);
BEGIN
    q_vec := query_embedding::text::vector(768);
    RETURN QUERY
    SELECT
        m.id,
        m.content,
        m.metadata,
        1 - (m.embedding <=> q_vec) AS similarity,
        m.created_at
    FROM memories m
    WHERE m.embedding IS NOT NULL
        AND (m.embedding <=> q_vec) IS NOT NULL
        AND (m.embedding <=> q_vec) < 2
        AND (1 - (m.embedding <=> q_vec)) > match_threshold
    ORDER BY similarity DESC
    LIMIT match_count;
END;
$function$