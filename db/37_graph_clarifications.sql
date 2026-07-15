-- Migration 32: Database-backed pending graph clarifications
-- Replaces the in-memory pending_graph_clarifications dict that
-- gets wiped on cold restart.
--
-- Usage:
--   INSERT INTO pending_graph_clarifications (chat_id, pending_id, step, type, label)
--   SELECT ... WHERE NOT EXISTS (SELECT 1 FROM pending_graph_clarifications WHERE ...)
--
--   SELECT * FROM pending_graph_clarifications
--   WHERE chat_id = <id> AND status = 'active' AND expires_at > now()
--
--   UPDATE pending_graph_clarifications SET resolved_at = now(), status = 'resolved'
--   WHERE id = <id>

CREATE TABLE IF NOT EXISTS pending_graph_clarifications (
    id              BIGSERIAL PRIMARY KEY,
    chat_id         BIGINT NOT NULL,
    pending_id      INTEGER NOT NULL,
    pending_type    TEXT NOT NULL DEFAULT 'node',  -- 'node' | 'edge'
    step            TEXT NOT NULL DEFAULT 'awaiting_person_context',  -- step name
    label           TEXT,
    context_json    JSONB DEFAULT '{}',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at      TIMESTAMPTZ NOT NULL DEFAULT (now() + interval '5 minutes'),
    claimed_at      TIMESTAMPTZ,   -- set when a sentinel/sweeper picks it up
    resolved_at     TIMESTAMPTZ,   -- set when clarification is answered
    status          TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'resolved', 'expired'))
);

CREATE INDEX IF NOT EXISTS idx_pgc_active_chat
    ON pending_graph_clarifications (chat_id, status)
    WHERE status = 'active';

CREATE INDEX IF NOT EXISTS idx_pgc_expires
    ON pending_graph_clarifications (expires_at)
    WHERE status = 'active' AND resolved_at IS NULL;

-- Cleanup expired clarifications
CREATE OR REPLACE FUNCTION cleanup_expired_clarifications()
RETURNS INTEGER
LANGUAGE plpgsql
AS $$
DECLARE
    cleaned INTEGER;
BEGIN
    UPDATE pending_graph_clarifications
    SET status = 'expired', resolved_at = now()
    WHERE status = 'active' AND expires_at < now();
    GET DIAGNOSTICS cleaned = ROW_COUNT;
    RETURN cleaned;
END;
$$;
