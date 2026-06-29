-- 10_decisions.sql — Structured Decision Log
-- Tracks explicit choices with lifecycle (active/superseded/reversed),
-- rationale, context, and entity links.

CREATE TABLE IF NOT EXISTS decisions (
    id              SERIAL PRIMARY KEY,
    decision_type   TEXT NOT NULL,          -- 'task_assignment', 'project_direction', 'resource_allocation', 'strategy', 'hiring', 'financial', 'technical', 'other'
    title           TEXT NOT NULL,          -- Short description of the decision
    context         TEXT,                   -- What prompted this decision (raw text or summary)
    rationale       TEXT,                   -- Why this decision was made
    entity_type     TEXT,                   -- 'task', 'project', 'organization', 'person', 'resource'
    entity_id       TEXT,                   -- FK to the relevant table (polymorphic)
    organization_id INTEGER,                -- FK to organizations
    project_id      INTEGER,                -- FK to projects
    status          TEXT NOT NULL DEFAULT 'active',  -- 'active', 'superseded', 'reversed', 'expired'
    superseded_by   INTEGER REFERENCES decisions(id),
    confidence      REAL DEFAULT 1.0,       -- 0.0 to 1.0, how confident we are in this decision
    source          TEXT DEFAULT 'manual',  -- 'manual', 'ai_suggested', 'approval', 'clarification'
    source_ref      TEXT,                   -- Reference to source (e.g. 'e123', 'g45', 'pe67')
    decided_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at      TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Indexes for common queries
CREATE INDEX IF NOT EXISTS idx_decisions_status ON decisions(status) WHERE status = 'active';
CREATE INDEX IF NOT EXISTS idx_decisions_entity ON decisions(entity_type, entity_id);
CREATE INDEX IF NOT EXISTS idx_decisions_project ON decisions(project_id) WHERE project_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_decisions_org ON decisions(organization_id) WHERE organization_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_decisions_type ON decisions(decision_type);
CREATE INDEX IF NOT EXISTS idx_decisions_decided ON decisions(decided_at DESC);

-- Updated_at trigger
CREATE OR REPLACE FUNCTION update_decisions_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS decisions_updated_at ON decisions;
CREATE TRIGGER decisions_updated_at
    BEFORE UPDATE ON decisions
    FOR EACH ROW
    EXECUTE FUNCTION update_decisions_updated_at();

-- RLS: Only service_role can access
ALTER TABLE decisions ENABLE ROW LEVEL SECURITY;
CREATE POLICY "service_role_full_access" ON decisions
    FOR ALL
    USING (auth.role() = 'service_role');

COMMENT ON TABLE decisions IS 'Structured decision log — tracks explicit choices with rationale, reversibility, and entity links. Replaces implicit decision tracking via audit_logs.';
