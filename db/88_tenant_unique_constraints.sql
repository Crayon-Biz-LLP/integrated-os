-- 88_tenant_unique_constraints.sql
-- M6/M7 hardening: make the content-level unique constraints PER-OWNER.
--
-- Before this migration the following uniques were GLOBAL (no owner_id), so
-- tenant #2's inserts could either collide with tenant #1's rows or — worse,
-- via upsert ON CONFLICT — silently UPDATE (steal) tenant #1's rows. All
-- rows in these tables were backfilled with owner_id by
-- scripts/migrate_danny_to_tenant1.py, so creating (owner_id, ...)
-- composites is safe: within a single owner the old uniqueness still holds.
--
-- Two of these were created as table CONSTRAINTS on live
-- (unique_graph_nodes_normalized_label_type, retrieval_phrase_nodes_normalized_text_key),
-- the rest as standalone UNIQUE INDEXes. `DROP INDEX` fails with 2BP01 on a
-- constraint-backed index, so the drop loop below inspects pg_constraint
-- first and uses ALTER TABLE ... DROP CONSTRAINT when needed.
--
-- Re-run safety: idempotent — the drop loop skips missing objects and the
-- CREATE statements are plain CREATE UNIQUE INDEX.

-- ── Drop phase: handle both CONSTRAINT and INDEX forms ─────────────────────
DO $$
DECLARE
    r record;
BEGIN
    FOR r IN SELECT * FROM (VALUES
        ('graph_nodes',             'idx_graph_nodes_label_ci'),
        ('graph_nodes',             'unique_graph_nodes_normalized_label'),
        ('graph_nodes',             'unique_graph_nodes_normalized_label_type'),
        ('retrieval_phrase_nodes',  'retrieval_phrase_nodes_normalized_text_key'),
        ('pending_graph_edges',     'idx_pending_edges_triple'),
        ('resources',               'resources_url_unique'),
        ('canonical_pages',         'canonical_pages_title_is_current_idx'),
        ('canonical_pages',         'canonical_pages_title_lower_is_current_idx'),
        ('tasks',                   'idx_tasks_dedup_unique'),
        ('conversation_threads',    'idx_unique_active_entity_thread')
    ) AS t(tbl, idxname)
    LOOP
        IF EXISTS (SELECT 1 FROM pg_constraint c
                   WHERE c.conname = r.idxname
                     AND c.conrelid = format('public.%I', r.tbl)::regclass) THEN
            EXECUTE format('ALTER TABLE public.%I DROP CONSTRAINT %I', r.tbl, r.idxname);
        ELSIF EXISTS (SELECT 1 FROM pg_indexes i
                      WHERE i.schemaname = 'public' AND i.indexname = r.idxname) THEN
            EXECUTE format('DROP INDEX public.%I', r.idxname);
        END IF;
    END LOOP;
END $$;

-- ── Create phase: per-owner composite uniques ───────────────────────────────
-- graph_nodes: label + normalized_label must be unique PER OWNER.
-- (upsert on_conflict="owner_id, normalized_label, type" depends on these)
CREATE UNIQUE INDEX idx_graph_nodes_label_ci
    ON public.graph_nodes USING btree (owner_id, lower(label))
    WHERE (is_current = true);

CREATE UNIQUE INDEX unique_graph_nodes_normalized_label
    ON public.graph_nodes USING btree (owner_id, normalized_label)
    WHERE (is_current = true);

CREATE UNIQUE INDEX unique_graph_nodes_normalized_label_type
    ON public.graph_nodes USING btree (owner_id, normalized_label, type);

-- retrieval_phrase_nodes: phrase text must be unique PER OWNER
-- (indexing writes run constantly; common words must not collide)
CREATE UNIQUE INDEX retrieval_phrase_nodes_normalized_text_key
    ON public.retrieval_phrase_nodes USING btree (owner_id, normalized_text);

-- pending_graph_edges: dedup triple must be unique PER OWNER
-- (shared-world people/orgs overlap between tenants)
CREATE UNIQUE INDEX idx_pending_edges_triple
    ON public.pending_graph_edges
    USING btree (owner_id, lower(source_label), lower(target_label), lower(relationship));

-- resources: the same URL is fine across tenants
CREATE UNIQUE INDEX resources_url_unique
    ON public.resources USING btree (owner_id, url)
    WHERE ((url IS NOT NULL) AND (is_current = true));

-- canonical_pages: the same title is fine across tenants
CREATE UNIQUE INDEX canonical_pages_title_is_current_idx
    ON public.canonical_pages USING btree (owner_id, title)
    WHERE (is_current = true);

CREATE UNIQUE INDEX canonical_pages_title_lower_is_current_idx
    ON public.canonical_pages USING btree (owner_id, lower(title))
    WHERE (is_current = true);

-- tasks: dedup_key must be unique PER OWNER
CREATE UNIQUE INDEX idx_tasks_dedup_unique
    ON public.tasks USING btree (owner_id, dedup_key)
    WHERE ((status <> ALL (ARRAY['done'::text, 'cancelled'::text])) AND (is_current = true));

-- conversation_threads: active entity thread must be unique PER OWNER
-- (defensive: app channel ids may be shared across users)
CREATE UNIQUE INDEX idx_unique_active_entity_thread
    ON public.conversation_threads USING btree (owner_id, chat_id, thread_type, entity_type, entity_id)
    WHERE ((archived_at IS NULL) AND (entity_id IS NOT NULL));

-- ── Verification (run after applying) ─────────────────────────────────────
--   SELECT indexname, indexdef FROM pg_indexes WHERE schemaname='public'
--   AND indexname IN ('idx_graph_nodes_label_ci',
--     'unique_graph_nodes_normalized_label','unique_graph_nodes_normalized_label_type',
--     'retrieval_phrase_nodes_normalized_text_key','idx_pending_edges_triple',
--     'resources_url_unique','canonical_pages_title_is_current_idx',
--     'canonical_pages_title_lower_is_current_idx','idx_tasks_dedup_unique',
--     'idx_unique_active_entity_thread') ORDER BY indexname;
--   Every indexdef must list owner_id as the FIRST column.
