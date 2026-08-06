-- 82_rpc_owner_scoping_graph.sql — scope the two graph RPCs missed in the M3 sweep
--
-- Context: the M3 facade injects `owner_id` into every non-global RPC. Two data
-- RPCs called by core/pulse/graph.py were not migrated (db/80/81) and would now
-- fail loudly at runtime once tenant mode is on:
--
--   * match_graph_nodes           (graph.py:1001 — vector match over graph_nodes)
--   * get_most_connected_nodes    (graph.py:1061 — degree ranking)
--
-- get_most_connected_nodes was LANGUAGE sql — the exact class of function where
-- an unqualified `owner_id` filter would silently resolve to the COLUMN (always
-- true → cross-tenant leak). It is converted to plpgsql with the same p_owner
-- snapshot pattern used across db/80/81 (PL/pgSQL params are ambiguous against
-- columns on PG17, so the DECLARE snapshot is mandatory).
--
-- Pattern: `owner_id uuid DEFAULT NULL` (legacy calls keep working unscoped);
-- filter is table-qualified against the local p_owner snapshot.
--
-- Verified on copy DB: single overload each; isolation smoke with two owners.

-- ── match_graph_nodes: vector similarity over graph_nodes ─────────────────────
DROP FUNCTION IF EXISTS public.match_graph_nodes(
    query_embedding vector,
    match_threshold double precision,
    match_count integer
);

CREATE OR REPLACE FUNCTION public.match_graph_nodes(
    query_embedding vector,
    match_threshold double precision,
    match_count integer,
    owner_id uuid DEFAULT NULL
)
 RETURNS TABLE(id uuid, label text, type text, metadata jsonb, similarity double precision)
 LANGUAGE plpgsql
AS $function$
DECLARE
    p_owner uuid := owner_id;
BEGIN
  RETURN QUERY
  SELECT
    n.id,
    n.label,
    n.type,
    n.metadata,
    1 - (n.embedding <=> query_embedding) AS similarity
  FROM graph_nodes n
  WHERE n.embedding IS NOT NULL
    AND 1 - (n.embedding <=> query_embedding) > match_threshold
    AND (p_owner IS NULL OR n.owner_id = p_owner)
  ORDER BY similarity DESC
  LIMIT match_count;
END;
$function$;

-- ── get_most_connected_nodes: degree ranking (was LANGUAGE sql — silent-leak
--    risk; converted to plpgsql with explicit owner scoping) ───────────────────
DROP FUNCTION IF EXISTS public.get_most_connected_nodes(integer);

CREATE OR REPLACE FUNCTION public.get_most_connected_nodes(
    limit_count integer DEFAULT 3,
    owner_id uuid DEFAULT NULL
)
 RETURNS TABLE(node_id uuid, label text, type text, edge_count bigint)
 LANGUAGE plpgsql
AS $function$
DECLARE
    p_owner uuid := owner_id;
BEGIN
    RETURN QUERY
    SELECT
        n.id as node_id,
        n.label,
        n.type,
        COUNT(e.id) as edge_count
    FROM graph_nodes n
    LEFT JOIN graph_edges e
      ON (n.id = e.source_node_id OR n.id = e.target_node_id)
     AND (p_owner IS NULL OR e.owner_id = p_owner)
    WHERE n.type IN ('person', 'project', 'concept')
      AND (p_owner IS NULL OR n.owner_id = p_owner)
    GROUP BY n.id, n.label, n.type
    ORDER BY edge_count DESC
    LIMIT limit_count;
END;
$function$;

