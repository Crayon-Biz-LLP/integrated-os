-- ============================================================
-- Migration 101: Drop dead schema (tables + RPCs superseded by
-- the graph + on-demand context pipeline)
--
-- Verified dead (2026-08-15 analysis — zero references in Python,
-- RPCs, triggers, or inbound FKs; checked against live schema):
--
--   - entity_briefs                  — pre-baked entity snapshots; superseded
--                                      by graph_nodes.metadata.enrichment and
--                                      on-demand context assembly (db/74).
--                                      22 rows, no readers since 07-23.
--   - project_organizations          — legacy pre-graph projects<->orgs join;
--                                      superseded by tasks.organization_id ->
--                                      graph_nodes(id) (db/75). 16 rows.
--   - retrieval_config               — old retrieval settings table; replaced
--                                      by core/retrieval/config.py. 5 rows,
--                                      grants already revoked (unreadable).
--   - retrieval_triples              — knowledge-triple storage from the old
--                                      retrieval system; superseded by graph
--                                      edges. 0 rows, no writers.
--   - retrieval_passage_triple_links — join of passages to the above; 0 rows.
--
--   - match_canonical_pages(jsonb,double precision,integer)
--   - match_logs(vector,double precision,integer)
--       RPCs defined in db/rpcs.sql, never called from Python
--       (live alternatives: direct canonical_pages reads, match_graph_nodes,
--       match_raw_dumps, match_memories_hybrid).
--
-- cleanup_expired_clarifications was ALREADY dropped by db/99 — not re-dropped.
-- trg_cascade_memory_delete is recreated WITHOUT the dead retrieval_triples
-- cleanup line; the memories AFTER-DELETE trigger stays attached.
-- ============================================================

-- 1. Drop the five dead tables (verified: no inbound FKs → no CASCADE needed)
DROP TABLE IF EXISTS public.retrieval_passage_triple_links;
DROP TABLE IF EXISTS public.retrieval_triples;
DROP TABLE IF EXISTS public.entity_briefs;
DROP TABLE IF EXISTS public.project_organizations;
DROP TABLE IF EXISTS public.retrieval_config;

-- 2. Recreate the memory-delete cascade without the dead triple cleanup.
CREATE OR REPLACE FUNCTION public.trg_cascade_memory_delete()
RETURNS TRIGGER AS $$
BEGIN
    -- 1. Remove bundle links by memory_id
    DELETE FROM public.retrieval_memory_bundle_links WHERE memory_id = OLD.id;

    -- 2. Remove passages (cascades to retrieval_passage_phrase_links via FK)
    DELETE FROM public.retrieval_passages WHERE source_type = 'memory' AND source_id = OLD.id::text;

    -- 3. Clean up index tracking rows
    DELETE FROM public.retrieval_index_runs WHERE source_type = 'memory' AND source_id = OLD.id::text;

    RETURN OLD;
END;
$$ LANGUAGE plpgsql;

-- 3. Drop unused RPCs (exact signatures from pg_proc)
DROP FUNCTION IF EXISTS public.match_canonical_pages(jsonb, double precision, integer);
DROP FUNCTION IF EXISTS public.match_logs(vector, double precision, integer);

-- 4. Verify: each should return 0
SELECT 'dead_tables_remaining' AS check_name, count(*) AS remaining
FROM pg_tables
WHERE schemaname = 'public'
  AND tablename IN ('entity_briefs', 'project_organizations', 'retrieval_config',
                    'retrieval_triples', 'retrieval_passage_triple_links');

SELECT 'dead_rpcs_remaining' AS check_name, count(*) AS remaining
FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
WHERE n.nspname = 'public'
  AND p.proname IN ('match_canonical_pages', 'match_logs');
