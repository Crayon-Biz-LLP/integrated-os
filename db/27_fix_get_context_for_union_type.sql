-- 27_fix_get_context_for_union_type.sql
-- Fix UNION type mismatch in get_context_for() RPC
-- Column 7 (source_ref): NULL::uuid vs ge.source_ref (text) → error 42804
-- Root cause: source_ref in graph_edges is text, but the anchor cast it as uuid

BEGIN;

-- Drop the second overload (no STABLE, missing archived=false filter)
DROP FUNCTION IF EXISTS public.get_context_for(
    p_entity_label text,
    p_intent text,
    p_max_depth integer,
    p_as_of timestamp with time zone
);

-- Re-create the STABLE overload with NULL::text instead of NULL::uuid
DROP FUNCTION IF EXISTS public.get_context_for(
    p_entity_label text,
    p_intent text,
    p_as_of timestamp with time zone,
    p_max_depth integer
);

CREATE OR REPLACE FUNCTION public.get_context_for(
    p_entity_label text,
    p_intent text,
    p_as_of timestamp with time zone DEFAULT now(),
    p_max_depth integer DEFAULT 2
)
 RETURNS TABLE(
    node_id uuid,
    label text,
    node_type text,
    edge_verb text,
    depth integer,
    valid_from timestamp with time zone,
    source_ref text,
    epistemic_status text,
    reference_count integer,
    last_referenced_at timestamp with time zone
)
 LANGUAGE sql
 STABLE
AS $function$
WITH RECURSIVE context(node_id, label, node_type, edge_verb, depth, valid_from, source_ref, epistemic_status, reference_count, last_referenced_at) AS (
    -- Anchor
    SELECT gn.id AS node_id, gn.label, gn.type AS node_type, 
           NULL::text AS edge_verb, 0 AS depth, 
           NULL::timestamptz AS valid_from, NULL::text AS source_ref, 
           gn.epistemic_status, gn.reference_count, gn.last_referenced_at
    FROM graph_nodes gn
    WHERE gn.label ILIKE p_entity_label
    UNION ALL
    -- Bidirectional recursive step
    SELECT 
        neighbor.id, neighbor.label, neighbor.type, 
        ge.relationship, context.depth + 1, 
        ge.valid_from, ge.source_ref, 
        neighbor.epistemic_status, neighbor.reference_count, 
        neighbor.last_referenced_at
    FROM context
    JOIN graph_edges ge 
      ON ge.source_node_id = context.node_id 
      OR ge.target_node_id = context.node_id
    JOIN graph_nodes neighbor 
      ON neighbor.id = CASE 
           WHEN ge.source_node_id = context.node_id THEN ge.target_node_id
           ELSE ge.source_node_id
         END
    WHERE context.depth < p_max_depth
      AND ge.valid_from <= p_as_of
      AND (ge.valid_until IS NULL OR ge.valid_until >= p_as_of)
      AND ge.archived = false
      AND CASE p_intent
            WHEN 'blockers' THEN ge.relationship IN ('BLOCKS', 'DEPENDS_ON')
            WHEN 'people'   THEN ge.relationship IN ('WORKS_ON', 'WORKS_AT', 'MET_WITH', 'LEADS', 'INVOLVES')
            WHEN 'history'  THEN ge.relationship NOT IN ('SPOUSE_OF', 'FAMILY_OF')
            WHEN 'summary'  THEN TRUE
            ELSE TRUE
          END
)
SELECT * FROM context ORDER BY depth, node_type;
$function$;

COMMIT;
