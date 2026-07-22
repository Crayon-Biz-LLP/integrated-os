-- db/44_retrieval_service_role_grants.sql
-- Grant service_role permissions on retrieval index tables.
-- Without these, the memory deletion trigger fails with permission denied
-- on retrieval_triples when trying to cascade-delete.

DO $$
BEGIN
    -- retrieval_passages (used for associative retrieval)
    IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_schema = 'public' AND table_name = 'retrieval_passages') THEN
        EXECUTE 'GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE public.retrieval_passages TO service_role';
    END IF;

    -- retrieval_phrase_nodes (used for associative retrieval)
    IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_schema = 'public' AND table_name = 'retrieval_phrase_nodes') THEN
        EXECUTE 'GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE public.retrieval_phrase_nodes TO service_role';
    END IF;

    -- retrieval_passage_phrase_links (used for associative retrieval)
    IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_schema = 'public' AND table_name = 'retrieval_passage_phrase_links') THEN
        EXECUTE 'GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE public.retrieval_passage_phrase_links TO service_role';
    END IF;

    -- retrieval_node_stats (used for PPR ranking)
    IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_schema = 'public' AND table_name = 'retrieval_node_stats') THEN
        EXECUTE 'GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE public.retrieval_node_stats TO service_role';
    END IF;

    -- retrieval_alias_edges (used for synonym bridge)
    IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_schema = 'public' AND table_name = 'retrieval_alias_edges') THEN
        EXECUTE 'GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE public.retrieval_alias_edges TO service_role';
    END IF;

    -- retrieval_memory_bundle_links (used for memory bundling)
    IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_schema = 'public' AND table_name = 'retrieval_memory_bundle_links') THEN
        EXECUTE 'GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE public.retrieval_memory_bundle_links TO service_role';
    END IF;

    -- retrieval_index_runs (used for checkpoint/resume)
    IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_schema = 'public' AND table_name = 'retrieval_index_runs') THEN
        EXECUTE 'GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE public.retrieval_index_runs TO service_role';
    END IF;

    -- retrieval_triples (the one causing the permission denied error)
    IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_schema = 'public' AND table_name = 'retrieval_triples') THEN
        EXECUTE 'GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE public.retrieval_triples TO service_role';
    END IF;
END
$$;

-- Also grant usage on sequences for any SERIAL columns in these tables
GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO service_role;
