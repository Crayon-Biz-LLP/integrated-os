-- Migration: 70_rpc_get_associative_data
-- 
-- Consolidates steps 2+4+6 of the associative retrieval pipeline:
--   - Search phrase nodes (replaces search_phrase_nodes RPC + lexical fetch)
--   - Fetch retrieval_edges + alias_edges (replaces 2 HTTP round-trips)
--   - Aggregate to passages → memories (replaces 2-3 HTTP round-trips)
--
-- Returns a single-row composite payload with two JSONB arrays:
--   nodes: array of matched phrase nodes with passage/memory links
--   edges: array of graph edges for Python PPR engine
--
-- ⚠️ The p_tsquery parameter must be pre-sanitized by Python's _build_tsquery().
--    Do NOT pass raw user input — to_tsquery crashes on special characters.
--
-- Review history:
--   Review 1: Fixed JSONB type mismatch, timestamptz casting
--   Review 2: Fixed dangling CTE bug (edges were defined but never returned)
--   Review 3: Fixed payload duplication (50x redundant edges → single-row composite)
--   Review 4: Fixed JSON codec interaction (asyncpg auto-decodes JSONB)

DROP FUNCTION IF EXISTS rpc_get_associative_data(text, int);

CREATE FUNCTION rpc_get_associative_data(
    p_tsquery text,       -- Pre-sanitized by Python's _build_tsquery()
    p_limit int DEFAULT 50
)
RETURNS TABLE (
    nodes jsonb,          -- Array of matched phrase nodes with passage/memory links
    edges jsonb           -- Array of graph edges (from/to/weight) for Python PPR engine
)
LANGUAGE plpgsql STABLE
AS $$
BEGIN
    RETURN QUERY
    WITH matched_nodes AS (
        SELECT pn.id, pn.normalized_text, pn.display_text, pn.node_type,
               ts_rank(pn.search_vector, to_tsquery('simple', p_tsquery)) AS rank
        FROM retrieval_phrase_nodes pn
        WHERE pn.search_vector @@ to_tsquery('simple', p_tsquery)
        ORDER BY rank DESC
        LIMIT p_limit
    ),
    linked_passages AS (
        SELECT ppl.node_id,
               ARRAY_AGG(DISTINCT ppl.passage_id) AS passage_ids,
               ARRAY_AGG(DISTINCT mb.memory_id) AS memory_ids
        FROM retrieval_passage_phrase_links ppl
        LEFT JOIN retrieval_memory_bundle_links mb ON mb.passage_id = ppl.passage_id
        WHERE ppl.node_id IN (SELECT mn.id FROM matched_nodes mn)
        GROUP BY ppl.node_id
    ),
    node_payload AS (
        SELECT jsonb_agg(
            jsonb_build_object(
                'id', mn.id::text,
                'normalized_text', mn.normalized_text,
                'display_text', mn.display_text,
                'node_type', mn.node_type,
                'rank', mn.rank,
                'passage_ids', COALESCE(lp.passage_ids, '{}'),
                'memory_ids', COALESCE(lp.memory_ids, '{}')
            )
        ) AS nodes_json
        FROM matched_nodes mn
        LEFT JOIN linked_passages lp ON lp.node_id = mn.id
    ),
    edge_payload AS (
        SELECT COALESCE(
            jsonb_agg(
                jsonb_build_object('from', e.from_node_id, 'to', e.to_node_id, 'weight', e.weight)
            ),
            '[]'::jsonb
        ) AS edges_json
        FROM (
            SELECT from_node_id, to_node_id, weight
            FROM retrieval_edges
            WHERE from_node_id IN (SELECT id FROM matched_nodes)
               OR to_node_id IN (SELECT id FROM matched_nodes)

            UNION ALL

            SELECT from_node_id, to_node_id, weight
            FROM retrieval_alias_edges
            WHERE from_node_id IN (SELECT id FROM matched_nodes)
        ) e
    )
    SELECT
        COALESCE((SELECT nodes_json FROM node_payload), '[]'::jsonb),
        COALESCE((SELECT edges_json FROM edge_payload), '[]'::jsonb);
END;
$$;
