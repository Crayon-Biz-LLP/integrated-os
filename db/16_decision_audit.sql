-- Phase 10: Decision Audit & "/why" Command
-- Adds last_decision_chain_id tracking to conversation_threads
-- A decision_chain_id is a UUID generated per webhook request, stored
-- on the thread so the user can ask "why" about the last bot response.

ALTER TABLE conversation_threads
ADD COLUMN IF NOT EXISTS last_decision_chain_id TEXT;

-- Index for fast lookup by chain_id across audit_logs
CREATE INDEX IF NOT EXISTS idx_audit_logs_decision_chain_id
ON audit_logs ((metadata->>'decision_chain_id'))
WHERE service = 'decision_audit';
