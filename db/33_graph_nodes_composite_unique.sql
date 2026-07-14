-- db/33_graph_nodes_composite_unique.sql
-- Replaces the UNIQUE(normalized_label) partial index with a full
-- UNIQUE(normalized_label, type) constraint, allowing the same label
-- to coexist as different types (e.g. "Solvstrat" org + "Solvstrat" project).
--
-- Also nullifies normalized_label on archived rows (via trigger) since
-- the column is a computed helper with no archival business value.
--
-- Drops the zombie UNIQUE(label) constraint (from db/28) that would
-- otherwise block this fix.
-- Drops the NOT NULL constraint on normalized_column (from db/22) so
-- archived rows can set it to NULL.
--
-- CRITICAL ORDERING: backfill of archived rows happens BEFORE the
-- UNIQUE constraint creation. The constraint applies to ALL rows (no
-- WHERE clause), and archived rows carry non-null normalized_labels
-- from before this migration.

-- ==============================================================
-- STEP 1: Drop all blocking constraints and indexes
-- ==============================================================

ALTER TABLE graph_nodes DROP CONSTRAINT IF EXISTS graph_nodes_label_key;
DROP INDEX IF EXISTS idx_graph_nodes_label_ci;
DROP INDEX IF EXISTS unique_graph_nodes_normalized_label;

-- ==============================================================
-- STEP 2: Create non-unique indexes for query performance
-- ==============================================================

CREATE INDEX IF NOT EXISTS idx_graph_nodes_label_ci
  ON graph_nodes (lower(label));

-- ==============================================================
-- STEP 3: Drop NOT NULL on normalized_label
-- ==============================================================
-- normalized_label is a computed helper column (LOWER(TRIM(label))).
-- It's always populated for active rows at insert time, but archived
-- rows need NULL to avoid colliding with active rows under the new
-- full UNIQUE constraint.

ALTER TABLE graph_nodes ALTER COLUMN normalized_label DROP NOT NULL;

-- ==============================================================
-- STEP 4: Backfill — nullify normalized_label on existing archived rows
-- ==============================================================
-- THIS MUST HAPPEN BEFORE the UNIQUE constraint creation.

UPDATE graph_nodes SET normalized_label = NULL
WHERE is_current = FALSE AND normalized_label IS NOT NULL;

-- ==============================================================
-- STEP 5: Create composite unique constraint
-- ==============================================================
-- FULL constraint (no WHERE clause!) — compatible with PostgREST's
-- on_conflict="normalized_label, type". Two rows with same
-- normalized_label but different type are allowed. Archived rows
-- have normalized_label=NULL which is distinct from all values.

ALTER TABLE graph_nodes ADD CONSTRAINT unique_graph_nodes_normalized_label_type
  UNIQUE (normalized_label, type);

-- ==============================================================
-- STEP 6: Modify temporal trigger to nullify normalized_label on archive
-- ==============================================================

CREATE OR REPLACE FUNCTION temporal_graph_nodes_update()
RETURNS TRIGGER AS $$
DECLARE
  archived_id uuid;
BEGIN
  IF NEW.is_current = true THEN
    IF NEW.label IS DISTINCT FROM OLD.label OR NEW.type IS DISTINCT FROM OLD.type OR NEW.metadata IS DISTINCT FROM OLD.metadata OR NEW.epistemic_status IS DISTINCT FROM OLD.epistemic_status OR NEW.canonical_id IS DISTINCT FROM OLD.canonical_id OR NEW.db_record_id IS DISTINCT FROM OLD.db_record_id OR NEW.normalized_label IS DISTINCT FROM OLD.normalized_label THEN

      INSERT INTO public.graph_nodes (label, type, metadata, embedding, canonical_page_id, canonical_id, created_at, epistemic_status, reference_count, last_referenced_at, db_record_id, normalized_label, is_current, version, supersedes_id)
      VALUES (OLD.label, OLD.type, OLD.metadata, OLD.embedding, OLD.canonical_page_id, OLD.canonical_id, OLD.created_at, OLD.epistemic_status, OLD.reference_count, OLD.last_referenced_at, OLD.db_record_id, NULL, false, OLD.version, OLD.supersedes_id)
      RETURNING id INTO archived_id;

      NEW.version = OLD.version + 1;
      NEW.supersedes_id = archived_id;
    END IF;
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;
