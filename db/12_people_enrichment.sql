-- 12_people_enrichment.sql — People Table Enrichment (TF-003)
-- Populates org, last_interaction_date, notes from graph edges.

-- ============================================================
-- 1. Add enrichment columns to people table (if not present)
-- ============================================================

ALTER TABLE people ADD COLUMN IF NOT EXISTS organization_name TEXT;
ALTER TABLE people ADD COLUMN IF NOT EXISTS last_interaction_date TIMESTAMPTZ;
ALTER TABLE people ADD COLUMN IF NOT EXISTS enrichment_notes TEXT;
ALTER TABLE people ADD COLUMN IF NOT EXISTS enriched_at TIMESTAMPTZ;

-- Index for efficient filtering
CREATE INDEX IF NOT EXISTS idx_people_last_interaction ON people(last_interaction_date DESC) WHERE last_interaction_date IS NOT NULL;

-- ============================================================
-- 2. Helper function: enrich a person from graph edges
-- ============================================================

CREATE OR REPLACE FUNCTION enrich_person_from_edges(person_id BIGINT)
RETURNS VOID AS $$
DECLARE
    person_node_id UUID;
    last_edge_time TIMESTAMPTZ;
    org_label TEXT;
BEGIN
    -- Find the graph_nodes entry for this person
    SELECT gn.id INTO person_node_id
    FROM graph_nodes gn
    WHERE gn.type = 'person'
      AND (gn.metadata->>'people_id')::TEXT = person_id::TEXT
    LIMIT 1;

    IF person_node_id IS NULL THEN
        RETURN;
    END IF;

    -- Find most recent edge involving this person
    SELECT MAX(e.created_at) INTO last_edge_time
    FROM graph_edges e
    WHERE e.source_node_id = person_node_id OR e.target_node_id = person_node_id;

    -- Find organization from MEMBER_OF edges
    SELECT t.label INTO org_label
    FROM graph_edges e
    JOIN graph_nodes t ON (
        (e.target_node_id = t.id AND e.source_node_id = person_node_id)
        OR (e.source_node_id = t.id AND e.target_node_id = person_node_id)
    )
    WHERE e.relationship = 'MEMBER_OF' AND t.type = 'organization'
    LIMIT 1;

    -- Update the people row
    UPDATE people
    SET
        last_interaction_date = COALESCE(last_edge_time, last_interaction_date),
        organization_name = COALESCE(org_label, organization_name),
        enriched_at = NOW()
    WHERE id = person_id;
END;
$$ LANGUAGE plpgsql;

-- ============================================================
-- 3. Bulk enrich all people (one-time backfill)
-- ============================================================

-- Uncomment to run backfill:
-- SELECT enrich_person_from_edges(id) FROM people;
