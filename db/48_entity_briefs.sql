-- B3: Pre-computed Entity Briefs
-- Stores 400-char compressed snapshots per entity, refreshed every 5min by sentinel.
-- The query path reads this table after anaphora resolution — if a fresh brief exists,
-- it replaces the entire 17-section context assembly for entity-anchored status queries.

CREATE TABLE IF NOT EXISTS entity_briefs (
    entity_name     TEXT PRIMARY KEY,
    entity_type     TEXT,             -- 'organization' | 'project' | 'person'
    brief_text      TEXT NOT NULL,
    open_task_count INT DEFAULT 0,
    updated_at      TIMESTAMPTZ DEFAULT NOW()
);

-- Index for freshness checks (most common query: get brief by name if fresh)
CREATE INDEX IF NOT EXISTS idx_entity_briefs_updated_at ON entity_briefs (updated_at DESC);

-- Grant to service_role
GRANT ALL ON entity_briefs TO service_role;
