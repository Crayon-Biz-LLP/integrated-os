-- Tier 5 Meta-Cognitive Learning Layer
-- Stores structured observations from every subsystem for pattern extraction.
-- Each observation records what the system predicted vs what Danny actually did.

CREATE TABLE IF NOT EXISTS subsystem_telemetry (
    id              BIGSERIAL PRIMARY KEY,
    subsystem       TEXT NOT NULL,          -- 'classification', 'entity_extraction', 'decision_pulse', etc.
    event_type      TEXT NOT NULL,          -- 'correction', 'approval', 'rejection', 'engagement', 'failure'
    features        JSONB NOT NULL DEFAULT '{}'::jsonb,
    predicted       JSONB,                 -- what the system predicted/produced
    actual          JSONB,                 -- what actually happened / Danny chose
    outcome         TEXT NOT NULL,          -- 'correct', 'corrected', 'confirmed', 'rejected', 'ignored', 'failed'
    confidence      REAL,                  -- system's confidence if applicable
    latency_ms      INTEGER,               -- operation duration if applicable
    session_id      TEXT,                  -- links to conversation thread
    source          TEXT,                  -- 'webhook', 'pulse', 'sentinel', etc.
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_telemetry_subsystem
    ON subsystem_telemetry(subsystem, outcome, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_telemetry_cleanup
    ON subsystem_telemetry(created_at);

-- Rolling pattern counters: per-subsystem, per-feature-hash frequency table.
-- Updated atomically on every emit_observation() call.
CREATE TABLE IF NOT EXISTS subsystem_patterns (
    id              SERIAL PRIMARY KEY,
    subsystem       TEXT NOT NULL,
    feature_hash    TEXT NOT NULL,
    feature_json    JSONB NOT NULL DEFAULT '{}'::jsonb,
    total_count     INTEGER DEFAULT 0,
    correct_count   INTEGER DEFAULT 0,
    corrected_count INTEGER DEFAULT 0,
    confidence      REAL DEFAULT 0.0,
    last_seen       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(subsystem, feature_hash)
);

CREATE INDEX IF NOT EXISTS idx_patterns_lookup
    ON subsystem_patterns(subsystem, feature_hash);

-- Cleanup: remove observations older than 90 days to keep table lean
-- Runs via the sentinel piggyback or manual maintenance
CREATE OR REPLACE FUNCTION cleanup_stale_telemetry()
RETURNS INTEGER
LANGUAGE plpgsql
AS $$
DECLARE
    deleted_count INTEGER;
BEGIN
    DELETE FROM subsystem_telemetry
    WHERE created_at < NOW() - INTERVAL '90 days';
    GET DIAGNOSTICS deleted_count = ROW_COUNT;
    RETURN deleted_count;
END;
$$;
