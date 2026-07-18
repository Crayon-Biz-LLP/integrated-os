-- db/36_missing_service_role_grants.sql
-- Fix 403 Forbidden on classifier_corrections and other tables with RLS but no GRANTs
-- Root cause: PostgREST needs explicit table-level GRANTs even for service_role
-- (which bypasses RLS). Without GRANTs, PostgREST returns 403 before it reaches RLS.

-- classifier_corrections (used by feedback_loop.py for LEARNED CORRECTIONS in classify prompt)
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE public.classifier_corrections TO service_role;
GRANT USAGE ON SEQUENCE public.classifier_corrections_id_seq TO service_role;

-- pending_graph_clarifications (used by clarifier.py for HITL clarification loops)
-- Check if table exists first
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_schema = 'public' AND table_name = 'pending_graph_clarifications') THEN
        EXECUTE 'GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE public.pending_graph_clarifications TO service_role';
    END IF;
END
$$;

-- model_registry (used by pattern learning for tracking LLM model versions)
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_schema = 'public' AND table_name = 'model_registry') THEN
        EXECUTE 'GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE public.model_registry TO service_role';
    END IF;
END
$$;
