-- 79_rpc_owner_scoping.sql
-- ============================================================================
-- M3: Tenant-scope RPCs — plans/69-multi-tenant-product-plan.md (§6 M3)
--
-- Every retrieval/state RPC gains an `owner_id uuid DEFAULT NULL` parameter
-- and a WHERE owner_id filter. DEFAULT NULL keeps PRE-db/78 legacy calls
-- working: when the caller omits owner_id (old code, scripts, tests before
-- the sweep) the filter is a no-op; once the M3 facade passes owner_id, rows
-- are strictly scoped to that tenant.
--
-- This file covers the RPCs used by core/webhook/*. The remaining match_* /
-- search / claim RPCs are scoped in their own M3 module passes.
--
-- DEPLOYMENT ORDER: same as db/78 — land with the M1–M3 code sweep, never
-- ahead of it (a scoped facade calling an RPC without the param would error
-- on an unknown parameter; the code passes owner_id only after this lands).
-- ============================================================================


-- ── match_conversations (used by core/webhook/dispatch.py interrogate_brain)
-- Finds semantically similar past conversation exchanges.
-- DROP the pre-scoping 4-arg overload FIRST: CREATE OR REPLACE with an
-- added parameter creates a NEW overload instead of replacing — leaving a
-- duplicate function is dead weight and a PostgREST ambiguity hazard.
DROP FUNCTION IF EXISTS public.match_conversations(vector(768), int, float, bigint[]);

CREATE OR REPLACE FUNCTION public.match_conversations(
    query_embedding vector(768),
    match_count int DEFAULT 3,
    match_threshold float DEFAULT 0.5,
    exclude_ids bigint[] DEFAULT '{}',
    owner_id uuid DEFAULT NULL
)
RETURNS TABLE(
    id bigint,
    role text,
    intent text,
    content text,
    created_at timestamp with time zone,
    similarity float
)
LANGUAGE sql STABLE AS $$
    SELECT
        id,
        role,
        intent,
        content,
        created_at,
        1 - (embedding <=> query_embedding) AS similarity
    FROM conversations
    WHERE embedding IS NOT NULL
      AND (cardinality(exclude_ids) = 0 OR NOT (id = ANY(exclude_ids)))
      AND (owner_id IS NULL OR conversations.owner_id = owner_id)
      AND 1 - (embedding <=> query_embedding) > match_threshold
    ORDER BY embedding <=> query_embedding
    LIMIT match_count;
$$;
