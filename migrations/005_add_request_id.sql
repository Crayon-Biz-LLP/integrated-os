-- Migration: Add request_id column to raw_dumps and processed_updates
-- Purpose: Enable idempotency tracking for duplicate prevention
-- Run this in Supabase Dashboard > SQL Editor

-- Add request_id to raw_dumps (if not exists)
ALTER TABLE raw_dumps 
ADD COLUMN IF NOT EXISTS request_id TEXT;

-- Index for quick idempotency lookups
CREATE INDEX IF NOT EXISTS idx_raw_dumps_request_id 
ON raw_dumps(request_id) 
WHERE request_id IS NOT NULL;

-- Ensure processed_updates has request_id (table creation is in 004)
ALTER TABLE processed_updates 
ADD COLUMN IF NOT EXISTS request_id TEXT;

-- Index for processed_updates (if not exists)
CREATE INDEX IF NOT EXISTS idx_processed_updates_request_id 
ON processed_updates(request_id) 
WHERE request_id IS NOT NULL;

-- Comments
COMMENT ON COLUMN raw_dumps.request_id IS 'Unique ID for idempotency (prevents duplicate processing)';
COMMENT ON COLUMN processed_updates.request_id IS 'Mirrors raw_dumps.request_id for double-idempotency';
