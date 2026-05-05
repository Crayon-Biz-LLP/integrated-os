-- Migration: Add sender and message_type columns to raw_dumps
-- Purpose: Track who sent each message and what type it is

-- Add sender column (if not exists)
ALTER TABLE raw_dumps 
ADD COLUMN IF NOT EXISTS sender TEXT DEFAULT 'telegram';

-- Add message_type column (if not exists)
ALTER TABLE raw_dumps 
ADD COLUMN IF NOT EXISTS message_type TEXT DEFAULT 'task';

-- Add indexes for faster queries
CREATE INDEX IF NOT EXISTS idx_raw_dumps_sender 
ON raw_dumps(sender, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_raw_dumps_message_type 
ON raw_dumps(message_type, created_at DESC);

-- Update existing records
UPDATE raw_dumps 
SET sender = 'telegram' 
WHERE sender IS NULL;

UPDATE raw_dumps 
SET message_type = 'task' 
WHERE message_type IS NULL;

-- Add comments
COMMENT ON COLUMN raw_dumps.sender IS 'Who sent the message: user (web UI), telegram (incoming), system (pulse/briefing)';
COMMENT ON COLUMN raw_dumps.message_type IS 'Type of message: chat, task, briefing, system';
