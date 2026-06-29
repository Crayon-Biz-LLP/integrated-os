-- Classifier Corrections table for feedback learning loop (C1)
-- Stores learned correction rules from FEEDBACK_OVERRIDE audit events.
-- These rules are injected into the classify_intent prompt as LEARNED CORRECTIONS.

CREATE TABLE IF NOT EXISTS classifier_corrections (
    id SERIAL PRIMARY KEY,
    text_pattern TEXT NOT NULL,
    old_intent TEXT NOT NULL,
    new_intent TEXT NOT NULL,
    count INTEGER DEFAULT 1,
    first_seen TIMESTAMPTZ DEFAULT NOW(),
    last_seen TIMESTAMPTZ DEFAULT NOW(),
    enabled BOOLEAN DEFAULT TRUE,
    created_by TEXT DEFAULT 'system'
);

-- Dedup guard: same pattern + old_intent + new_intent should be one row
CREATE UNIQUE INDEX IF NOT EXISTS idx_corrections_dedup
    ON classifier_corrections (text_pattern, old_intent, new_intent);

-- Fast lookup for enabled corrections
CREATE INDEX IF NOT EXISTS idx_corrections_enabled
    ON classifier_corrections (enabled) WHERE enabled = TRUE;

-- RLS: only service_role can access
ALTER TABLE classifier_corrections ENABLE ROW LEVEL SECURITY;

CREATE POLICY "service_role_all" ON classifier_corrections
    FOR ALL
    TO service_role
    USING (true)
    WITH CHECK (true);
