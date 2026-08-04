-- Migration 74: Graph-Node Enrichment Consolidation
-- ============================================================================
-- Moves person/organization enrichment (role, strategic_weight,
-- organization_name, last_interaction_date, is_active, org_type, description,
-- parent_organization_id) INTO graph_nodes.metadata.enrichment so the graph
-- node is the SINGLE source of truth for entity identity + enrichment.
--
-- The domain tables (people, organizations) remain as trigger-maintained
-- mirrors (migration 47) — identity anchors for legacy bigint references
-- (messages.linked_person_id, memories.people_id) and FK anchors for
-- tasks/projects.organization_id. Application code no longer reads them for
-- enrichment; the drop/view decision happens after a soak period.
--
-- Idempotent: safe to re-run. Skips nodes that already carry enrichment.
-- ============================================================================

-- ── 1. Backfill enrichment for PERSON graph nodes ──────────────────────────
-- Deterministic per-node match: prefer metadata.people_id, then db_record_id,
-- then graph_node_id back-link (DISTINCT ON prevents double-matching).
WITH matched AS (
    SELECT DISTINCT ON (gn.id)
           gn.id AS node_id, p.*
    FROM graph_nodes gn
    JOIN people p ON
           (gn.metadata->>'people_id')::bigint = p.id
        OR gn.db_record_id = p.id::text
        OR gn.id = p.graph_node_id
    WHERE gn.type = 'person'
      AND gn.is_current = TRUE
      AND p.is_current = TRUE
      AND p.deleted_at IS NULL
    ORDER BY gn.id,
        CASE WHEN (gn.metadata->>'people_id')::bigint = p.id THEN 0
             WHEN gn.db_record_id = p.id::text THEN 1
             ELSE 2 END
)
UPDATE graph_nodes gn
SET metadata = jsonb_set(
    COALESCE(gn.metadata, '{}'::jsonb),
    '{enrichment}',
    jsonb_build_object(
        'role',                  p.role,
        'strategic_weight',      COALESCE(p.strategic_weight, 5),
        'organization_name',     p.organization_name,
        'last_interaction_date', p.last_interaction_date,
        'is_active',             TRUE,
        'enriched_at',           COALESCE(p.enriched_at, now())::text
    )
)
FROM matched p
WHERE gn.id = p.node_id
  AND NOT (COALESCE(gn.metadata, '{}'::jsonb) ? 'enrichment');

-- ── 2. Backfill enrichment for ORGANIZATION graph nodes ────────────────────
WITH matched AS (
    SELECT DISTINCT ON (gn.id)
           gn.id AS node_id, o.*
    FROM graph_nodes gn
    JOIN organizations o ON
           (gn.metadata->>'organization_id')::uuid = o.id
        OR gn.db_record_id = o.id::text
        OR gn.id = o.graph_node_id
    WHERE gn.type = 'organization'
      AND gn.is_current = TRUE
      AND o.is_active = TRUE
    ORDER BY gn.id,
        CASE WHEN (gn.metadata->>'organization_id')::uuid = o.id THEN 0
             WHEN gn.db_record_id = o.id::text THEN 1
             ELSE 2 END
)
UPDATE graph_nodes gn
SET metadata = jsonb_set(
    COALESCE(gn.metadata, '{}'::jsonb),
    '{enrichment}',
    jsonb_build_object(
        'is_active',              TRUE,
        'org_type',               o.org_type,
        'description',            o.description,
        'parent_organization_id', o.parent_organization_id::text,
        'org_created_at',         o.created_at::text
    )
)
FROM matched o
WHERE gn.id = o.node_id
  AND NOT (COALESCE(gn.metadata, '{}'::jsonb) ? 'enrichment');

-- ── 3. Reconciliation report ────────────────────────────────────────────────
-- Run anytime (e.g. SELECT * FROM public.entity_consolidation_report();) to
-- see residual drift between the graph and the domain mirrors.
CREATE OR REPLACE FUNCTION public.entity_consolidation_report()
RETURNS TABLE(category TEXT, label TEXT, detail TEXT) AS $$
    SELECT 'person_without_node', p.name,
           'people row (id=' || p.id || ') has no live person graph node'
    FROM people p
    WHERE p.is_current AND p.deleted_at IS NULL
      AND NOT EXISTS (
        SELECT 1 FROM graph_nodes gn
        WHERE gn.type = 'person' AND gn.is_current
          AND (gn.id = p.graph_node_id
               OR gn.db_record_id = p.id::text
               OR (gn.metadata->>'people_id')::bigint = p.id)
      )
    UNION ALL
    SELECT 'org_without_node', o.name,
           'organizations row (id=' || o.id || ') has no live org graph node'
    FROM organizations o
    WHERE o.is_active
      AND NOT EXISTS (
        SELECT 1 FROM graph_nodes gn
        WHERE gn.type = 'organization' AND gn.is_current
          AND (gn.id = o.graph_node_id
               OR gn.db_record_id = o.id::text
               OR (gn.metadata->>'organization_id')::uuid = o.id)
      )
    UNION ALL
    SELECT 'node_without_person', gn.label,
           'live person graph node has no people mirror row'
    FROM graph_nodes gn
    WHERE gn.type = 'person' AND gn.is_current
      AND NOT EXISTS (
        SELECT 1 FROM people p
        WHERE p.is_current AND p.deleted_at IS NULL
          AND (gn.id = p.graph_node_id
               OR gn.db_record_id = p.id::text
               OR (gn.metadata->>'people_id')::bigint = p.id)
      )
    UNION ALL
    SELECT 'node_without_org', gn.label,
           'live org graph node has no organizations mirror row'
    FROM graph_nodes gn
    WHERE gn.type = 'organization' AND gn.is_current
      AND NOT EXISTS (
        SELECT 1 FROM organizations o
        WHERE o.is_active
          AND (gn.id = o.graph_node_id
               OR gn.db_record_id = o.id::text
               OR (gn.metadata->>'organization_id')::uuid = o.id)
      )
$$ LANGUAGE sql STABLE;
