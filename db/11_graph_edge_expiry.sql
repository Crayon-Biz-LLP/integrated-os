-- 11_graph_edge_expiry.sql — Graph Edge Expiry (TF-002)
-- Adds last_confirmed_at and valid_until to graph_edges
-- to prevent stale relationship poisoning.

-- ============================================================
-- 1. Add expiry columns
-- ============================================================

ALTER TABLE graph_edges ADD COLUMN IF NOT EXISTS last_confirmed_at TIMESTAMPTZ;
ALTER TABLE graph_edges ADD COLUMN IF NOT EXISTS valid_until TIMESTAMPTZ;

-- Indexes for efficient filtering
CREATE INDEX IF NOT EXISTS idx_graph_edges_valid_until ON graph_edges(valid_until) WHERE valid_until IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_graph_edges_last_confirmed ON graph_edges(last_confirmed_at DESC) WHERE last_confirmed_at IS NOT NULL;

-- ============================================================
-- 2. Backfill: set last_confirmed_at on existing edges from metadata timestamps
-- ============================================================

UPDATE graph_edges
SET last_confirmed_at = created_at
WHERE last_confirmed_at IS NULL;

-- ============================================================
-- 3. Helper function: confirm an edge (update last_confirmed_at)
-- ============================================================

CREATE OR REPLACE FUNCTION confirm_graph_edge(edge_id UUID)
RETURNS VOID AS $$
BEGIN
    UPDATE graph_edges
    SET last_confirmed_at = NOW()
    WHERE id = edge_id;
END;
$$ LANGUAGE plpgsql;

-- ============================================================
-- 4. Helper function: expire stale edges (called by sentinel)
-- ============================================================

CREATE OR REPLACE FUNCTION expire_stale_graph_edges(expiry_days INTEGER DEFAULT 90)
RETURNS INTEGER AS $$
DECLARE
    expired_count INTEGER;
BEGIN
    UPDATE graph_edges
    SET metadata = jsonb_set(
        COALESCE(metadata, '{}'::jsonb),
        '{expired}',
        'true'
    )
    WHERE (valid_until IS NOT NULL AND valid_until < NOW())
      AND (metadata->>'expired' IS DISTINCT FROM 'true');

    GET DIAGNOSTICS expired_count = ROW_COUNT;
    RETURN expired_count;
END;
$$ LANGUAGE plpgsql;

COMMENT ON COLUMN graph_edges.last_confirmed_at IS 'Last time this edge was confirmed by user action or AI inference. Used for staleness detection.';
COMMENT ON COLUMN graph_edges.valid_until IS 'Optional expiry date. After this date, the edge is considered stale and excluded from active queries.';
