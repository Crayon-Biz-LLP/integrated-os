-- Migration 47: Graph-Primary Entity Sync Triggers
-- 
-- Enforces the architectural invariant: graph_nodes is the canonical source of
-- truth for entity IDENTITY. Domain tables (people, projects, organizations)
-- are metadata extensions that follow automatically.
--
-- Three triggers:
--   1. INSERT:  When a person/project/org graph_node is created, auto-create
--               the corresponding domain row if one doesn't already exist.
--   2. DELETE:  When a person/project/org graph_node is hard-deleted, archive
--               the corresponding domain row (is_current = false).
--   3. SET:     When is_current changes from true→false or type changes,
--               archive old domain row and/or create new domain row.
--
-- The application function create_graph_node_with_db_record() still writes to
-- both tables directly. These triggers are the safety net for any path that
-- bypasses it (entity extraction, corrections, future code).

-- ──────────────────────────────────────────────────────────────────
-- Helper function: match a graph_node label to an existing domain row
-- ──────────────────────────────────────────────────────────────────
CREATE OR REPLACE FUNCTION resolve_domain_id(
    p_label TEXT,
    p_type TEXT
) RETURNS UUID AS $$
DECLARE
    v_id UUID;
BEGIN
    IF p_type = 'person' THEN
        -- Try exact match first
        SELECT id INTO v_id FROM people
        WHERE LOWER(TRIM(name)) = LOWER(TRIM(p_label))
          AND is_current = true
        LIMIT 1;
        -- Fallback: ILIKE
        IF v_id IS NULL THEN
            SELECT id INTO v_id FROM people
            WHERE name ILIKE p_label
              AND is_current = true
            LIMIT 1;
        END IF;

    ELSIF p_type = 'project' THEN
        SELECT id INTO v_id FROM projects
        WHERE LOWER(TRIM(name)) = LOWER(TRIM(p_label))
          AND is_current = true
        LIMIT 1;
        IF v_id IS NULL THEN
            SELECT id INTO v_id FROM projects
            WHERE name ILIKE p_label
              AND is_current = true
            LIMIT 1;
        END IF;

    ELSIF p_type = 'organization' THEN
        SELECT id INTO v_id FROM organizations
        WHERE LOWER(TRIM(name)) = LOWER(TRIM(p_label))
          AND is_active = true
        LIMIT 1;
        IF v_id IS NULL THEN
            SELECT id INTO v_id FROM organizations
            WHERE name ILIKE p_label
              AND is_active = true
            LIMIT 1;
        END IF;
    END IF;

    RETURN v_id;
END;
$$ LANGUAGE plpgsql;

-- ──────────────────────────────────────────────────────────────────
-- Helper function: create or update domain row from graph_node
-- ──────────────────────────────────────────────────────────────────
CREATE OR REPLACE FUNCTION sync_domain_row_from_graph_node()
RETURNS TRIGGER AS $$
DECLARE
    v_domain_id UUID;
    v_exists BOOLEAN;
BEGIN
    -- Skip if not an entity type that has a domain table
    IF NEW.type NOT IN ('person', 'project', 'organization') THEN
        RETURN NEW;
    END IF;

    -- Skip if db_record_id is already set AND valid
    IF NEW.db_record_id IS NOT NULL THEN
        IF NEW.type = 'person' THEN
            SELECT EXISTS(SELECT 1 FROM people WHERE id = NEW.db_record_id::uuid) INTO v_exists;
        ELSIF NEW.type = 'project' THEN
            SELECT EXISTS(SELECT 1 FROM projects WHERE id = NEW.db_record_id::uuid) INTO v_exists;
        ELSIF NEW.type = 'organization' THEN
            SELECT EXISTS(SELECT 1 FROM organizations WHERE id = NEW.db_record_id::uuid) INTO v_exists;
        END IF;
        IF v_exists THEN
            RETURN NEW;  -- Domain row already exists, nothing to do
        END IF;
    END IF;

    -- Try to match by label first
    v_domain_id := resolve_domain_id(NEW.label, NEW.type);

    -- If no match found, create a new domain row
    IF v_domain_id IS NULL THEN
        IF NEW.type = 'person' THEN
            INSERT INTO people (name, source, strategic_weight, is_current)
            VALUES (NEW.label, 'graph_trigger', 5, true)
            RETURNING id INTO v_domain_id;
        ELSIF NEW.type = 'project' THEN
            INSERT INTO projects (name, status, is_active, is_current)
            VALUES (NEW.label, 'active', true, true)
            RETURNING id INTO v_domain_id;
        ELSIF NEW.type = 'organization' THEN
            INSERT INTO organizations (name, is_active)
            VALUES (NEW.label, true)
            RETURNING id INTO v_domain_id;
        END IF;
    END IF;

    -- Back-fill db_record_id on the graph_node if it wasn't set
    IF NEW.db_record_id IS NULL AND v_domain_id IS NOT NULL THEN
        UPDATE graph_nodes
        SET db_record_id = v_domain_id::text
        WHERE id = NEW.id;
    END IF;

    -- Back-fill graph_node_id on the domain table
    IF v_domain_id IS NOT NULL THEN
        IF NEW.type = 'person' THEN
            UPDATE people SET graph_node_id = NEW.id WHERE id = v_domain_id;
        ELSIF NEW.type = 'organization' THEN
            UPDATE organizations SET graph_node_id = NEW.id WHERE id = v_domain_id;
        END IF;
        -- Projects don't have graph_node_id column — skip
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- ──────────────────────────────────────────────────────────────────
-- Helper function: archive domain row when graph_node is removed
-- ──────────────────────────────────────────────────────────────────
CREATE OR REPLACE FUNCTION archive_domain_row_on_graph_node_remove()
RETURNS TRIGGER AS $$
DECLARE
    v_domain_id UUID;
BEGIN
    IF OLD.type NOT IN ('person', 'project', 'organization') THEN
        RETURN OLD;
    END IF;

    -- Find the domain row: by db_record_id, by graph_node_id, or by label
    IF OLD.db_record_id IS NOT NULL THEN
        v_domain_id := OLD.db_record_id::uuid;
    ELSE
        -- Use graph_node_id back-link on domain tables
        IF OLD.type = 'person' THEN
            SELECT id INTO v_domain_id FROM people WHERE graph_node_id = OLD.id AND is_current = true;
        ELSIF OLD.type = 'organization' THEN
            SELECT id INTO v_domain_id FROM organizations WHERE graph_node_id = OLD.id AND is_active = true;
        END IF;
        -- Fallback: match by label
        IF v_domain_id IS NULL THEN
            v_domain_id := resolve_domain_id(OLD.label, OLD.type);
        END IF;
    END IF;

    -- Archive the domain row
    IF v_domain_id IS NOT NULL THEN
        IF OLD.type = 'person' THEN
            UPDATE people SET is_current = false WHERE id = v_domain_id AND is_current = true;
        ELSIF OLD.type = 'project' THEN
            UPDATE projects SET is_current = false WHERE id = v_domain_id AND is_current = true;
        ELSIF OLD.type = 'organization' THEN
            UPDATE organizations SET is_active = false WHERE id = v_domain_id AND is_active = true;
        END IF;
    END IF;

    RETURN OLD;
END;
$$ LANGUAGE plpgsql;

-- ──────────────────────────────────────────────────────────────────
-- Helper function: migrate domain rows when graph_node type changes
-- ──────────────────────────────────────────────────────────────────
CREATE OR REPLACE FUNCTION migrate_domain_on_type_change()
RETURNS TRIGGER AS $$
DECLARE
    v_old_domain_id UUID;
    v_new_domain_id UUID;
BEGIN
    IF OLD.type NOT IN ('person', 'project', 'organization') AND
       NEW.type NOT IN ('person', 'project', 'organization') THEN
        RETURN NEW;
    END IF;
    IF OLD.type = NEW.type THEN
        RETURN NEW;
    END IF;

    -- Step 1: Archive the old domain row
    IF OLD.type IN ('person', 'project', 'organization') THEN
        IF OLD.db_record_id IS NOT NULL THEN
            v_old_domain_id := OLD.db_record_id::uuid;
        ELSE
            v_old_domain_id := resolve_domain_id(OLD.label, OLD.type);
        END IF;

        IF v_old_domain_id IS NOT NULL THEN
            IF OLD.type = 'person' THEN
                UPDATE people SET is_current = false WHERE id = v_old_domain_id;
            ELSIF OLD.type = 'project' THEN
                UPDATE projects SET is_current = false WHERE id = v_old_domain_id;
            ELSIF OLD.type = 'organization' THEN
                UPDATE organizations SET is_active = false WHERE id = v_old_domain_id;
            END IF;
        END IF;
    END IF;

    -- Step 2: Create the new domain row
    IF NEW.type IN ('person', 'project', 'organization') THEN
        IF NEW.db_record_id IS NOT NULL THEN
            -- Check if valid
            IF NEW.type = 'person' THEN
                SELECT EXISTS(SELECT 1 FROM people WHERE id = NEW.db_record_id::uuid AND is_current = true) INTO v_new_domain_id;
            ELSIF NEW.type = 'project' THEN
                SELECT EXISTS(SELECT 1 FROM projects WHERE id = NEW.db_record_id::uuid AND is_current = true) INTO v_new_domain_id;
            ELSIF NEW.type = 'organization' THEN
                SELECT EXISTS(SELECT 1 FROM organizations WHERE id = NEW.db_record_id::uuid AND is_active = true) INTO v_new_domain_id;
            END IF;
            IF v_new_domain_id IS NOT NULL THEN
                RETURN NEW;  -- Already has a valid domain row
            END IF;
        END IF;

        v_new_domain_id := resolve_domain_id(NEW.label, NEW.type);

        IF v_new_domain_id IS NULL THEN
            IF NEW.type = 'person' THEN
                INSERT INTO people (name, source, strategic_weight, is_current)
                VALUES (NEW.label, 'graph_trigger_migration', 5, true)
                RETURNING id INTO v_new_domain_id;
            ELSIF NEW.type = 'project' THEN
                INSERT INTO projects (name, status, is_active, is_current)
                VALUES (NEW.label, 'active', true, true)
                RETURNING id INTO v_new_domain_id;
            ELSIF NEW.type = 'organization' THEN
                INSERT INTO organizations (name, is_active)
                VALUES (NEW.label, true)
                RETURNING id INTO v_new_domain_id;
            END IF;
        END IF;

        -- Update db_record_id
        IF v_new_domain_id IS NOT NULL AND NEW.db_record_id IS NULL THEN
            UPDATE graph_nodes SET db_record_id = v_new_domain_id::text WHERE id = NEW.id;
        END IF;

        -- Update graph_node_id back-link
        IF v_new_domain_id IS NOT NULL THEN
            IF NEW.type = 'person' THEN
                UPDATE people SET graph_node_id = NEW.id WHERE id = v_new_domain_id;
            ELSIF NEW.type = 'organization' THEN
                UPDATE organizations SET graph_node_id = NEW.id WHERE id = v_new_domain_id;
            END IF;
        END IF;
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- ──────────────────────────────────────────────────────────────────
-- Install the triggers
-- ──────────────────────────────────────────────────────────────────

-- Trigger 1: INSERT → auto-create domain row
DROP TRIGGER IF EXISTS trg_graph_node_insert_sync_domain ON graph_nodes;
CREATE TRIGGER trg_graph_node_insert_sync_domain
  AFTER INSERT ON graph_nodes
  FOR EACH ROW
  WHEN (NEW.type IN ('person', 'project', 'organization'))
  EXECUTE FUNCTION sync_domain_row_from_graph_node();

-- Trigger 2: DELETE → archive domain row
DROP TRIGGER IF EXISTS trg_graph_node_delete_archive_domain ON graph_nodes;
CREATE TRIGGER trg_graph_node_delete_archive_domain
  AFTER DELETE ON graph_nodes
  FOR EACH ROW
  WHEN (OLD.type IN ('person', 'project', 'organization'))
  EXECUTE FUNCTION archive_domain_row_on_graph_node_remove();

-- Trigger 3: SET is_current = false → archive domain row (soft delete)
DROP TRIGGER IF EXISTS trg_graph_node_soft_delete_archive_domain ON graph_nodes;
CREATE TRIGGER trg_graph_node_soft_delete_archive_domain
  AFTER UPDATE OF is_current ON graph_nodes
  FOR EACH ROW
  WHEN (OLD.is_current = true AND NEW.is_current = false
        AND OLD.type IN ('person', 'project', 'organization'))
  EXECUTE FUNCTION archive_domain_row_on_graph_node_remove();

-- Trigger 4: UPDATE type → migrate domain rows
DROP TRIGGER IF EXISTS trg_graph_node_type_change_migrate_domain ON graph_nodes;
CREATE TRIGGER trg_graph_node_type_change_migrate_domain
  AFTER UPDATE OF type ON graph_nodes
  FOR EACH ROW
  WHEN (OLD.type IS DISTINCT FROM NEW.type
        AND (OLD.type IN ('person', 'project', 'organization')
             OR NEW.type IN ('person', 'project', 'organization')))
  EXECUTE FUNCTION migrate_domain_on_type_change();

-- ──────────────────────────────────────────────────────────────────
-- Verify the triggers are installed
-- ──────────────────────────────────────────────────────────────────
DO $$
DECLARE
    trigger_count INTEGER;
BEGIN
    SELECT COUNT(*) INTO trigger_count
    FROM pg_trigger
    WHERE tgname IN (
        'trg_graph_node_insert_sync_domain',
        'trg_graph_node_delete_archive_domain',
        'trg_graph_node_soft_delete_archive_domain',
        'trg_graph_node_type_change_migrate_domain'
    );
    IF trigger_count < 4 THEN
        RAISE WARNING 'Only % of 4 triggers installed. Check for errors above.', trigger_count;
    ELSE
        RAISE NOTICE 'All 4 graph_node sync triggers installed successfully.';
    END IF;
END $$;

-- ──────────────────────────────────────────────────────────────────
-- Clean up helper functions (they stay installed for DB operations)
-- Note: Functions are intentionally NOT dropped — they remain available
-- for manual use and debugging.
-- ──────────────────────────────────────────────────────────────────
