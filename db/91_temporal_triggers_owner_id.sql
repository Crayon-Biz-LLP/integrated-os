-- 91_temporal_triggers_owner_id.sql
--
-- Root Cause: the temporal-lineage trigger functions (db/02, db/31) archive
-- the OLD row on material UPDATE by inserting it into the same table with a
-- HARDCODED column list written BEFORE migration 78 added owner_id. After
-- 78 set owner_id NOT NULL, every material UPDATE (task status change,
-- memory edit, graph node touch, etc.) fires the trigger, whose INSERT
-- omits owner_id → violates the NOT NULL constraint → the WHOLE update
-- aborts and rolls back (the onboarding-demo "Close it" failure, and any
-- in-app task completion).
--
-- Fix: re-CREATE all 7 functions, adding owner_id = OLD.owner_id to the
-- archive INSERT column list. Idempotent (CREATE OR REPLACE) — safe to
-- re-run.

-- ── tasks ────────────────────────────────────────────────────────────────
CREATE OR REPLACE FUNCTION temporal_task_update()
RETURNS TRIGGER AS $$
DECLARE
  archived_id int8;
BEGIN
  IF NEW.is_current = true THEN
    IF NEW.title IS DISTINCT FROM OLD.title OR NEW.status IS DISTINCT FROM OLD.status OR NEW.project_id IS DISTINCT FROM OLD.project_id OR NEW.priority IS DISTINCT FROM OLD.priority OR NEW.deadline IS DISTINCT FROM OLD.deadline OR NEW.reminder_at IS DISTINCT FROM OLD.reminder_at THEN
      INSERT INTO public.tasks (title, status, priority, project_id, estimated_minutes, is_revenue_critical, deadline, created_at, completed_at, google_task_id, reminder_at, google_event_id, duration_mins, source, email_id, dedup_key, updated_at, is_current, version, supersedes_id, recurrence, owner_id)
      VALUES (OLD.title, OLD.status, OLD.priority, OLD.project_id, OLD.estimated_minutes, OLD.is_revenue_critical, OLD.deadline, OLD.created_at, OLD.completed_at, OLD.google_task_id, OLD.reminder_at, OLD.google_event_id, OLD.duration_mins, OLD.source, OLD.email_id, OLD.dedup_key, OLD.updated_at, false, OLD.version, OLD.supersedes_id, OLD.recurrence, OLD.owner_id)
      RETURNING id INTO archived_id;
      NEW.version = OLD.version + 1;
      NEW.supersedes_id = archived_id;
    END IF;
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- ── canonical_pages ──────────────────────────────────────────────────────
CREATE OR REPLACE FUNCTION temporal_canonical_pages_update()
RETURNS TRIGGER AS $$
DECLARE
  archived_id int8;
BEGIN
  IF NEW.is_current = true THEN
    IF NEW.title IS DISTINCT FROM OLD.title OR NEW.content IS DISTINCT FROM OLD.content THEN
      INSERT INTO public.canonical_pages (title, content, category, entity_id, embedding, updated_at, project_id, source_count, last_synth_at, is_sparse, is_archived, archived_at, archive_reason, is_current, version, supersedes_id, owner_id)
      VALUES (OLD.title, OLD.content, OLD.category, OLD.entity_id, OLD.embedding, OLD.updated_at, OLD.project_id, OLD.source_count, OLD.last_synth_at, OLD.is_sparse, OLD.is_archived, OLD.archived_at, OLD.archive_reason, false, OLD.version, OLD.supersedes_id, OLD.owner_id)
      RETURNING id INTO archived_id;
      NEW.version = OLD.version + 1;
      NEW.supersedes_id = archived_id;
    END IF;
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- ── projects ─────────────────────────────────────────────────────────────
CREATE OR REPLACE FUNCTION temporal_projects_update()
RETURNS TRIGGER AS $$
DECLARE
  archived_id int8;
BEGIN
  IF NEW.is_current = true THEN
    IF NEW.name IS DISTINCT FROM OLD.name OR NEW.status IS DISTINCT FROM OLD.status OR NEW.context IS DISTINCT FROM OLD.context OR NEW.description IS DISTINCT FROM OLD.description OR NEW.organization_id IS DISTINCT FROM OLD.organization_id OR NEW.is_active IS DISTINCT FROM OLD.is_active OR NEW.keywords IS DISTINCT FROM OLD.keywords OR NEW.parent_project_id IS DISTINCT FROM OLD.parent_project_id THEN
      INSERT INTO public.projects (name, status, context, description, created_at, is_active, parent_project_id, keywords, is_current, version, supersedes_id, organization_id, owner_id)
      VALUES (OLD.name, OLD.status, OLD.context, OLD.description, OLD.created_at, OLD.is_active, OLD.parent_project_id, OLD.keywords, false, OLD.version, OLD.supersedes_id, OLD.organization_id, OLD.owner_id)
      RETURNING id INTO archived_id;
      NEW.version = OLD.version + 1;
      NEW.supersedes_id = archived_id;
    END IF;
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- ── resources ────────────────────────────────────────────────────────────
CREATE OR REPLACE FUNCTION temporal_resources_update()
RETURNS TRIGGER AS $$
DECLARE
  archived_id int8;
BEGIN
  IF NEW.is_current = true THEN
    IF NEW.title IS DISTINCT FROM OLD.title OR NEW.summary IS DISTINCT FROM OLD.summary OR NEW.category IS DISTINCT FROM OLD.category OR NEW.url IS DISTINCT FROM OLD.url OR NEW.strategic_note IS DISTINCT FROM OLD.strategic_note OR NEW.project_id IS DISTINCT FROM OLD.project_id OR NEW.cluster_id IS DISTINCT FROM OLD.cluster_id OR NEW.organization_id IS DISTINCT FROM OLD.organization_id THEN
      INSERT INTO public.resources (url, title, summary, category, project_id, created_at, strategic_note, cluster_id, enriched_at, embedding, is_current, version, supersedes_id, organization_id, dismissed_at, owner_id)
      VALUES (OLD.url, OLD.title, OLD.summary, OLD.category, OLD.project_id, OLD.created_at, OLD.strategic_note, OLD.cluster_id, OLD.enriched_at, OLD.embedding, false, OLD.version, OLD.supersedes_id, OLD.organization_id, OLD.dismissed_at, OLD.owner_id)
      RETURNING id INTO archived_id;
      NEW.version = OLD.version + 1;
      NEW.supersedes_id = archived_id;
    END IF;
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- ── memories ─────────────────────────────────────────────────────────────
CREATE OR REPLACE FUNCTION temporal_memories_update()
RETURNS TRIGGER AS $$
DECLARE
  archived_id int8;
BEGIN
  IF NEW.is_current = true THEN
    IF NEW.version IS DISTINCT FROM OLD.version THEN
      RETURN NEW;
    END IF;
    IF NEW.content IS DISTINCT FROM OLD.content OR NEW.memory_type IS DISTINCT FROM OLD.memory_type OR NEW.project_id IS DISTINCT FROM OLD.project_id OR NEW.organization_id IS DISTINCT FROM OLD.organization_id OR NEW.sentiment_score IS DISTINCT FROM OLD.sentiment_score OR NEW.entities_mentioned IS DISTINCT FROM OLD.entities_mentioned OR NEW.expires_at IS DISTINCT FROM OLD.expires_at OR NEW.importance_score IS DISTINCT FROM OLD.importance_score OR NEW.metadata IS DISTINCT FROM OLD.metadata THEN
      INSERT INTO public.memories (content, metadata, embedding, created_at, memory_type, source, embedding_status, is_archived, archived_at, archive_reason, importance_score, last_accessed_at, supersedes_id, pruned, pruned_at, pruned_reason, superseded_by, is_current, version, project_id, sentiment_score, sentiment, entities_mentioned, expires_at, organization_id, owner_id)
      VALUES (OLD.content, OLD.metadata, OLD.embedding, OLD.created_at, OLD.memory_type, OLD.source, OLD.embedding_status, OLD.is_archived, OLD.archived_at, OLD.archive_reason, OLD.importance_score, OLD.last_accessed_at, OLD.supersedes_id, OLD.pruned, OLD.pruned_at, OLD.pruned_reason, OLD.superseded_by, false, OLD.version, OLD.project_id, OLD.sentiment_score, OLD.sentiment, OLD.entities_mentioned, OLD.expires_at, OLD.organization_id, OLD.owner_id)
      RETURNING id INTO archived_id;
      NEW.version = OLD.version + 1;
      NEW.supersedes_id = archived_id;
    END IF;
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- ── graph_nodes ──────────────────────────────────────────────────────────
CREATE OR REPLACE FUNCTION temporal_graph_nodes_update()
RETURNS TRIGGER AS $$
DECLARE
  archived_id uuid;
BEGIN
  IF NEW.is_current = true THEN
    IF NEW.label IS DISTINCT FROM OLD.label OR NEW.type IS DISTINCT FROM OLD.type OR NEW.metadata IS DISTINCT FROM OLD.metadata OR NEW.epistemic_status IS DISTINCT FROM OLD.epistemic_status OR NEW.canonical_id IS DISTINCT FROM OLD.canonical_id OR NEW.db_record_id IS DISTINCT FROM OLD.db_record_id OR NEW.normalized_label IS DISTINCT FROM OLD.normalized_label THEN
      INSERT INTO public.graph_nodes (label, type, metadata, embedding, canonical_page_id, canonical_id, created_at, epistemic_status, reference_count, last_referenced_at, db_record_id, normalized_label, is_current, version, supersedes_id, owner_id)
      VALUES (OLD.label, OLD.type, OLD.metadata, OLD.embedding, OLD.canonical_page_id, OLD.canonical_id, OLD.created_at, OLD.epistemic_status, OLD.reference_count, OLD.last_referenced_at, OLD.db_record_id, NULL, false, OLD.version, OLD.supersedes_id, OLD.owner_id)
      RETURNING id INTO archived_id;
      NEW.version = OLD.version + 1;
      NEW.supersedes_id = archived_id;
    END IF;
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- ── graph_edges ──────────────────────────────────────────────────────────
CREATE OR REPLACE FUNCTION temporal_graph_edges_update()
RETURNS TRIGGER AS $$
DECLARE
  archived_id uuid;
BEGIN
  IF NEW.is_current = true THEN
    IF NEW.relationship IS DISTINCT FROM OLD.relationship OR NEW.weight IS DISTINCT FROM OLD.weight OR NEW.metadata IS DISTINCT FROM OLD.metadata OR NEW.epistemic_status IS DISTINCT FROM OLD.epistemic_status OR NEW.valid_until IS DISTINCT FROM OLD.valid_until OR NEW.source_ref IS DISTINCT FROM OLD.source_ref OR NEW.archived IS DISTINCT FROM OLD.archived THEN
      INSERT INTO public.graph_edges (source_node_id, target_node_id, relationship, weight, metadata, created_at, valid_from, valid_until, source_ref, epistemic_status, archived, last_confirmed_at, is_current, version, supersedes_id, owner_id)
      VALUES (OLD.source_node_id, OLD.target_node_id, OLD.relationship, OLD.weight, OLD.metadata, OLD.created_at, OLD.valid_from, OLD.valid_until, OLD.source_ref, OLD.epistemic_status, OLD.archived, OLD.last_confirmed_at, false, OLD.version, OLD.supersedes_id, OLD.owner_id)
      RETURNING id INTO archived_id;
      NEW.version = OLD.version + 1;
      NEW.supersedes_id = archived_id;
    END IF;
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;
