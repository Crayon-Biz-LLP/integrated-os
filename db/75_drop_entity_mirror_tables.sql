-- ============================================================================
-- Migration 75: Full removal of the people / organizations mirror tables
-- ============================================================================
-- GOAL: graph_nodes becomes the ONLY home of person/org identity + enrichment.
--       people / organizations are dropped after every reference is repointed.
--
-- PRECONDITION (verified by scripts/remove_entity_tables.py --dry-run):
--   * Every live person/org row has graph_node_id (159/159, 43/43, plus all
--     archived rows that are still referenced).
--   * 0 dangling refs: every messages.linked_person_id, memories people_id,
--     tasks/projects/project_organizations organization_id maps to a node.
--
-- WHAT THIS DOES (in order):
--   1. Pre-check: enumerate every FK referencing people/organizations and RAISE
--      if anything unexpected exists (defense-in-depth — nothing missed).
--   1c. DROP the legacy domain-sync triggers + functions (migration 47). They
--      exist ONLY to maintain the people/organizations mirrors that THIS
--      migration removes — post-75 they are dead and actively harmful:
--        * every graph_nodes UPDATE fires the temporal trigger (migration 31),
--          which INSERTs an archived copy; the AFTER-INSERT sync trigger then
--          casts db_record_id::bigint, which now holds a self-canonical UUID
--          -> 22P02 "invalid input syntax for type bigint" (the exact error
--          this migration hit on first run).
--        * they INSERT/UPDATE people/organizations, which no longer exist.
--   2. Node metadata self-canonicalization: for person/org nodes, rewrite
--      metadata.people_id / metadata.organization_id / db_record_id to the
--      node's OWN id, so every `metadata->>people_id` filter and API id field
--      resolves to the node UUID going forward.
--   3. memories.metadata.people_id / organization_id -> node UUID (JSONB).
--   4. memories.organization_id column -> node UUID.
--   5. canonical_pages.organization_id -> node UUID.
--   6. messages.linked_person_id INT8 -> UUID (drop column + rename pattern),
--      FK -> graph_nodes(id) ON DELETE SET NULL.
--   7. tasks.organization_id, projects.organization_id,
--      projects.migrated_to_organization_id, project_organizations.organization_id
--      -> node UUID, FKs re-pointed to graph_nodes(id).
--   8. Residual enrichment merged: people.enrichment_notes -> enrichment.notes;
--      organizations.parent_organization_id -> enrichment.parent_organization_id
--      (as the parent NODE id).
--   9. DROP TABLE people, organizations. Replace db/74's
--      entity_consolidation_report() (reads the dropped tables) with a
--      graph-self-consistency report.
--
-- IDEMPOTENT: every step is guarded; safe to re-run. Run in the Supabase SQL
-- editor (single transaction).
--
-- RECOVERY: if this script errors mid-run, the SQL editor may leave the
-- triggers disabled in step 1d (it commits statements individually). Simply
-- re-run the WHOLE script — step 1d re-disables (no-op), and step 10
-- re-enables. If you want to re-enable manually after an aborted run:
--     ALTER TABLE graph_nodes ENABLE TRIGGER USER;
--     ALTER TABLE memories ENABLE TRIGGER USER;
--     ALTER TABLE canonical_pages ENABLE TRIGGER USER;
--     ALTER TABLE tasks ENABLE TRIGGER USER;
--     ALTER TABLE projects ENABLE TRIGGER USER;
-- ============================================================================

BEGIN;

-- ── 0. Guard: if this migration is already fully applied (both tables gone),
--        stop cleanly instead of crashing on a missing people/organizations.
--        NOTE: this MUST be the first executable statement — everything below
--        assumes the mirror tables exist. -------------------------------------
DO $$
BEGIN
    IF to_regclass('public.people') IS NULL AND to_regclass('public.organizations') IS NULL THEN
        RAISE EXCEPTION 'Migration 75 already applied — people/organizations tables are gone. Nothing to run.';
    END IF;
END $$;

-- ── 1. Pre-check: enumerate every FK that references people/organizations. --
--        If any UNEXPECTED constraint exists, abort so nothing is missed.    --
DO $$
DECLARE
    fk_list text;
BEGIN
    IF to_regclass('public.people') IS NOT NULL OR to_regclass('public.organizations') IS NOT NULL THEN
        SELECT string_agg(format('%I.%I -> %I.%I.%I', tc.table_schema, tc.table_name,
                                 ccu.table_schema, ccu.table_name, kcu.column_name), E'\n  ')
        INTO fk_list
        FROM information_schema.table_constraints tc
        JOIN information_schema.key_column_usage kcu
          ON tc.constraint_name = kcu.constraint_name AND tc.table_schema = kcu.table_schema
        JOIN information_schema.constraint_column_usage ccu
          ON tc.constraint_name = ccu.constraint_name AND tc.table_schema = ccu.table_schema
        WHERE tc.constraint_type = 'FOREIGN KEY'
          AND ccu.table_name IN ('people', 'organizations');

        IF fk_list IS NOT NULL AND fk_list <> '' THEN
            RAISE NOTICE 'FKs referencing people/organizations to be dropped/repointed:%', E'\n  ' || fk_list;
        END IF;
    END IF;
END $$;

-- ── 1b. Drop EVERY FK that references people/organizations (defensive: covers
--        auto-named constraints whose exact name we may not know). -----------
DO $$
DECLARE r record;
BEGIN
    FOR r IN
        SELECT conrelid::regclass::text AS tbl, conname
        FROM pg_constraint
        WHERE contype = 'f'
          AND confrelid IN ('public.people'::regclass, 'public.organizations'::regclass)
    LOOP
        EXECUTE format('ALTER TABLE %s DROP CONSTRAINT %I', r.tbl, r.conname);
    END LOOP;
END $$;

-- ── 1c. Drop the legacy domain-sync triggers + functions (migration 47). ----
--        They maintain the people/organizations mirrors that this migration
--        removes. If left in place they crash every person/org graph_nodes
--        write AFTER this migration (they reference the dropped tables) and
--        they already crash the migration itself: the temporal trigger's
--        archived INSERT fires sync_domain_row_from_graph_node(), which casts
--        db_record_id::bigint — a UUID after self-canonicalization → 22P02.
DROP TRIGGER IF EXISTS trg_graph_node_insert_sync_domain ON graph_nodes;
DROP TRIGGER IF EXISTS trg_graph_node_delete_archive_domain ON graph_nodes;
DROP TRIGGER IF EXISTS trg_graph_node_soft_delete_archive_domain ON graph_nodes;
DROP TRIGGER IF EXISTS trg_graph_node_type_change_migrate_domain ON graph_nodes;
DROP FUNCTION IF EXISTS sync_domain_row_from_graph_node();
DROP FUNCTION IF EXISTS archive_domain_row_on_graph_node_remove();
DROP FUNCTION IF EXISTS migrate_domain_on_type_change();
DROP FUNCTION IF EXISTS resolve_domain_id(text, text);

-- ── 1d. Disable ALL remaining user triggers for the duration of this --------
--        migration, then re-enable before COMMIT (see step 10).
--        WHY: the bulk UPDATEs below fire the temporal lineage triggers
--        (migration 31), which INSERT archived copies of every touched row.
--        Post-74 those archived copies carry a UUID db_record_id / UUID
--        metadata ids, and any leftover cast on them (or any future trigger
--        added to these tables) would crash the migration with 22P02. A data
--        migration must not fire per-row application triggers — disable them,
--        migrate, re-enable.
--        Also prevents the migration from creating hundreds of junk archived
--        copies of person/org nodes as a side effect.
ALTER TABLE graph_nodes DISABLE TRIGGER USER;
ALTER TABLE memories DISABLE TRIGGER USER;
ALTER TABLE canonical_pages DISABLE TRIGGER USER;
ALTER TABLE tasks DISABLE TRIGGER USER;
ALTER TABLE projects DISABLE TRIGGER USER;

-- ── 2. Node metadata self-canonicalization ---------------------------------
--        person/org nodes: metadata.people_id / organization_id AND the
--        db_record_id COLUMN become the node's own id (legacy ids gone).
UPDATE graph_nodes
SET metadata = metadata
    || jsonb_build_object(
         CASE WHEN type = 'person' THEN 'people_id' ELSE 'organization_id' END, id::text
       )
WHERE type IN ('person', 'organization')
  AND is_current IS NOT FALSE;   -- touch current nodes; archived stay as trace

UPDATE graph_nodes
SET db_record_id = id::text
WHERE type IN ('person', 'organization')
  AND is_current IS NOT FALSE;

-- ── 3. memories.metadata.people_id / organization_id -> node UUID ----------
--        Rewrite known ids; drop any key whose id has no matching node
--        (defensive — dry-run proves there are none today).
-- NOTE: the `::bigint` casts below are guarded with a numeric regex so a
-- non-numeric (e.g. node-UUID) people_id can never crash the migration with
-- 22P02 — rows that fail the regex are simply not matched here.
UPDATE memories
SET metadata = jsonb_set(metadata, '{people_id}', to_jsonb(p.graph_node_id::text))
FROM people p
WHERE p.graph_node_id IS NOT NULL
  AND metadata ? 'people_id'
  AND CASE 
        WHEN (metadata ->> 'people_id') ~ '^[0-9]+$' 
        THEN (metadata ->> 'people_id')::bigint 
        ELSE NULL 
      END = p.id;

UPDATE memories
SET metadata = metadata - 'people_id'
WHERE metadata ? 'people_id'
  AND NOT EXISTS (
    SELECT 1 FROM people p
    WHERE CASE 
            WHEN (metadata ->> 'people_id') ~ '^[0-9]+$' 
            THEN (metadata ->> 'people_id')::bigint 
            ELSE NULL 
          END = p.id AND p.graph_node_id IS NOT NULL
  );

UPDATE memories
SET metadata = jsonb_set(metadata, '{organization_id}', to_jsonb(o.graph_node_id::text))
FROM organizations o
WHERE o.graph_node_id IS NOT NULL
  AND metadata ? 'organization_id'
  AND (metadata ->> 'organization_id') = o.id::text;

UPDATE memories
SET metadata = metadata - 'organization_id'
WHERE metadata ? 'organization_id'
  AND NOT EXISTS (
    SELECT 1 FROM organizations o
    WHERE (metadata ->> 'organization_id') = o.id::text AND o.graph_node_id IS NOT NULL
  );

-- ── 4. memories.organization_id column -> node UUID ------------------------
UPDATE memories m
SET organization_id = o.graph_node_id
FROM organizations o
WHERE o.graph_node_id IS NOT NULL AND m.organization_id = o.id;

-- ── 5. canonical_pages.organization_id -> node UUID -------------------------
UPDATE canonical_pages cp
SET organization_id = o.graph_node_id
FROM organizations o
WHERE o.graph_node_id IS NOT NULL AND cp.organization_id = o.id;

-- ── 6. messages.linked_person_id INT8 -> UUID -> graph_nodes FK -------------
ALTER TABLE messages ADD COLUMN IF NOT EXISTS linked_node_id uuid;
UPDATE messages m
SET linked_node_id = p.graph_node_id
FROM people p
WHERE p.graph_node_id IS NOT NULL AND m.linked_person_id = p.id;

ALTER TABLE messages DROP CONSTRAINT IF EXISTS messages_linked_person_id_fkey;

ALTER TABLE messages ALTER COLUMN linked_person_id DROP DEFAULT;
ALTER TABLE messages ALTER COLUMN linked_person_id DROP NOT NULL;
ALTER TABLE messages DROP COLUMN IF EXISTS linked_person_id;
ALTER TABLE messages RENAME COLUMN linked_node_id TO linked_person_id;
-- messages (guarded: Postgres has no ADD CONSTRAINT IF NOT EXISTS, so a
-- re-run after a partial apply must not fail on an already-added constraint)
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'messages_linked_person_id_fkey') THEN
        ALTER TABLE messages
          ADD CONSTRAINT messages_linked_person_id_fkey
          FOREIGN KEY (linked_person_id) REFERENCES graph_nodes(id) ON DELETE SET NULL;
    END IF;
END $$;

-- ── 7. tasks / projects / project_organizations -> node UUID + FK repoint ---
--        (All three columns are already UUID — only values + FK target change.)

-- tasks
UPDATE tasks t SET organization_id = o.graph_node_id
FROM organizations o WHERE o.graph_node_id IS NOT NULL AND t.organization_id = o.id;
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'tasks_organization_id_fkey') THEN
        ALTER TABLE tasks
          ADD CONSTRAINT tasks_organization_id_fkey
          FOREIGN KEY (organization_id) REFERENCES graph_nodes(id) ON DELETE SET NULL;
    END IF;
END $$;

-- projects
UPDATE projects p SET organization_id = o.graph_node_id
FROM organizations o WHERE o.graph_node_id IS NOT NULL AND p.organization_id = o.id;
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_projects_organization') THEN
        ALTER TABLE projects
          ADD CONSTRAINT fk_projects_organization
          FOREIGN KEY (organization_id) REFERENCES graph_nodes(id) ON DELETE SET NULL;
    END IF;
END $$;

-- migrated_to_organization_id only exists if migration 05's project-org expansion
-- was fully applied (it is absent in prod) — guard it.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_name = 'projects' AND column_name = 'migrated_to_organization_id') THEN
        UPDATE projects p SET migrated_to_organization_id = o.graph_node_id
        FROM organizations o WHERE o.graph_node_id IS NOT NULL AND p.migrated_to_organization_id = o.id;
        IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'projects_migrated_to_organization_id_fkey') THEN
            ALTER TABLE projects
              ADD CONSTRAINT projects_migrated_to_organization_id_fkey
              FOREIGN KEY (migrated_to_organization_id) REFERENCES graph_nodes(id) ON DELETE SET NULL;
        END IF;
    END IF;
END $$;

-- project_organizations
UPDATE project_organizations po SET organization_id = o.graph_node_id
FROM organizations o WHERE o.graph_node_id IS NOT NULL AND po.organization_id = o.id;
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'project_organizations_organization_id_fkey') THEN
        ALTER TABLE project_organizations
          ADD CONSTRAINT project_organizations_organization_id_fkey
          FOREIGN KEY (organization_id) REFERENCES graph_nodes(id) ON DELETE CASCADE;
    END IF;
END $$;

-- ── 8. Residual enrichment merges -------------------------------------------
--        people.enrichment_notes -> enrichment.notes
UPDATE graph_nodes g
SET metadata = jsonb_set(metadata, '{enrichment,notes}', to_jsonb(p.enrichment_notes::text), true)
FROM people p
WHERE g.type = 'person' AND p.graph_node_id = g.id AND p.enrichment_notes IS NOT NULL;

--        organizations.parent_organization_id -> enrichment.parent_organization_id (parent NODE id)
UPDATE graph_nodes g
SET metadata = jsonb_set(metadata, '{enrichment,parent_organization_id}', to_jsonb(par.graph_node_id::text), true)
FROM organizations o
JOIN organizations par ON par.id = o.parent_organization_id AND par.graph_node_id IS NOT NULL
WHERE g.type = 'organization' AND o.graph_node_id = g.id AND o.parent_organization_id IS NOT NULL;

-- ── 9. Drop the mirror tables -----------------------------------------------
--        (people.supersedes_id self-FK and organizations.parent_organization_id
--         self-FK die with their tables. Indexes, sequences, grants die too.)
DROP TABLE IF EXISTS people CASCADE;
DROP TABLE IF EXISTS organizations CASCADE;

-- ── Replace db/74's entity_consolidation_report() (it read the dropped
--    tables) with a graph-self-consistency report. ----------------------------
DROP FUNCTION IF EXISTS public.entity_consolidation_report();

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

-- ── 10. Re-enable the triggers disabled in step 1d. -------------------------
--        (The migration-47 sync triggers are intentionally NOT recreated —
--         they are dead after this migration: they write to the dropped
--         people/organizations tables.)
ALTER TABLE graph_nodes ENABLE TRIGGER USER;
ALTER TABLE memories ENABLE TRIGGER USER;
ALTER TABLE canonical_pages ENABLE TRIGGER USER;
ALTER TABLE tasks ENABLE TRIGGER USER;
ALTER TABLE projects ENABLE TRIGGER USER;

COMMIT;
