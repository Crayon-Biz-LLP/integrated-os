-- Fix routing schema bug: projects use INT8 ids, organizations use UUID ids.
-- entity_id must be TEXT to hold both safely.

BEGIN;

-- Drop the index that depends on the column
DROP INDEX IF EXISTS idx_unique_active_entity_thread;

-- Alter the column type from UUID to TEXT
ALTER TABLE public.conversation_threads
ALTER COLUMN entity_id TYPE TEXT USING entity_id::TEXT;

-- Recreate the unique index
CREATE UNIQUE INDEX idx_unique_active_entity_thread 
ON public.conversation_threads (chat_id, thread_type, entity_type, entity_id) 
WHERE archived_at IS NULL AND entity_id IS NOT NULL;

COMMIT;
