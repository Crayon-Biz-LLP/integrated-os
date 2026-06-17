-- Layer 2: Expiry Schema
-- Run this after deploying the Layer 2 code changes.

-- ============================================================
-- 1. Add expires_at columns
-- ============================================================

ALTER TABLE memories ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ;
ALTER TABLE messages ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ;

-- Index for efficient filtering
CREATE INDEX IF NOT EXISTS idx_memories_expires_at ON memories(expires_at);
CREATE INDEX IF NOT EXISTS idx_messages_expires_at ON messages(expires_at);

-- ============================================================
-- 2. Update match_memories_hybrid RPC to filter expired items
-- ============================================================

CREATE OR REPLACE FUNCTION public.match_memories_hybrid(query_embedding vector, match_threshold double precision, match_count integer, recency_weight double precision DEFAULT 0.3, importance_weight double precision DEFAULT 0.2)
 RETURNS TABLE(id bigint, content text, memory_type text, metadata jsonb, similarity double precision, hybrid_score double precision, created_at timestamp with time zone)
 LANGUAGE plpgsql
AS $function$
DECLARE
    q_vec vector(768);
    now_utc timestamptz;
BEGIN
    q_vec := query_embedding::text::vector(768);
    now_utc := current_timestamp;
    
    RETURN QUERY
    WITH base_matches AS (
        SELECT
            m.id,
            m.content,
            m.memory_type,
            m.metadata,
            m.created_at,
            m.importance_score,
            1 - (m.embedding <=> q_vec) AS similarity
        FROM memories m
        WHERE m.embedding IS NOT NULL
            AND (m.embedding <=> q_vec) IS NOT NULL
            AND (m.embedding <=> q_vec) < 2
            AND (1 - (m.embedding <=> q_vec)) > match_threshold
            AND m.is_archived = false
            AND m.is_current = true
            AND m.pruned = false
            AND (m.expires_at IS NULL OR m.expires_at > now_utc)
    )
    SELECT
        b.id,
        b.content,
        b.memory_type,
        b.metadata,
        b.similarity,
        (b.similarity * (1 - recency_weight - importance_weight) + 
         EXP(-GREATEST(EXTRACT(EPOCH FROM (now_utc - b.created_at))/86400.0, 0) / 15.0) * recency_weight + 
         (COALESCE(b.importance_score, 5) / 10.0) * importance_weight)::float AS hybrid_score,
        b.created_at
    FROM base_matches b
    ORDER BY hybrid_score DESC
    LIMIT match_count;
END;
$function$;

-- ============================================================
-- 3. One-time backfill: set expires_at on existing time-sensitive rows
--    Anchored to created_at, not now().
-- ============================================================

-- Memories containing "today" → expires end of their creation day
UPDATE memories
SET expires_at = date_trunc('day', created_at) + INTERVAL '23 hours 59 minutes 59 seconds'
WHERE expires_at IS NULL
  AND (
    content ILIKE '%today%'
    OR content ILIKE '%tomorrow%'
    OR content ILIKE '%this %onday%'
    OR content ILIKE '%this %uesday%'
    OR content ILIKE '%this %ednesday%'
    OR content ILIKE '%this %hursday%'
    OR content ILIKE '%this %riday%'
    OR content ILIKE '%this %aturday%'
    OR content ILIKE '%this %unday%'
  );

-- Messages containing "today" → expires end of their received_at day
UPDATE messages
SET expires_at = date_trunc('day', received_at) + INTERVAL '23 hours 59 minutes 59 seconds'
WHERE expires_at IS NULL
  AND (
    body ILIKE '%today%'
    OR body ILIKE '%tomorrow%'
    OR body ILIKE '%this %onday%'
    OR body ILIKE '%this %uesday%'
    OR body ILIKE '%this %ednesday%'
    OR body ILIKE '%this %hursday%'
    OR body ILIKE '%this %riday%'
    OR body ILIKE '%this %aturday%'
    OR body ILIKE '%this %unday%'
  );
