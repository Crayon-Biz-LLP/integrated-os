-- Temporal Versioning Expansion
-- Adds versioning columns + triggers to graph_nodes, graph_edges, people
-- Adds triggers to projects, resources (columns already exist)
-- Migrates memories from app-level to trigger-based versioning
-- Fixes unique constraints to allow archived rows

-- ==============================================================
-- STEP 1: Add versioning columns to 3 missing tables
-- ==============================================================

-- graph_nodes (uuid pk)
ALTER TABLE graph_nodes ADD COLUMN is_current BOOLEAN NOT NULL DEFAULT TRUE;
ALTER TABLE graph_nodes ADD COLUMN version INTEGER NOT NULL DEFAULT 1;
ALTER TABLE graph_nodes ADD COLUMN supersedes_id UUID REFERENCES graph_nodes(id);

-- graph_edges (uuid pk)
ALTER TABLE graph_edges ADD COLUMN is_current BOOLEAN NOT NULL DEFAULT TRUE;
ALTER TABLE graph_edges ADD COLUMN version INTEGER NOT NULL DEFAULT 1;
ALTER TABLE graph_edges ADD COLUMN supersedes_id UUID REFERENCES graph_edges(id);

-- people (bigint pk)
ALTER TABLE people ADD COLUMN is_current BOOLEAN NOT NULL DEFAULT TRUE;
ALTER TABLE people ADD COLUMN version INTEGER NOT NULL DEFAULT 1;
ALTER TABLE people ADD COLUMN supersedes_id BIGINT REFERENCES people(id);

-- Note: projects and resources already have these columns.
-- Note: memories already has these columns.

-- ==============================================================
-- STEP 2: Fix unique constraints for versioning
-- ==============================================================

-- graph_nodes: both unique indexes need WHERE is_current = TRUE
-- to allow archived rows with the same label/normalized_label
DROP INDEX IF EXISTS idx_graph_nodes_label_ci;
CREATE UNIQUE INDEX idx_graph_nodes_label_ci
  ON graph_nodes (lower(label)) WHERE is_current = TRUE;

DROP INDEX IF EXISTS unique_graph_nodes_normalized_label;
CREATE UNIQUE INDEX unique_graph_nodes_normalized_label
  ON graph_nodes (normalized_label) WHERE is_current = TRUE;

-- projects: org-scoped unique must also scope to active rows only
DROP INDEX IF EXISTS projects_name_org_unique;
CREATE UNIQUE INDEX projects_name_org_unique
  ON projects (name, organization_id)
  WHERE organization_id IS NOT NULL AND is_current = TRUE;

-- resources: URL uniqueness must scope to active rows only
DROP INDEX IF EXISTS resources_url_unique;
CREATE UNIQUE INDEX resources_url_unique
  ON resources (url) WHERE url IS NOT NULL AND is_current = TRUE;

-- Performance indexes for is_current queries on new tables
CREATE INDEX idx_graph_nodes_is_current ON graph_nodes (is_current) WHERE is_current = TRUE;
CREATE INDEX idx_graph_edges_is_current ON graph_edges (is_current) WHERE is_current = TRUE;
CREATE INDEX idx_people_is_current ON people (is_current) WHERE is_current = TRUE;

-- ==============================================================
-- STEP 3: Trigger Functions - UUID PK tables
-- ==============================================================

-- Trigger Function for graph_nodes
CREATE OR REPLACE FUNCTION temporal_graph_nodes_update()
RETURNS TRIGGER AS $$
DECLARE
  archived_id uuid;
BEGIN
  IF NEW.is_current = true THEN
    -- Only version if something material changed
    IF NEW.label IS DISTINCT FROM OLD.label OR NEW.type IS DISTINCT FROM OLD.type OR NEW.metadata IS DISTINCT FROM OLD.metadata OR NEW.epistemic_status IS DISTINCT FROM OLD.epistemic_status OR NEW.canonical_id IS DISTINCT FROM OLD.canonical_id OR NEW.db_record_id IS DISTINCT FROM OLD.db_record_id OR NEW.normalized_label IS DISTINCT FROM OLD.normalized_label THEN

      -- Insert the OLD state as a historical record
      INSERT INTO public.graph_nodes (label, type, metadata, embedding, canonical_page_id, canonical_id, created_at, epistemic_status, reference_count, last_referenced_at, db_record_id, normalized_label, is_current, version, supersedes_id)
      VALUES (OLD.label, OLD.type, OLD.metadata, OLD.embedding, OLD.canonical_page_id, OLD.canonical_id, OLD.created_at, OLD.epistemic_status, OLD.reference_count, OLD.last_referenced_at, OLD.db_record_id, OLD.normalized_label, false, OLD.version, OLD.supersedes_id)
      RETURNING id INTO archived_id;

      -- Update the NEW row to increment version and point to the historical record
      NEW.version = OLD.version + 1;
      NEW.supersedes_id = archived_id;
    END IF;
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_temporal_graph_nodes_update
BEFORE UPDATE ON public.graph_nodes
FOR EACH ROW
WHEN (pg_trigger_depth() = 0)
EXECUTE FUNCTION temporal_graph_nodes_update();

-- Trigger Function for graph_edges
CREATE OR REPLACE FUNCTION temporal_graph_edges_update()
RETURNS TRIGGER AS $$
DECLARE
  archived_id uuid;
BEGIN
  IF NEW.is_current = true THEN
    -- Only version if something material changed
    IF NEW.relationship IS DISTINCT FROM OLD.relationship OR NEW.weight IS DISTINCT FROM OLD.weight OR NEW.metadata IS DISTINCT FROM OLD.metadata OR NEW.epistemic_status IS DISTINCT FROM OLD.epistemic_status OR NEW.valid_until IS DISTINCT FROM OLD.valid_until OR NEW.source_ref IS DISTINCT FROM OLD.source_ref OR NEW.archived IS DISTINCT FROM OLD.archived THEN

      -- Insert the OLD state as a historical record
      INSERT INTO public.graph_edges (source_node_id, target_node_id, relationship, weight, metadata, created_at, valid_from, valid_until, source_ref, epistemic_status, archived, last_confirmed_at, is_current, version, supersedes_id)
      VALUES (OLD.source_node_id, OLD.target_node_id, OLD.relationship, OLD.weight, OLD.metadata, OLD.created_at, OLD.valid_from, OLD.valid_until, OLD.source_ref, OLD.epistemic_status, OLD.archived, OLD.last_confirmed_at, false, OLD.version, OLD.supersedes_id)
      RETURNING id INTO archived_id;

      -- Update the NEW row to increment version and point to the historical record
      NEW.version = OLD.version + 1;
      NEW.supersedes_id = archived_id;
    END IF;
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_temporal_graph_edges_update
BEFORE UPDATE ON public.graph_edges
FOR EACH ROW
WHEN (pg_trigger_depth() = 0)
EXECUTE FUNCTION temporal_graph_edges_update();

-- ==============================================================
-- STEP 4: Trigger Functions - BIGINT PK tables
-- ==============================================================

-- Trigger Function for people
CREATE OR REPLACE FUNCTION temporal_people_update()
RETURNS TRIGGER AS $$
DECLARE
  archived_id int8;
BEGIN
  IF NEW.is_current = true THEN
    -- Only version if something material changed
    IF NEW.name IS DISTINCT FROM OLD.name OR NEW.role IS DISTINCT FROM OLD.role OR NEW.organization_name IS DISTINCT FROM OLD.organization_name OR NEW.strategic_weight IS DISTINCT FROM OLD.strategic_weight OR NEW.enrichment_notes IS DISTINCT FROM OLD.enrichment_notes THEN

      -- Insert the OLD state as a historical record
      INSERT INTO public.people (name, role, strategic_weight, created_at, source, graph_node_id, organization_name, last_interaction_date, enrichment_notes, enriched_at, is_current, version, supersedes_id)
      VALUES (OLD.name, OLD.role, OLD.strategic_weight, OLD.created_at, OLD.source, OLD.graph_node_id, OLD.organization_name, OLD.last_interaction_date, OLD.enrichment_notes, OLD.enriched_at, false, OLD.version, OLD.supersedes_id)
      RETURNING id INTO archived_id;

      -- Update the NEW row to increment version and point to the historical record
      NEW.version = OLD.version + 1;
      NEW.supersedes_id = archived_id;
    END IF;
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_temporal_people_update
BEFORE UPDATE ON public.people
FOR EACH ROW
WHEN (pg_trigger_depth() = 0)
EXECUTE FUNCTION temporal_people_update();

-- Trigger Function for projects (columns already exist)
CREATE OR REPLACE FUNCTION temporal_projects_update()
RETURNS TRIGGER AS $$
DECLARE
  archived_id int8;
BEGIN
  IF NEW.is_current = true THEN
    -- Only version if something material changed
    IF NEW.name IS DISTINCT FROM OLD.name OR NEW.status IS DISTINCT FROM OLD.status OR NEW.context IS DISTINCT FROM OLD.context OR NEW.description IS DISTINCT FROM OLD.description OR NEW.organization_id IS DISTINCT FROM OLD.organization_id OR NEW.is_active IS DISTINCT FROM OLD.is_active OR NEW.keywords IS DISTINCT FROM OLD.keywords OR NEW.parent_project_id IS DISTINCT FROM OLD.parent_project_id THEN

      -- Insert the OLD state as a historical record
      INSERT INTO public.projects (name, status, context, description, created_at, is_active, parent_project_id, keywords, is_current, version, supersedes_id, organization_id)
      VALUES (OLD.name, OLD.status, OLD.context, OLD.description, OLD.created_at, OLD.is_active, OLD.parent_project_id, OLD.keywords, false, OLD.version, OLD.supersedes_id, OLD.organization_id)
      RETURNING id INTO archived_id;

      -- Update the NEW row to increment version and point to the historical record
      NEW.version = OLD.version + 1;
      NEW.supersedes_id = archived_id;
    END IF;
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_temporal_projects_update
BEFORE UPDATE ON public.projects
FOR EACH ROW
WHEN (pg_trigger_depth() = 0)
EXECUTE FUNCTION temporal_projects_update();

-- Trigger Function for resources (columns already exist)
CREATE OR REPLACE FUNCTION temporal_resources_update()
RETURNS TRIGGER AS $$
DECLARE
  archived_id int8;
BEGIN
  IF NEW.is_current = true THEN
    -- Only version if something material changed
    IF NEW.title IS DISTINCT FROM OLD.title OR NEW.summary IS DISTINCT FROM OLD.summary OR NEW.category IS DISTINCT FROM OLD.category OR NEW.url IS DISTINCT FROM OLD.url OR NEW.strategic_note IS DISTINCT FROM OLD.strategic_note OR NEW.project_id IS DISTINCT FROM OLD.project_id OR NEW.cluster_id IS DISTINCT FROM OLD.cluster_id OR NEW.organization_id IS DISTINCT FROM OLD.organization_id THEN

      -- Insert the OLD state as a historical record
      INSERT INTO public.resources (url, title, summary, category, project_id, created_at, strategic_note, cluster_id, enriched_at, embedding, is_current, version, supersedes_id, organization_id, dismissed_at)
      VALUES (OLD.url, OLD.title, OLD.summary, OLD.category, OLD.project_id, OLD.created_at, OLD.strategic_note, OLD.cluster_id, OLD.enriched_at, OLD.embedding, false, OLD.version, OLD.supersedes_id, OLD.organization_id, OLD.dismissed_at)
      RETURNING id INTO archived_id;

      -- Update the NEW row to increment version and point to the historical record
      NEW.version = OLD.version + 1;
      NEW.supersedes_id = archived_id;
    END IF;
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_temporal_resources_update
BEFORE UPDATE ON public.resources
FOR EACH ROW
WHEN (pg_trigger_depth() = 0)
EXECUTE FUNCTION temporal_resources_update();

-- ==============================================================
-- STEP 5: Migrate memories from app-level to trigger-based
-- ==============================================================

-- Trigger Function for memories
-- Includes backward-compat guard: if version was already bumped
-- by the old Python code (version_memory_for_update), skip.
CREATE OR REPLACE FUNCTION temporal_memories_update()
RETURNS TRIGGER AS $$
DECLARE
  archived_id int8;
BEGIN
  IF NEW.is_current = true THEN
    -- Backward-compat guard: old Python code already archived
    IF NEW.version IS DISTINCT FROM OLD.version THEN
      RETURN NEW;
    END IF;
    -- Only version if something material changed
    IF NEW.content IS DISTINCT FROM OLD.content OR NEW.memory_type IS DISTINCT FROM OLD.memory_type OR NEW.project_id IS DISTINCT FROM OLD.project_id OR NEW.organization_id IS DISTINCT FROM OLD.organization_id OR NEW.sentiment_score IS DISTINCT FROM OLD.sentiment_score OR NEW.entities_mentioned IS DISTINCT FROM OLD.entities_mentioned OR NEW.expires_at IS DISTINCT FROM OLD.expires_at OR NEW.importance_score IS DISTINCT FROM OLD.importance_score OR NEW.metadata IS DISTINCT FROM OLD.metadata THEN

      -- Insert the OLD state as a historical record
      INSERT INTO public.memories (content, metadata, embedding, created_at, memory_type, source, embedding_status, is_archived, archived_at, archive_reason, importance_score, last_accessed_at, supersedes_id, pruned, pruned_at, pruned_reason, superseded_by, is_current, version, project_id, sentiment_score, sentiment, entities_mentioned, expires_at, organization_id)
      VALUES (OLD.content, OLD.metadata, OLD.embedding, OLD.created_at, OLD.memory_type, OLD.source, OLD.embedding_status, OLD.is_archived, OLD.archived_at, OLD.archive_reason, OLD.importance_score, OLD.last_accessed_at, OLD.supersedes_id, OLD.pruned, OLD.pruned_at, OLD.pruned_reason, OLD.superseded_by, false, OLD.version, OLD.project_id, OLD.sentiment_score, OLD.sentiment, OLD.entities_mentioned, OLD.expires_at, OLD.organization_id)
      RETURNING id INTO archived_id;

      -- Update the NEW row to increment version and point to the historical record
      NEW.version = OLD.version + 1;
      NEW.supersedes_id = archived_id;
    END IF;
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_temporal_memories_update
BEFORE UPDATE ON public.memories
FOR EACH ROW
WHEN (pg_trigger_depth() = 0)
EXECUTE FUNCTION temporal_memories_update();
