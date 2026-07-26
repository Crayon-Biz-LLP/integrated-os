-- Migration: 71_rpc_get_memory_metadata
--
-- Consolidates steps 6b+7 of the associative retrieval pipeline:
--   - Fetch memory metadata + expiry check (replaces 1 HTTP round-trip)
--   - Fetch passage embeddings + compute semantic similarity (replaces 1 HTTP + Python)
--   - Fetch phrase node specificity scores (replaces 2-3 HTTP round-trips)
--
-- Returns a TABLE (one row per memory) with all metadata and boost signals.
-- Uses set-based CTEs (LEFT JOIN, GROUP BY) instead of correlated subqueries.
-- ::text casts on timestamptz columns for PostgREST type compatibility.
--
-- Review history:
--   Review 1: Fixed JSONB type mismatch, timestamptz casting
--   Review 2: Fixed correlated subquery bottleneck (N scans → set-based CTEs)
--   Review 4: Confirmed TABLE return type correct for async_fetch (multi-row)

DROP FUNCTION IF EXISTS rpc_get_memory_metadata(int[], vector);

CREATE FUNCTION rpc_get_memory_metadata(
    p_memory_ids int[],
    p_query_embedding vector(768) DEFAULT NULL
)
RETURNS TABLE (
    memory_id          bigint,
    content            text,
    memory_type        text,
    created_at         text,          -- ::text cast for PostgREST compat (ISO string)
    expires_at         text,          -- ::text cast for PostgREST compat
    importance_score   float,
    project_id         bigint,
    semantic_score     float,         -- Max cosine similarity across passages
    specificity_score  float          -- Max specificity across linked phrase nodes
)
LANGUAGE plpgsql STABLE
AS $$
BEGIN
    RETURN QUERY
    WITH semantic_scores AS (
        SELECT rp.memory_id,
               MAX(1 - (rp.embedding <=> p_query_embedding)) AS score
        FROM retrieval_passages rp
        WHERE rp.memory_id = ANY(p_memory_ids)
          AND p_query_embedding IS NOT NULL
        GROUP BY rp.memory_id
    ),
    specificity_scores AS (
        SELECT mb.memory_id,
               MAX(rns.specificity_score) AS score
        FROM retrieval_memory_bundle_links mb
        JOIN retrieval_passage_phrase_links ppl ON ppl.passage_id = mb.passage_id
        JOIN retrieval_node_stats rns ON rns.node_id = ppl.node_id
        WHERE mb.memory_id = ANY(p_memory_ids)
        GROUP BY mb.memory_id
    )
    SELECT
        m.id,
        m.content,
        m.memory_type,
        m.created_at::text,       -- ISO string for PostgREST compat
        m.expires_at::text,
        (COALESCE(m.importance_score, 5) / 10.0)::float,
        m.project_id,
        COALESCE(ss.score, 0.0)::float,
        COALESCE(sps.score, 0.5)::float
    FROM memories m
    LEFT JOIN semantic_scores ss ON ss.memory_id = m.id
    LEFT JOIN specificity_scores sps ON sps.memory_id = m.id
    WHERE m.id = ANY(p_memory_ids)
      AND m.is_current = TRUE
      AND (m.expires_at IS NULL OR m.expires_at > NOW());
END;
$$;
