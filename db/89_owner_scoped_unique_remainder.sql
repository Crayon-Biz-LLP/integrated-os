-- 89_owner_scoped_unique_remainder.sql — owner-scope the remaining
-- content-derived unique keys
--
-- Context: db/88 converted the 10 label/token uniques that BLOCK cross-tenant
-- inserts (graph node labels, phrase nodes, pending-edge triples, resources,
-- canonical pages, tasks, threads). This migration closes the SAME class for
-- the unique keys derived from CONTENT — values two DIFFERENT tenants can
-- legitimately produce identically. On a global unique, the second tenant's
-- insert is rejected: not a leak, but a silently LOST row for that tenant.
--
-- Fixed (all rows are tenant #1 today, so the old uniques already guarantee
-- zero duplicates for the new composites — verified pre-apply):
--
--   retrieval_passages      (source_fingerprint, passage_index, index_version)
--       fingerprint = sha256(text)[:16] — byte-identical content across tenants
--   messages                (channel, message_id)
--       channel is 'email' (not chat-scoped) and Gmail Message-IDs are
--       sender-global: a mailing-list/group mail received by two tenants has
--       the SAME Message-ID → tenant #2's copy would be rejected
--   classifier_corrections  (text_pattern, old_intent, new_intent)
--       user message text — "mark as done"-class corrections recur across tenants
--   pending_graph_edges_archive (source_label, target_label, relationship)
--       entity labels — common names recur across tenants
--   subsystem_patterns      (subsystem, feature_hash)
--       feature_hash of pattern content; the subsystem vocabulary is shared
--   retrieval_eval_gold     (query_text) — admin/dev eval data, same class
--
-- Deliberately NOT changed (structurally safe — keyed on global UUIDs,
-- external global ids, per-account opaque ids, or globally-allocated tokens):
--   graph_edges / pending_graph_edges node-id triples, retrieval_edges,
--   retrieval_triples/passage links (passage_id), retrieval_index_runs
--   (memory_id), memory_cluster_members, project_organizations, projects
--   (name+org_id), retrieval_node_stats (node_id), processed_updates
--   (update_id), call_recordings (drive_file_id — per-account), shortcodes
--   (allocated by the GLOBAL next_clarification_shortcode RPC).
--
-- Mechanics (db/88 lesson + pattern): the old constraint-backed uniques
-- (retrieval_passages, messages, subsystem_patterns) are DROPPED as
-- CONSTRAINTS (plain DROP INDEX fails with 2BP01) and recreated as
-- standalone CREATE UNIQUE INDEX IF NOT EXISTS with the SAME names — the
-- exact approach db/88 used for the same class. Standalone indexes
-- (classifier_corrections, pending_graph_edges_archive labels,
-- retrieval_eval_gold) drop as indexes. PostgREST on_conflict works against
-- unique indexes, so upserts keep working. Every statement is guarded
-- (IF EXISTS / IF NOT EXISTS) — the whole migration is re-runnable after a
-- partial failure.
--
-- Code impact: retrieval_eval_gold is upserted with on_conflict="query_text"
-- in core/retrieval/eval.py — updated to "owner_id, query_text" alongside.

-- ── Pre-flight: the new composites must have zero duplicates ───────────────
-- All rows are tenant #1 today and the old uniques already forbid duplicates
-- of the non-owner columns, so these are guaranteed 0 — but if any future
-- partial state ever violates it, ABORT before the uniques are dropped
-- (DROP + ADD are separate statements; this makes the pair atomic in
-- intent). bool_and(...) mimics unique semantics: rows with a NULL key
-- column are DISTINCT under a unique index, so they must not count as dups.
DO $$
DECLARE
    n int;
BEGIN
    SELECT count(*) INTO n FROM (SELECT 1 FROM public.retrieval_passages
        GROUP BY owner_id, source_fingerprint, passage_index, index_version
        HAVING count(*) > 1
           AND bool_and(source_fingerprint IS NOT NULL
                     AND passage_index IS NOT NULL
                     AND index_version IS NOT NULL)) d;
    IF n > 0 THEN RAISE EXCEPTION 'db/89 abort: retrieval_passages % duplicate composites', n; END IF;
    SELECT count(*) INTO n FROM (SELECT 1 FROM public.messages
        GROUP BY owner_id, channel, message_id
        HAVING count(*) > 1 AND bool_and(message_id IS NOT NULL)) d;
    IF n > 0 THEN RAISE EXCEPTION 'db/89 abort: messages % duplicate composites', n; END IF;
    SELECT count(*) INTO n FROM (SELECT 1 FROM public.classifier_corrections
        GROUP BY owner_id, text_pattern, old_intent, new_intent
        HAVING count(*) > 1) d;
    IF n > 0 THEN RAISE EXCEPTION 'db/89 abort: classifier_corrections % duplicate composites', n; END IF;
    SELECT count(*) INTO n FROM (SELECT 1 FROM public.pending_graph_edges_archive
        GROUP BY owner_id, source_label, target_label, relationship
        HAVING count(*) > 1) d;
    IF n > 0 THEN RAISE EXCEPTION 'db/89 abort: pending_graph_edges_archive % duplicate composites', n; END IF;
    SELECT count(*) INTO n FROM (SELECT 1 FROM public.subsystem_patterns
        GROUP BY owner_id, subsystem, feature_hash
        HAVING count(*) > 1) d;
    IF n > 0 THEN RAISE EXCEPTION 'db/89 abort: subsystem_patterns % duplicate composites', n; END IF;
    SELECT count(*) INTO n FROM (SELECT 1 FROM public.retrieval_eval_gold
        GROUP BY owner_id, query_text HAVING count(*) > 1) d;
    IF n > 0 THEN RAISE EXCEPTION 'db/89 abort: retrieval_eval_gold % duplicate composites', n; END IF;
    RAISE NOTICE 'db/89 pre-flight OK — no duplicate composites';
END $$;

-- ── 1. retrieval_passages (old constraint → owner-scoped unique index) ─────
ALTER TABLE public.retrieval_passages
    DROP CONSTRAINT IF EXISTS retrieval_passages_source_fingerprint_passage_index_index_v_key;
CREATE UNIQUE INDEX IF NOT EXISTS retrieval_passages_source_fingerprint_passage_index_index_v_key
    ON public.retrieval_passages (owner_id, source_fingerprint, passage_index, index_version);

-- ── 2. messages (old constraint → owner-scoped unique index) ───────────────
ALTER TABLE public.messages
    DROP CONSTRAINT IF EXISTS unique_channel_message;
CREATE UNIQUE INDEX IF NOT EXISTS unique_channel_message
    ON public.messages (owner_id, channel, message_id);

-- ── 3. classifier_corrections (standalone unique index) ───────────────────
DROP INDEX IF EXISTS public.idx_corrections_dedup;
CREATE UNIQUE INDEX idx_corrections_dedup
    ON public.classifier_corrections (owner_id, text_pattern, old_intent, new_intent);

-- ── 4. pending_graph_edges_archive labels (standalone unique index) ────────
DROP INDEX IF EXISTS public.pending_graph_edges_archive_source_label_target_label_relat_idx;
CREATE UNIQUE INDEX pending_graph_edges_archive_source_label_target_label_relat_idx
    ON public.pending_graph_edges_archive (owner_id, source_label, target_label, relationship);

-- ── 5. subsystem_patterns (old constraint → owner-scoped unique index) ─────
ALTER TABLE public.subsystem_patterns
    DROP CONSTRAINT IF EXISTS subsystem_patterns_subsystem_feature_hash_key;
CREATE UNIQUE INDEX IF NOT EXISTS subsystem_patterns_subsystem_feature_hash_key
    ON public.subsystem_patterns (owner_id, subsystem, feature_hash);

-- ── 6. retrieval_eval_gold (standalone unique index) ───────────────────────
DROP INDEX IF EXISTS public.idx_eval_gold_query;
CREATE UNIQUE INDEX idx_eval_gold_query
    ON public.retrieval_eval_gold (owner_id, query_text);
