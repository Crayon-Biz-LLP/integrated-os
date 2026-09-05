-- 110_temporal_triggers_full_archive.sql
--
-- Root Cause: the temporal-lineage trigger functions (db/02 → db/31 → db/91)
-- archive the OLD row on material UPDATE by INSERTing it back into the same
-- table with a HARDCODED column list. That list was written in the projects-era
-- schema and never kept pace with migrations 73/78/91/105+. Result: every
-- archived historical snapshot silently loses `direction`, `organization_id`,
-- `pending_org_id`, `notes`, `committed_to` (tasks) and `pending_org_id`
-- (memories) — the "bare twin" rows seen in residue sweeps are these
-- incomplete archives, not duplicate creations. Migration 91 fixed only
-- `owner_id`; the rest of the drift stayed.
--
-- Fix: replace the hardcoded archive INSERT with a DYNAMIC column list built
-- from information_schema at fire time (excluding `id` — GENERATED ALWAYS
-- identity must generate it — plus `is_current`/`version`, which are forced
-- to `false`/`OLD.version`). Any column added in the future is archived
-- automatically; this mechanism cannot rot again.
--
-- Also hardened in the same pass:
--   1. Material-change conditions extended with the identity-bearing columns
--      the archive was dropping (tasks: organization_id, pending_org_id,
--      direction, notes, committed_to; memories: pending_org_id), so org /
--      notes changes now create history instead of silently rewriting it.
--   2. dedup_memories_insert() (db/108) fires on the archive INSERT into
--      memories and could swallow the archive row as a "duplicate". Guard:
--      non-current inserts are never deduped.
--
-- Idempotent: CREATE OR REPLACE only — safe to re-run.

-- ════════════════════════════════════════════════════════════════════════
-- tasks
-- ════════════════════════════════════════════════════════════════════════
CREATE OR REPLACE FUNCTION temporal_task_update()
RETURNS TRIGGER AS $$
DECLARE
  archived_id int8;
  ins_cols text;
  sel_exprs text;
  q text;
BEGIN
  IF NEW.is_current = true THEN
    IF NEW.title IS DISTINCT FROM OLD.title
       OR NEW.status IS DISTINCT FROM OLD.status
       OR NEW.project_id IS DISTINCT FROM OLD.project_id
       OR NEW.priority IS DISTINCT FROM OLD.priority
       OR NEW.deadline IS DISTINCT FROM OLD.deadline
       OR NEW.reminder_at IS DISTINCT FROM OLD.reminder_at
       OR NEW.organization_id IS DISTINCT FROM OLD.organization_id
       OR NEW.pending_org_id IS DISTINCT FROM OLD.pending_org_id
       OR NEW.direction IS DISTINCT FROM OLD.direction
       OR NEW.notes IS DISTINCT FROM OLD.notes
       OR NEW.committed_to IS DISTINCT FROM OLD.committed_to THEN
      SELECT string_agg(quote_ident(column_name), ', ' ORDER BY ordinal_position),
             string_agg('(s.t).' || quote_ident(column_name), ', ' ORDER BY ordinal_position)
        INTO ins_cols, sel_exprs
        FROM information_schema.columns
       WHERE table_schema = 'public' AND table_name = 'tasks'
         AND column_name NOT IN ('id', 'is_current', 'version');
      q := format(
        'INSERT INTO public.tasks (%s, is_current, version) '
        'SELECT %s, false, (s.t).version '
        'FROM (SELECT $1::public.tasks AS t) s RETURNING id',
        ins_cols, sel_exprs);
      EXECUTE q USING OLD INTO archived_id;
      NEW.version := OLD.version + 1;
      NEW.supersedes_id := archived_id;
    END IF;
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- ════════════════════════════════════════════════════════════════════════
-- canonical_pages
-- ════════════════════════════════════════════════════════════════════════
CREATE OR REPLACE FUNCTION temporal_canonical_pages_update()
RETURNS TRIGGER AS $$
DECLARE
  archived_id int8;
  ins_cols text;
  sel_exprs text;
  q text;
BEGIN
  IF NEW.is_current = true THEN
    IF NEW.title IS DISTINCT FROM OLD.title OR NEW.content IS DISTINCT FROM OLD.content THEN
      SELECT string_agg(quote_ident(column_name), ', ' ORDER BY ordinal_position),
             string_agg('(s.t).' || quote_ident(column_name), ', ' ORDER BY ordinal_position)
        INTO ins_cols, sel_exprs
        FROM information_schema.columns
       WHERE table_schema = 'public' AND table_name = 'canonical_pages'
         AND column_name NOT IN ('id', 'is_current', 'version');
      q := format(
        'INSERT INTO public.canonical_pages (%s, is_current, version) '
        'SELECT %s, false, (s.t).version '
        'FROM (SELECT $1::public.canonical_pages AS t) s RETURNING id',
        ins_cols, sel_exprs);
      EXECUTE q USING OLD INTO archived_id;
      NEW.version := OLD.version + 1;
      NEW.supersedes_id := archived_id;
    END IF;
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- ════════════════════════════════════════════════════════════════════════
-- projects
-- ════════════════════════════════════════════════════════════════════════
CREATE OR REPLACE FUNCTION temporal_projects_update()
RETURNS TRIGGER AS $$
DECLARE
  archived_id int8;
  ins_cols text;
  sel_exprs text;
  q text;
BEGIN
  IF NEW.is_current = true THEN
    IF NEW.name IS DISTINCT FROM OLD.name OR NEW.status IS DISTINCT FROM OLD.status
       OR NEW.context IS DISTINCT FROM OLD.context OR NEW.description IS DISTINCT FROM OLD.description
       OR NEW.organization_id IS DISTINCT FROM OLD.organization_id OR NEW.is_active IS DISTINCT FROM OLD.is_active
       OR NEW.keywords IS DISTINCT FROM OLD.keywords OR NEW.parent_project_id IS DISTINCT FROM OLD.parent_project_id THEN
      SELECT string_agg(quote_ident(column_name), ', ' ORDER BY ordinal_position),
             string_agg('(s.t).' || quote_ident(column_name), ', ' ORDER BY ordinal_position)
        INTO ins_cols, sel_exprs
        FROM information_schema.columns
       WHERE table_schema = 'public' AND table_name = 'projects'
         AND column_name NOT IN ('id', 'is_current', 'version');
      q := format(
        'INSERT INTO public.projects (%s, is_current, version) '
        'SELECT %s, false, (s.t).version '
        'FROM (SELECT $1::public.projects AS t) s RETURNING id',
        ins_cols, sel_exprs);
      EXECUTE q USING OLD INTO archived_id;
      NEW.version := OLD.version + 1;
      NEW.supersedes_id := archived_id;
    END IF;
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- ════════════════════════════════════════════════════════════════════════
-- resources
-- ════════════════════════════════════════════════════════════════════════
CREATE OR REPLACE FUNCTION temporal_resources_update()
RETURNS TRIGGER AS $$
DECLARE
  archived_id int8;
  ins_cols text;
  sel_exprs text;
  q text;
BEGIN
  IF NEW.is_current = true THEN
    IF NEW.title IS DISTINCT FROM OLD.title OR NEW.summary IS DISTINCT FROM OLD.summary
       OR NEW.category IS DISTINCT FROM OLD.category OR NEW.url IS DISTINCT FROM OLD.url
       OR NEW.strategic_note IS DISTINCT FROM OLD.strategic_note OR NEW.project_id IS DISTINCT FROM OLD.project_id
       OR NEW.cluster_id IS DISTINCT FROM OLD.cluster_id OR NEW.organization_id IS DISTINCT FROM OLD.organization_id THEN
      SELECT string_agg(quote_ident(column_name), ', ' ORDER BY ordinal_position),
             string_agg('(s.t).' || quote_ident(column_name), ', ' ORDER BY ordinal_position)
        INTO ins_cols, sel_exprs
        FROM information_schema.columns
       WHERE table_schema = 'public' AND table_name = 'resources'
         AND column_name NOT IN ('id', 'is_current', 'version');
      q := format(
        'INSERT INTO public.resources (%s, is_current, version) '
        'SELECT %s, false, (s.t).version '
        'FROM (SELECT $1::public.resources AS t) s RETURNING id',
        ins_cols, sel_exprs);
      EXECUTE q USING OLD INTO archived_id;
      NEW.version := OLD.version + 1;
      NEW.supersedes_id := archived_id;
    END IF;
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- ════════════════════════════════════════════════════════════════════════
-- memories
-- ════════════════════════════════════════════════════════════════════════
CREATE OR REPLACE FUNCTION temporal_memories_update()
RETURNS TRIGGER AS $$
DECLARE
  archived_id int8;
  ins_cols text;
  sel_exprs text;
  q text;
BEGIN
  IF NEW.is_current = true THEN
    IF NEW.version IS DISTINCT FROM OLD.version THEN
      RETURN NEW;
    END IF;
    IF NEW.content IS DISTINCT FROM OLD.content OR NEW.memory_type IS DISTINCT FROM OLD.memory_type
       OR NEW.project_id IS DISTINCT FROM OLD.project_id OR NEW.organization_id IS DISTINCT FROM OLD.organization_id
       OR NEW.pending_org_id IS DISTINCT FROM OLD.pending_org_id OR NEW.sentiment_score IS DISTINCT FROM OLD.sentiment_score
       OR NEW.entities_mentioned IS DISTINCT FROM OLD.entities_mentioned OR NEW.expires_at IS DISTINCT FROM OLD.expires_at
       OR NEW.importance_score IS DISTINCT FROM OLD.importance_score OR NEW.metadata IS DISTINCT FROM OLD.metadata THEN
      SELECT string_agg(quote_ident(column_name), ', ' ORDER BY ordinal_position),
             string_agg('(s.t).' || quote_ident(column_name), ', ' ORDER BY ordinal_position)
        INTO ins_cols, sel_exprs
        FROM information_schema.columns
       WHERE table_schema = 'public' AND table_name = 'memories'
         AND column_name NOT IN ('id', 'is_current', 'version');
      q := format(
        'INSERT INTO public.memories (%s, is_current, version) '
        'SELECT %s, false, (s.t).version '
        'FROM (SELECT $1::public.memories AS t) s RETURNING id',
        ins_cols, sel_exprs);
      EXECUTE q USING OLD INTO archived_id;
      NEW.version := OLD.version + 1;
      NEW.supersedes_id := archived_id;
    END IF;
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- ════════════════════════════════════════════════════════════════════════
-- graph_nodes (uuid id)
-- ════════════════════════════════════════════════════════════════════════
CREATE OR REPLACE FUNCTION temporal_graph_nodes_update()
RETURNS TRIGGER AS $$
DECLARE
  archived_id uuid;
  ins_cols text;
  sel_exprs text;
  q text;
BEGIN
  IF NEW.is_current = true THEN
    IF NEW.label IS DISTINCT FROM OLD.label OR NEW.type IS DISTINCT FROM OLD.type
       OR NEW.metadata IS DISTINCT FROM OLD.metadata OR NEW.epistemic_status IS DISTINCT FROM OLD.epistemic_status
       OR NEW.canonical_id IS DISTINCT FROM OLD.canonical_id OR NEW.db_record_id IS DISTINCT FROM OLD.db_record_id
       OR NEW.normalized_label IS DISTINCT FROM OLD.normalized_label THEN
      SELECT string_agg(quote_ident(column_name), ', ' ORDER BY ordinal_position),
             string_agg('(s.t).' || quote_ident(column_name), ', ' ORDER BY ordinal_position)
        INTO ins_cols, sel_exprs
        FROM information_schema.columns
       WHERE table_schema = 'public' AND table_name = 'graph_nodes'
         AND column_name NOT IN ('id', 'is_current', 'version');
      q := format(
        'INSERT INTO public.graph_nodes (%s, is_current, version) '
        'SELECT %s, false, (s.t).version '
        'FROM (SELECT $1::public.graph_nodes AS t) s RETURNING id',
        ins_cols, sel_exprs);
      EXECUTE q USING OLD INTO archived_id;
      NEW.version := OLD.version + 1;
      NEW.supersedes_id := archived_id;
    END IF;
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- ════════════════════════════════════════════════════════════════════════
-- graph_edges (uuid id)
-- ════════════════════════════════════════════════════════════════════════
CREATE OR REPLACE FUNCTION temporal_graph_edges_update()
RETURNS TRIGGER AS $$
DECLARE
  archived_id uuid;
  ins_cols text;
  sel_exprs text;
  q text;
BEGIN
  IF NEW.is_current = true THEN
    IF NEW.relationship IS DISTINCT FROM OLD.relationship OR NEW.weight IS DISTINCT FROM OLD.weight
       OR NEW.metadata IS DISTINCT FROM OLD.metadata OR NEW.epistemic_status IS DISTINCT FROM OLD.epistemic_status
       OR NEW.valid_until IS DISTINCT FROM OLD.valid_until OR NEW.source_ref IS DISTINCT FROM OLD.source_ref
       OR NEW.archived IS DISTINCT FROM OLD.archived THEN
      SELECT string_agg(quote_ident(column_name), ', ' ORDER BY ordinal_position),
             string_agg('(s.t).' || quote_ident(column_name), ', ' ORDER BY ordinal_position)
        INTO ins_cols, sel_exprs
        FROM information_schema.columns
       WHERE table_schema = 'public' AND table_name = 'graph_edges'
         AND column_name NOT IN ('id', 'is_current', 'version');
      q := format(
        'INSERT INTO public.graph_edges (%s, is_current, version) '
        'SELECT %s, false, (s.t).version '
        'FROM (SELECT $1::public.graph_edges AS t) s RETURNING id',
        ins_cols, sel_exprs);
      EXECUTE q USING OLD INTO archived_id;
      NEW.version := OLD.version + 1;
      NEW.supersedes_id := archived_id;
    END IF;
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- ════════════════════════════════════════════════════════════════════════
-- dedup_memories_insert (db/108): never swallow non-current inserts.
-- The temporal archive INSERT above inserts is_current=false — if the
-- archived content matches a recent current note, the old dedup trigger
-- would swallow the archive row and silently destroy history.
-- ════════════════════════════════════════════════════════════════════════
CREATE OR REPLACE FUNCTION dedup_memories_insert()
RETURNS TRIGGER AS $$
DECLARE
  normalized_new TEXT;
  content_hash TEXT;
  dedup_window INTERVAL := INTERVAL '10 minutes';
BEGIN
  -- Never dedup non-current inserts (temporal-versioning archive rows).
  IF NOT NEW.is_current THEN
    RETURN NEW;
  END IF;

  -- Only dedup user-facing note types (skip outcome, briefing, etc.)
  IF NEW.memory_type NOT IN ('note', 'relationship_note') THEN
    RETURN NEW;
  END IF;

  -- Only dedup within same tenant
  IF NEW.owner_id IS NULL THEN
    RETURN NEW;
  END IF;

  -- Normalize: lowercase, collapse whitespace, strip leading source prefix
  -- ("teams: Hello world" → "hello world")
  normalized_new := lower(regexp_replace(NEW.content, '\s+', ' ', 'g'));
  normalized_new := regexp_replace(normalized_new, '^[a-z_]+:\s*', '');
  normalized_new := trim(normalized_new);

  -- Skip trivially short content (tokens, single words)
  IF length(normalized_new) < 10 THEN
    RETURN NEW;
  END IF;

  -- Compute content hash for comparison
  content_hash := md5(normalized_new);

  -- Check if a RECENT current note with same normalized content + org + tenant exists.
  -- The 10-minute window is critical:
  --   - True duplicates (web ingest → confirm → pulse, same message): seconds apart ✅ caught
  --   - Recurring notes (daily standup, weekly recap): hours/days apart ✅ allowed
  --   - Same content from different threads: different sender/session, hours apart ✅ allowed
  IF EXISTS (
    SELECT 1 FROM memories
    WHERE is_current = true
      AND owner_id = NEW.owner_id
      AND memory_type = NEW.memory_type
      AND created_at >= NOW() - dedup_window
      AND md5(
            lower(regexp_replace(
              regexp_replace(content, '^[a-z_]+:\s*', ''),
              '\s+', ' ', 'g'
            ))
          ) = content_hash
      AND (
        (organization_id IS NOT NULL AND organization_id = NEW.organization_id)
        OR (organization_id IS NULL AND NEW.organization_id IS NULL)
      )
  ) THEN
    -- Duplicate detected — swallow the INSERT
    RAISE NOTICE 'dedup_memories: swallowed duplicate note (content_hash=%, org=%, window=%)',
      content_hash, NEW.organization_id, dedup_window;
    RETURN NULL;
  END IF;

  -- No duplicate — allow the INSERT
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;