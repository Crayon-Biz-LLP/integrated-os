-- Migration 017: Add conversations table
-- Purpose: Structured exchange logging for conversational Q&A sessions
-- Run this in Supabase Dashboard > SQL Editor

CREATE TABLE IF NOT EXISTS conversations (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    chat_id BIGINT NOT NULL,
    session_id UUID NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('user', 'bot')),
    intent TEXT,
    content TEXT NOT NULL,
    token_count INT DEFAULT 0,
    metadata JSONB DEFAULT '{}',
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_conversations_session
ON conversations(session_id, created_at);

CREATE INDEX IF NOT EXISTS idx_conversations_chat_time
ON conversations(chat_id, created_at DESC);

COMMENT ON TABLE conversations IS 'Session-based conversation history for Rhodey Q&A';
COMMENT ON COLUMN conversations.session_id IS 'UUIDv4 generated per session (5min timeout)';
COMMENT ON COLUMN conversations.token_count IS 'Approximate token count for context window truncation';
COMMENT ON COLUMN conversations.metadata IS 'Flags like {"sent": false} if send_telegram failed';
