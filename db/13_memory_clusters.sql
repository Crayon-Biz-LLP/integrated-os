-- 13_memory_clusters.sql — Memory Clustering (M5)
-- Additive only: new tables, no changes to existing memories table.

-- ============================================================
-- 1. Cluster table (standalone, no FK on memories)
-- ============================================================

CREATE TABLE IF NOT EXISTS memory_clusters (
    id                  SERIAL PRIMARY KEY,
    name                TEXT,
    theme               TEXT,
    summary             TEXT,
    fingerprint         TEXT NOT NULL,          -- SHA256 hash for stability matching
    centroid_embedding  vector(768),
    memory_count        INT NOT NULL DEFAULT 0,
    quality_score       REAL NOT NULL DEFAULT 0.0,
    status              TEXT NOT NULL DEFAULT 'candidate'
                        CHECK (status IN ('candidate', 'active', 'superseded', 'archived')),
    merged_into_id      INT REFERENCES memory_clusters(id),
    superseded_by_id    INT REFERENCES memory_clusters(id),
    version             INT NOT NULL DEFAULT 1,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_memory_clusters_fingerprint ON memory_clusters(fingerprint);
CREATE INDEX IF NOT EXISTS idx_memory_clusters_status ON memory_clusters(status);
CREATE INDEX IF NOT EXISTS idx_memory_clusters_quality ON memory_clusters(quality_score DESC);

-- ============================================================
-- 2. Link table (additive, reversible)
-- ============================================================

CREATE TABLE IF NOT EXISTS memory_cluster_members (
    id              SERIAL PRIMARY KEY,
    memory_id       BIGINT NOT NULL,
    cluster_id      INT NOT NULL REFERENCES memory_clusters(id) ON DELETE CASCADE,
    score           REAL NOT NULL DEFAULT 1.0,
    source          TEXT NOT NULL DEFAULT 'graph'
                    CHECK (source IN ('graph', 'embedding', 'llm', 'manual')),
    is_primary      BOOLEAN NOT NULL DEFAULT FALSE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(memory_id, cluster_id)
);

CREATE INDEX IF NOT EXISTS idx_mcm_memory ON memory_cluster_members(memory_id);
CREATE INDEX IF NOT EXISTS idx_mcm_cluster ON memory_cluster_members(cluster_id);
CREATE INDEX IF NOT EXISTS idx_mcm_primary ON memory_cluster_members(is_primary) WHERE is_primary = TRUE;

-- ============================================================
-- 3. Audit log table for cluster runs
-- ============================================================

CREATE TABLE IF NOT EXISTS memory_cluster_runs (
    id                  SERIAL PRIMARY KEY,
    clusters_created    INT NOT NULL DEFAULT 0,
    clusters_reused     INT NOT NULL DEFAULT 0,
    clusters_superseded INT NOT NULL DEFAULT 0,
    orphans_count       INT NOT NULL DEFAULT 0,
    total_memories      INT NOT NULL DEFAULT 0,
    seeds_processed     INT NOT NULL DEFAULT 0,
    quality_histogram   JSONB DEFAULT '{}',    -- { '<0.5': n, '0.5-0.7': n, '0.7-0.9': n, '>0.9': n }
    status              TEXT NOT NULL DEFAULT 'completed'
                        CHECK (status IN ('running', 'completed', 'failed')),
    error               TEXT,
    started_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at        TIMESTAMPTZ
);

-- ============================================================
-- 4. Updated_at trigger
-- ============================================================

CREATE OR REPLACE FUNCTION update_memory_clusters_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS memory_clusters_updated_at ON memory_clusters;
CREATE TRIGGER memory_clusters_updated_at
    BEFORE UPDATE ON memory_clusters
    FOR EACH ROW
    EXECUTE FUNCTION update_memory_clusters_updated_at();

-- ============================================================
-- 5. RLS: Only service_role can access
-- ============================================================

ALTER TABLE memory_clusters ENABLE ROW LEVEL SECURITY;
CREATE POLICY "service_role_full_access" ON memory_clusters
    FOR ALL USING (auth.role() = 'service_role');

ALTER TABLE memory_cluster_members ENABLE ROW LEVEL SECURITY;
CREATE POLICY "service_role_full_access" ON memory_cluster_members
    FOR ALL USING (auth.role() = 'service_role');

ALTER TABLE memory_cluster_runs ENABLE ROW LEVEL SECURITY;
CREATE POLICY "service_role_full_access" ON memory_cluster_runs
    FOR ALL USING (auth.role() = 'service_role');

COMMENT ON TABLE memory_clusters IS 'M5: Memory clustering — groups related memories by entity connections and semantic similarity. Additive only, no FK on memories.';
COMMENT ON TABLE memory_cluster_members IS 'M5: Link table — many-to-many between memories and clusters. Score = membership strength. Source = clustering method.';
COMMENT ON TABLE memory_cluster_runs IS 'M5: Audit log for weekly clustering runs. Tracks created/reused/superseded clusters, orphan count, quality histogram.';
