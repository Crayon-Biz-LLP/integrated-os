-- ============================================================
-- Migration 34: Separate pending_graph_nodes into two tables
--               + add deleted_at to people (replace text markers)
-- ============================================================

-- ── Part 1: pending_nodes (node creation approvals) ──
CREATE TABLE IF NOT EXISTS pending_nodes (
    id              BIGSERIAL PRIMARY KEY,
    label           TEXT NOT NULL,
    node_type       TEXT NOT NULL CHECK (node_type IN (
                        'person', 'organization', 'project',
                        'concept', 'place', 'event', 'animal',
                        'emotional_state', 'practice'
                    )),
    source_text     TEXT DEFAULT '',
    context         TEXT,
    eval_context    JSONB DEFAULT '{}'::jsonb,
    status          TEXT NOT NULL DEFAULT 'pending'
                        CHECK (status IN (
                            'pending', 'approved', 'rejected',
                            'awaiting_details', 'flagged', 'merged'
                        )),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    resolved_at     TIMESTAMPTZ,
    clarification_id TEXT,
    origin_table    TEXT,  -- 'pending_graph_nodes' for migrated rows
    origin_id       BIGINT  -- original id in pending_graph_nodes
);

CREATE INDEX IF NOT EXISTS idx_pending_nodes_status
    ON pending_nodes (status);
CREATE INDEX IF NOT EXISTS idx_pending_nodes_label
    ON pending_nodes (label);
CREATE INDEX IF NOT EXISTS idx_pending_nodes_type
    ON pending_nodes (node_type);

COMMENT ON TABLE pending_nodes IS
    'Holds new graph node creation requests awaiting approval. '
    'Replaces pending_graph_nodes for node-creation concerns.';

-- ── Part 2: merge_proposals (merge source→target approvals) ──
CREATE TABLE IF NOT EXISTS merge_proposals (
    id              BIGSERIAL PRIMARY KEY,
    source_label    TEXT NOT NULL,
    source_type     TEXT,
    source_node_id  UUID REFERENCES graph_nodes(id) ON DELETE SET NULL,
    target_node_id  UUID NOT NULL REFERENCES graph_nodes(id) ON DELETE CASCADE,
    target_label    TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'proposed'
                        CHECK (status IN (
                            'proposed', 'accepted', 'rejected'
                        )),
    rationale       TEXT DEFAULT '',
    proposed_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    resolved_at     TIMESTAMPTZ,
    origin_table    TEXT,  -- 'pending_graph_nodes' for migrated rows
    origin_id       BIGINT
);

CREATE INDEX IF NOT EXISTS idx_merge_proposals_status
    ON merge_proposals (status);
CREATE INDEX IF NOT EXISTS idx_merge_proposals_source
    ON merge_proposals (source_label);

COMMENT ON TABLE merge_proposals IS
    'Holds merge proposals (source → target) awaiting approval. '
    'Replaces merge_proposed status in pending_graph_nodes.';

-- ── Part 3: Add deleted_at to people (replace [DELETED] markers) ──
ALTER TABLE people
    ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS idx_people_deleted_at
    ON people (deleted_at);

COMMENT ON COLUMN people.deleted_at IS
    'Set when a person record is deleted. Replaces [DELETED] text markers in role column. '
    'NULL = active, non-NULL = deleted at that timestamp.';
