-- Migration: Create processed_updates table (clean slate approach)
-- Purpose: Track processed Telegram updates for idempotency (prevents duplicate processing)
-- Run this in Supabase Dashboard > SQL Editor

-- Drop table if it exists (ensures clean schema)
DROP TABLE IF EXISTS processed_updates CASCADE;

-- Create table with correct schema
CREATE TABLE processed_updates (
    id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    update_id BIGINT UNIQUE NOT NULL,
    chat_id BIGINT,
    processed_at TIMESTAMPTZ DEFAULT NOW(),
    request_id TEXT,
    metadata JSONB DEFAULT '{}'::jsonb
);

-- Indexes for quick lookups
CREATE INDEX idx_processed_updates_update_id 
ON processed_updates(update_id);

CREATE INDEX idx_processed_updates_request_id 
ON processed_updates(request_id) WHERE request_id IS NOT NULL;

-- Table comment
COMMENT ON TABLE processed_updates IS 'Tracks processed Telegram updates for idempotency';
