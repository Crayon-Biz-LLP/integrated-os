-- Migration: Create audit_logs table for Phase-3 hardening
-- Replaces printf for errors with permanent audit trail.

CREATE TABLE IF NOT EXISTS audit_logs (
    id BIGSERIAL PRIMARY KEY,
    service TEXT NOT NULL,        -- 'pulse', 'webhook', 'backfill_graph'
    level TEXT NOT NULL,          -- 'INFO', 'WARNING', 'ERROR', 'CRITICAL'
    message TEXT NOT NULL,
    metadata JSONB DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- Index for efficient querying
CREATE INDEX IF NOT EXISTS idx_audit_logs_service ON audit_logs(service, level);
CREATE INDEX IF NOT EXISTS idx_audit_logs_created ON audit_logs(created_at DESC);

-- Comments
COMMENT ON TABLE audit_logs IS 'Permanent audit trail for system errors and state transitions.';
COMMENT ON COLUMN audit_logs.service IS 'Which service: pulse, webhook, backfill_graph';
COMMENT ON COLUMN audit_logs.level IS 'INFO, WARNING, ERROR, CRITICAL';
COMMENT ON COLUMN audit_logs.message IS 'The log message (500 char limit in function)';
COMMENT ON COLUMN audit_logs.metadata IS 'Additional context: error stack, memory_id, etc.';
