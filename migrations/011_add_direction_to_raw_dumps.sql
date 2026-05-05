-- Migration: Add direction column to raw_dumps
-- Purpose: Track incoming vs outgoing messages for the messaging interface

-- Add direction column (if not exists)
ALTER TABLE raw_dumps 
ADD COLUMN IF NOT EXISTS direction TEXT DEFAULT 'incoming';

-- Add index for faster queries
CREATE INDEX IF NOT EXISTS idx_raw_dumps_direction 
ON raw_dumps(direction, created_at DESC);

-- Update existing records to have 'incoming' as default
UPDATE raw_dumps 
SET direction = 'incoming' 
WHERE direction IS NULL;

-- Add comment
COMMENT ON COLUMN raw_dumps.direction IS 'Message direction: incoming (from Telegram) or outgoing (from web interface)';
