-- Conversation Episodic Index (Phase 1 — Option H)
-- Adds embedding column for vector similarity search across conversation history,
-- enabling semantic retrieval of past exchanges regardless of thread or recency.
--
-- Watchout A (Dedup): exclude_ids parameter in RPC prevents duplicate exchanges
-- Watchout B (Vector Dim): vector(768) matches EMBEDDING_DIMENSION in core/llm/constants.py
-- Watchout C (HNSW): HNSW index chosen over IVFFlat because:

-- Step 1: Add columns (both nullable — backward compatible)
ALTER TABLE conversations ADD COLUMN embedding vector(768);
ALTER TABLE conversations ADD COLUMN entity_ids uuid[];

-- Step 2: HNSW index — no training needed, excellent recall from day one
-- HNSW chosen over IVFFlat because:
--   - Table has ~1000-5000 rows (IVFFlat needs tens of thousands for good centroid distribution)
--   - Grows by ~50 rows/day (IVFFlat needs periodic re-indexing, HNSW handles incrementally)
--   - We query with LIMIT 3 (HNSW has better recall at low limits)
--   - No training step needed (IVFFlat requires representative data at index-build time)
-- m=16, ef_construction=64 are reasonable defaults for a table this size.
CREATE INDEX IF NOT EXISTS idx_conversations_embedding
  ON conversations
  USING hnsw (embedding vector_cosine_ops)
  WITH (m = 16, ef_construction = 64);

-- Step 3: match_conversations RPC
-- Finds semantically similar past conversation exchanges.
-- exclude_ids prevents returning exchanges already included via recency path.
CREATE OR REPLACE FUNCTION public.match_conversations(
    query_embedding vector(768),
    match_count int DEFAULT 3,
    match_threshold float DEFAULT 0.5,
    exclude_ids bigint[] DEFAULT '{}'
)
RETURNS TABLE(
    id bigint,
    role text,
    intent text,
    content text,
    created_at timestamp with time zone,
    similarity float
)
LANGUAGE sql STABLE AS $$
    SELECT
        id,
        role,
        intent,
        content,
        created_at,
        1 - (embedding <=> query_embedding) AS similarity
    FROM conversations
    WHERE embedding IS NOT NULL
      AND (cardinality(exclude_ids) = 0 OR NOT (id = ANY(exclude_ids)))
      AND 1 - (embedding <=> query_embedding) > match_threshold
    ORDER BY embedding <=> query_embedding
    LIMIT match_count;
$$;
