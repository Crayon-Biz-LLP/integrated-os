-- ============================================================================
-- Migration 76: Aliases move onto the graph node
-- ============================================================================
-- GOAL: retire the person_aliases table; every alias lives on the node it
--       refers to (graph_nodes.metadata.aliases). One row per real-world
--       entity — the node carries its own alternate names.
--
-- WHY:  after migration 75 the graph node is the single source of truth for
--       identity. A node's aliases ("sunju", "my wife", "amma") belong ON that
--       node, so merge/rename keeps them consistent and retrieval can resolve
--       a mention to the node UUID directly.
--
-- WHAT THIS DOES (in order):
--   1. Pre-check: verify person_aliases exists and canonical_name values map
--      to live graph nodes (defense-in-depth).
--   2. Backfill: for every person_aliases row, find the person node whose
--      label matches canonical_name and append the alias to
--      metadata.aliases (dedup, lowercase) + metadata.alias_usage count.
--   3. Self-consistency: every remaining person_aliases row that could NOT be
--      mapped is reported (should be 0 in prod — verified by script).
--   4. DROP TABLE person_aliases (indexes/sequences/grants die with it).
--
-- IDEMPOTENT: every step is guarded; safe to re-run. Run in the Supabase SQL
-- editor (single transaction). Code-first deploy order, then paste this.
-- ============================================================================

BEGIN;

-- ── 0. Guard: table must still exist. ----------------------------------------
DO $$
BEGIN
    IF to_regclass('public.person_aliases') IS NULL THEN
        RAISE NOTICE 'Migration 76 already applied — person_aliases gone. Nothing to do.';
    END IF;
END $$;

-- ── 1. Build the alias→live-node mapping (canonical-chain aware). -----------
--        Resolution order for canonical_name → live person node:
--          a. exact live label match, OR
--          b. follow canonical_id from an archived node with that label (a
--             merged 'Mother' now lives as 'Amma'), OR
--          c. any archived node with that label (last resort — keeps the alias
--             attached so a future revive keeps working).
DO $$
DECLARE
    miss text;
BEGIN
    IF to_regclass('public.person_aliases') IS NOT NULL THEN
        SELECT string_agg(format('  %L -> %L (no live node, no canonical chain)', alias, canonical_name), E'\n')
        INTO miss
        FROM person_aliases pa
        WHERE NOT EXISTS (
            SELECT 1 FROM graph_nodes g
            WHERE g.type = 'person'
              AND g.is_current IS NOT FALSE
              AND lower(g.label) = lower(pa.canonical_name)
        )
        AND NOT EXISTS (
            -- follows canonical_id from an archived same-label node to a live one
            SELECT 1 FROM graph_nodes g_old
            JOIN graph_nodes g2 ON g2.id = g_old.canonical_id AND g2.is_current IS NOT FALSE
            WHERE g_old.type = 'person' AND lower(g_old.label) = lower(pa.canonical_name)
        )
        AND NOT EXISTS (
            SELECT 1 FROM graph_nodes g3
            WHERE g3.type = 'person' AND lower(g3.label) = lower(pa.canonical_name)
        );
        IF miss IS NOT NULL AND miss <> '' THEN
            RAISE NOTICE 'person_aliases rows with NO resolvable person node (will be skipped):%', E'\n' || miss;
        END IF;
    END IF;
END $$;

-- ── 2. Backfill metadata.aliases / metadata.alias_usage from person_aliases --
--        Alias rows resolve to a node via the 3-step chain above. Aliases are
--        stored lowercase for matching; the display name is the node's label.
WITH resolved AS (
    SELECT pa.id AS pa_id,
           lower(btrim(pa.alias)) AS alias,
           pa.resolution_count AS cnt,
           COALESCE(
               -- a. exact live label
               (SELECT g.id FROM graph_nodes g
                 WHERE g.type = 'person' AND g.is_current IS NOT FALSE
                   AND lower(g.label) = lower(pa.canonical_name) LIMIT 1),
               -- b. canonical_id chain from an archived same-label node
               (SELECT g2.id FROM graph_nodes g_old
                 JOIN graph_nodes g2 ON g2.id = g_old.canonical_id AND g2.is_current IS NOT FALSE
                 WHERE g_old.type = 'person' AND lower(g_old.label) = lower(pa.canonical_name) LIMIT 1),
               -- c. any archived same-label node
               (SELECT g3.id FROM graph_nodes g3
                 WHERE g3.type = 'person' AND lower(g3.label) = lower(pa.canonical_name) LIMIT 1)
           ) AS node_id,
           pa.canonical_name AS canonical_name
    FROM person_aliases pa
    WHERE btrim(pa.alias) <> ''
),
dedup AS (
    SELECT DISTINCT ON (node_id, alias)
           node_id, alias, cnt, canonical_name
    FROM resolved
    WHERE node_id IS NOT NULL
    ORDER BY node_id, alias, cnt DESC NULLS LAST
)
UPDATE graph_nodes g
SET metadata = jsonb_set(
        jsonb_set(
            COALESCE(g.metadata, '{}'::jsonb),
            '{aliases}',
            to_jsonb(
                (SELECT COALESCE(array_agg(DISTINCT a), ARRAY[]::text[])
                 FROM (
                     SELECT d.alias AS a FROM dedup d WHERE d.node_id = g.id
                     UNION
                     -- the canonical name itself becomes an alias when it differs
                     -- from the node's label (e.g. 'Mother' -> live node 'Amma')
                     SELECT lower(btrim(d.canonical_name)) AS a FROM dedup d
                       WHERE d.node_id = g.id
                         AND lower(btrim(d.canonical_name)) <> lower(g.label)
                         AND btrim(d.canonical_name) <> ''
                     UNION
                     SELECT x FROM jsonb_array_elements_text(
                         CASE WHEN COALESCE(g.metadata, '{}'::jsonb) ? 'aliases'
                              THEN COALESCE(g.metadata, '{}'::jsonb) -> 'aliases'
                              ELSE '[]'::jsonb END
                     ) AS t(x) WHERE x IS NOT NULL AND btrim(x) <> ''
                 ) s)
            ),
            true
        ),
        '{alias_usage}',
        (
            SELECT COALESCE(jsonb_object_agg(a, cnt), '{}'::jsonb)
            FROM (
                SELECT d.alias AS a, d.cnt AS cnt FROM dedup d WHERE d.node_id = g.id
            ) u
        ),
        true
    )
WHERE g.id IN (SELECT node_id FROM dedup);

-- ── 3. Self-consistency report of unmapped rows (should be empty in prod). ---
DO $$
DECLARE
    n int;
BEGIN
    IF to_regclass('public.person_aliases') IS NOT NULL THEN
        SELECT count(*) INTO n FROM person_aliases pa
        WHERE btrim(pa.alias) <> ''
          AND NOT EXISTS (SELECT 1 FROM graph_nodes g
             WHERE g.type = 'person' AND g.is_current IS NOT FALSE
               AND lower(g.label) = lower(pa.canonical_name))
          AND NOT EXISTS (SELECT 1 FROM graph_nodes g_old
             JOIN graph_nodes g2 ON g2.id = g_old.canonical_id AND g2.is_current IS NOT FALSE
             WHERE g_old.type = 'person' AND lower(g_old.label) = lower(pa.canonical_name))
          AND NOT EXISTS (SELECT 1 FROM graph_nodes g3
             WHERE g3.type = 'person' AND lower(g3.label) = lower(pa.canonical_name));
        IF n > 0 THEN
            RAISE NOTICE 'Migration 76: % person_aliases rows skipped (no resolvable node).', n;
        END IF;
    END IF;
END $$;

-- ── 4. Drop the mirror table. ------------------------------------------------
DROP TABLE IF EXISTS person_aliases;

-- ── 5. Extend the self-consistency report with alias coverage. ---------------
CREATE OR REPLACE FUNCTION public.entity_self_consistency_report()
RETURNS TABLE(check_name text, status text, detail bigint)
LANGUAGE plpgsql
AS $$
BEGIN
    -- People: nodes with enrichment, self-canonical id, dangling legacy refs
    RETURN QUERY SELECT 'person_nodes_total'::text, 'ok'::text, count(*)::bigint
        FROM graph_nodes WHERE type = 'person' AND is_current IS NOT FALSE;
    RETURN QUERY SELECT 'person_nodes_with_enrichment'::text, 'ok'::text, count(*)::bigint
        FROM graph_nodes WHERE type = 'person' AND is_current IS NOT FALSE
        AND metadata ? 'enrichment';
    RETURN QUERY SELECT 'person_nodes_self_canonical'::text, 'ok'::text, count(*)::bigint
        FROM graph_nodes WHERE type = 'person' AND is_current IS NOT FALSE
        AND metadata->>'people_id' = id::text;
    RETURN QUERY SELECT 'person_nodes_legacy_db_record'::text, 'ok'::text, count(*)::bigint
        FROM graph_nodes WHERE type = 'person' AND is_current IS NOT FALSE
        AND db_record_id = id::text;
    RETURN QUERY SELECT 'person_nodes_with_aliases'::text, 'ok'::text, count(*)::bigint
        FROM graph_nodes WHERE type = 'person' AND is_current IS NOT FALSE
        AND metadata ? 'aliases';
    RETURN QUERY SELECT 'person_alias_entries'::text, 'ok'::text, count(*)::bigint
        FROM graph_nodes g, jsonb_array_elements_text(
            CASE WHEN COALESCE(g.metadata, '{}'::jsonb) ? 'aliases'
                 THEN COALESCE(g.metadata, '{}'::jsonb) -> 'aliases'
                 ELSE '[]'::jsonb END) a
        WHERE g.type = 'person' AND g.is_current IS NOT FALSE;
    -- Orgs
    RETURN QUERY SELECT 'org_nodes_total'::text, 'ok'::text, count(*)::bigint
        FROM graph_nodes WHERE type = 'organization' AND is_current IS NOT FALSE;
    RETURN QUERY SELECT 'org_nodes_with_enrichment'::text, 'ok'::text, count(*)::bigint
        FROM graph_nodes WHERE type = 'organization' AND is_current IS NOT FALSE
        AND metadata ? 'enrichment';
    RETURN QUERY SELECT 'org_nodes_self_canonical'::text, 'ok'::text, count(*)::bigint
        FROM graph_nodes WHERE type = 'organization' AND is_current IS NOT FALSE
        AND metadata->>'organization_id' = id::text;
    -- Referential integrity (all should be 0)
    RETURN QUERY SELECT 'dangling_messages_linked_person'::text, 'ok'::text, count(*)::bigint
        FROM messages m WHERE m.linked_person_id IS NOT NULL
        AND NOT EXISTS (SELECT 1 FROM graph_nodes g WHERE g.id = m.linked_person_id);
    RETURN QUERY SELECT 'dangling_tasks_org'::text, 'ok'::text, count(*)::bigint
        FROM tasks t WHERE t.organization_id IS NOT NULL
        AND NOT EXISTS (SELECT 1 FROM graph_nodes g WHERE g.id = t.organization_id);
    RETURN QUERY SELECT 'dangling_projects_org'::text, 'ok'::text, count(*)::bigint
        FROM projects p WHERE p.organization_id IS NOT NULL
        AND NOT EXISTS (SELECT 1 FROM graph_nodes g WHERE g.id = p.organization_id);
    RETURN QUERY SELECT 'dangling_project_orgs_join'::text, 'ok'::text, count(*)::bigint
        FROM project_organizations po
        WHERE NOT EXISTS (SELECT 1 FROM graph_nodes g WHERE g.id = po.organization_id);
    RETURN QUERY SELECT 'dangling_memories_org'::text, 'ok'::text, count(*)::bigint
        FROM memories m WHERE m.organization_id IS NOT NULL
        AND NOT EXISTS (SELECT 1 FROM graph_nodes g WHERE g.id = m.organization_id);
END $$;

COMMIT;
