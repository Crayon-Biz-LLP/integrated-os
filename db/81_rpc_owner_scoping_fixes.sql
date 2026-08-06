-- 81_rpc_owner_scoping_fixes.sql
-- M3 follow-up: db/79's match_conversations was written LANGUAGE sql, where
-- an unqualified param name that matches a column resolves to the COLUMN —
-- `owner_id IS NULL OR conversations.owner_id = owner_id` became
-- `conversations.owner_id = conversations.owner_id` (always true), silently
-- disabling tenant scoping. Same bug class verified on the copy DB via
-- match_resources (db/80). Convert to plpgsql with the p_owner snapshot
-- pattern used across db/80. The param stays named `owner_id` (PostgREST
-- named-arg injection compatibility); only the body references change.
--
-- NOTE: in a plpgsql RETURNS TABLE function the OUT params shadow column
-- names, so every output column is table-qualified (the pattern all other
-- scoped RPCs use).

DROP FUNCTION IF EXISTS public.match_conversations(vector, integer, double precision, bigint[], uuid);
CREATE OR REPLACE FUNCTION public.match_conversations(query_embedding vector, match_count integer DEFAULT 3, match_threshold double precision DEFAULT 0.5, exclude_ids bigint[] DEFAULT '{}'::bigint[], owner_id uuid DEFAULT NULL::uuid)
 RETURNS TABLE(id bigint, role text, intent text, content text, created_at timestamp with time zone, similarity double precision)
 LANGUAGE plpgsql
 STABLE
AS $function$
DECLARE
    p_owner uuid := owner_id;
BEGIN
    RETURN QUERY
    SELECT
        conversations.id,
        conversations.role,
        conversations.intent,
        conversations.content,
        conversations.created_at,
        1 - (conversations.embedding <=> query_embedding) AS similarity
    FROM conversations
    WHERE conversations.embedding IS NOT NULL
      AND (cardinality(exclude_ids) = 0 OR NOT (conversations.id = ANY(exclude_ids)))
      AND (p_owner IS NULL OR conversations.owner_id = p_owner)
      AND 1 - (conversations.embedding <=> query_embedding) > match_threshold
    ORDER BY conversations.embedding <=> query_embedding
    LIMIT match_count;
END;
$function$;
