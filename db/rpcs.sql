-- Supabase RPC Definitions

-- match_canonical_pages
CREATE OR REPLACE FUNCTION public.match_canonical_pages(query_embedding jsonb, match_threshold double precision, match_count integer)
 RETURNS TABLE(id bigint, title text, content text, similarity double precision, updated_at timestamp with time zone)
 LANGUAGE plpgsql
AS $function$
DECLARE
    q_vec vector(768);
BEGIN
    q_vec := query_embedding::text::vector(768);
    RETURN QUERY
    SELECT
        cp.id,
        cp.title,
        cp.content,
        1 - (cp.embedding <=> q_vec) AS similarity,
        cp.updated_at
    FROM canonical_pages cp
    WHERE cp.embedding IS NOT NULL
        AND (cp.embedding <=> q_vec) IS NOT NULL
        AND (cp.embedding <=> q_vec) < 2
        AND (1 - (cp.embedding <=> q_vec)) > match_threshold
    ORDER BY similarity DESC
    LIMIT match_count;
END;
$function$


-- match_raw_dumps
CREATE OR REPLACE FUNCTION public.match_raw_dumps(query_embedding vector, match_threshold double precision, match_count integer)
 RETURNS TABLE(id bigint, content text, source text, similarity double precision)
 LANGUAGE plpgsql
AS $function$
BEGIN
  RETURN QUERY
  SELECT
    r.id,
    r.content,
    r.source,
    1 - (r.embedding <=> query_embedding) AS similarity
  FROM raw_dumps r
  WHERE r.embedding IS NOT NULL
    AND 1 - (r.embedding <=> query_embedding) > match_threshold
  ORDER BY similarity DESC
  LIMIT match_count;
END;
$function$


-- match_logs
CREATE OR REPLACE FUNCTION public.match_logs(query_embedding vector, match_threshold double precision, match_count integer)
 RETURNS TABLE(id bigint, content text, similarity double precision)
 LANGUAGE sql
 STABLE
AS $function$
  SELECT
    id,
    content,
    1 - (embedding <=> query_embedding) AS similarity
  FROM logs
  WHERE 1 - (embedding <=> query_embedding) > match_threshold
  ORDER BY embedding <=> query_embedding
  LIMIT match_count;
$function$


-- update_updated_at
CREATE OR REPLACE FUNCTION public.update_updated_at()
 RETURNS trigger
 LANGUAGE plpgsql
AS $function$
BEGIN
  NEW.updated_at = NOW();
  RETURN NEW;
END;
$function$


-- match_resources
CREATE OR REPLACE FUNCTION public.match_resources(query_embedding vector, match_threshold double precision, match_count integer)
 RETURNS TABLE(id bigint, title text, summary text, similarity double precision)
 LANGUAGE sql
 STABLE
AS $function$
  SELECT
    id,
    title,
    summary,
    1 - (embedding <=> query_embedding) AS similarity
  FROM resources
  WHERE 1 - (embedding <=> query_embedding) > match_threshold
  ORDER BY embedding <=> query_embedding
  LIMIT match_count;
$function$


-- match_emails_hybrid
CREATE OR REPLACE FUNCTION public.match_emails_hybrid(query_embedding vector, match_count integer DEFAULT 5, match_threshold double precision DEFAULT 0.5)
 RETURNS TABLE(id integer, subject text, sender text, body_summary text, classification text, received_at timestamp with time zone, similarity double precision)
 LANGUAGE plpgsql
AS $function$
BEGIN
  RETURN QUERY
  SELECT
    e.id,
    e.subject,
    e.sender,
    e.body_summary,
    e.classification,
    e.received_at,
    1 - (e.embedding <=> query_embedding) AS similarity
  FROM emails e
  WHERE 1 - (e.embedding <=> query_embedding) > match_threshold
  ORDER BY similarity DESC
  LIMIT match_count;
END;
$function$


-- match_memories
CREATE OR REPLACE FUNCTION public.match_memories(query_embedding jsonb, match_threshold double precision, match_count integer)
 RETURNS TABLE(id bigint, content text, metadata jsonb, similarity double precision, created_at timestamp with time zone)
 LANGUAGE plpgsql
AS $function$
DECLARE
    q_vec vector(768);
BEGIN
    q_vec := query_embedding::text::vector(768);
    RETURN QUERY
    SELECT
        m.id,
        m.content,
        m.metadata,
        1 - (m.embedding <=> q_vec) AS similarity,
        m.created_at
    FROM memories m
    WHERE m.embedding IS NOT NULL
        AND (m.embedding <=> q_vec) IS NOT NULL
        AND (m.embedding <=> q_vec) < 2
        AND (1 - (m.embedding <=> q_vec)) > match_threshold
    ORDER BY similarity DESC
    LIMIT match_count;
END;
$function$


-- prune_old_memories
CREATE OR REPLACE FUNCTION public.prune_old_memories()
 RETURNS integer
 LANGUAGE plpgsql
AS $function$
DECLARE
    pruned_count INTEGER;
BEGIN
    -- Prune memories where importance_score < 3 AND last_accessed_at > 90 days
    WITH to_prune AS (
        UPDATE memories 
        SET pruned = TRUE, 
            pruned_at = NOW(),
            pruned_reason = 'importance_decay',
            metadata = COALESCE(metadata, '{}'::jsonb) || '{"pruned": true, "pruned_reason": "importance_decay"}'::jsonb
        WHERE importance_score < 3 
          AND last_accessed_at < NOW() - INTERVAL '90 days'
          AND pruned = FALSE
        RETURNING id
    )
    SELECT COUNT(*) INTO pruned_count FROM to_prune;
    
    RETURN pruned_count;
END;
$function$


-- match_whatsapp_hybrid
CREATE OR REPLACE FUNCTION public.match_whatsapp_hybrid(query_embedding vector, match_count integer DEFAULT 5, match_threshold double precision DEFAULT 0.5)
 RETURNS TABLE(id bigint, sender_name text, sender_phone text, message_text text, summary text, classification text, received_at timestamp with time zone, similarity double precision)
 LANGUAGE plpgsql
AS $function$
BEGIN
  RETURN QUERY
  SELECT
    w.id,
    w.sender_name,
    w.sender_phone,
    w.message_text,
    w.summary,
    w.classification,
    w.received_at,
    1 - (w.embedding <=> query_embedding) AS similarity
  FROM whatsapp_messages w
  WHERE 1 - (w.embedding <=> query_embedding) > match_threshold
  ORDER BY similarity DESC
  LIMIT match_count;
END;
$function$


-- get_memory_at_time
CREATE OR REPLACE FUNCTION public.get_memory_at_time(memory_id bigint, query_time timestamp with time zone)
 RETURNS TABLE(id bigint, content text, version integer, created_at timestamp with time zone, metadata jsonb)
 LANGUAGE plpgsql
AS $function$
BEGIN
    RETURN QUERY
    SELECT m.id, m.content, m.version, m.created_at, m.metadata
    FROM memories m
    WHERE m.id = memory_id 
       OR m.supersedes_id = memory_id
    ORDER BY m.version DESC
    LIMIT 1;
END;
$function$


-- detect_drift
CREATE OR REPLACE FUNCTION public.detect_drift(project_name text, hours_window integer DEFAULT 48)
 RETURNS TABLE(update_count bigint, first_update timestamp with time zone, last_update timestamp with time zone)
 LANGUAGE plpgsql
AS $function$
BEGIN
    RETURN QUERY
    SELECT 
        COUNT(*) as update_count,
        MIN(created_at) as first_update,
        MAX(created_at) as last_update
    FROM memories
    WHERE metadata->>'project' = project_name
      AND created_at > NOW() - (hours_window || ' hours')::INTERVAL
      AND metadata->>'type' = 'project_goal_update';
END;
$function$


-- match_memories_hybrid
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
    )
    SELECT
        b.id,
        b.content,
        b.memory_type,
        b.metadata,
        b.similarity,
        -- Hybrid score calculation:
        -- Base similarity (0 to 1)
        -- Recency: exponential decay over 30 days. Exp(-days/15).
        -- Importance: normalized (score/10)
        (b.similarity * (1 - recency_weight - importance_weight) + 
         EXP(-GREATEST(EXTRACT(EPOCH FROM (now_utc - b.created_at))/86400.0, 0) / 15.0) * recency_weight + 
         (COALESCE(b.importance_score, 5) / 10.0) * importance_weight)::float AS hybrid_score,
        b.created_at
    FROM base_matches b
    ORDER BY hybrid_score DESC
    LIMIT match_count;
END;
$function$


-- get_most_connected_nodes
CREATE OR REPLACE FUNCTION public.get_most_connected_nodes(limit_count integer DEFAULT 3)
 RETURNS TABLE(node_id uuid, label text, type text, edge_count bigint)
 LANGUAGE sql
AS $function$
    SELECT 
        n.id as node_id, 
        n.label, 
        n.type, 
        COUNT(e.id) as edge_count
    FROM graph_nodes n
    LEFT JOIN graph_edges e ON n.id = e.source_node_id OR n.id = e.target_node_id
    WHERE n.type IN ('person', 'project', 'concept')
    GROUP BY n.id, n.label, n.type
    ORDER BY edge_count DESC
    LIMIT limit_count;
$function$


-- find_serendipity_paths
CREATE OR REPLACE FUNCTION public.find_serendipity_paths(start_node_ids uuid[], max_depth integer DEFAULT 3)
 RETURNS TABLE(start_node_id uuid, end_node_id uuid, path_labels text[], path_types text[], path_relations text[], total_weight numeric)
 LANGUAGE plpgsql
AS $function$
BEGIN
    RETURN QUERY
    WITH RECURSIVE graph_paths AS (
        -- Base case: Starting edges from the given nodes
        SELECT 
            e.source_node_id AS current_node,
            e.target_node_id AS end_node,
            1 AS depth,
            ARRAY[n1.label, n2.label] AS path_labels,
            ARRAY[n1.type, n2.type] AS path_types,
            ARRAY[e.relationship] AS path_relations,
            (COALESCE((e.metadata->>'weight')::numeric, 1.0)) AS total_weight,
            ARRAY[e.source_node_id, e.target_node_id] AS visited_nodes
        FROM graph_edges e
        JOIN graph_nodes n1 ON e.source_node_id = n1.id
        JOIN graph_nodes n2 ON e.target_node_id = n2.id
        WHERE e.source_node_id = ANY(start_node_ids)
        UNION ALL
        -- Recursive case: traverse to next edges
        SELECT 
            gp.current_node,
            e.target_node_id AS end_node,
            gp.depth + 1,
            gp.path_labels || n2.label,
            gp.path_types || n2.type,
            gp.path_relations || e.relationship,
            gp.total_weight + (COALESCE((e.metadata->>'weight')::numeric, 1.0)),
            gp.visited_nodes || e.target_node_id
        FROM graph_paths gp
        JOIN graph_edges e ON gp.end_node = e.source_node_id
        JOIN graph_nodes n2 ON e.target_node_id = n2.id
        WHERE gp.depth < max_depth
          -- Prevent cycles (don't revisit nodes already in this path)
          AND NOT e.target_node_id = ANY(gp.visited_nodes)
    )
    SELECT 
        gp.current_node AS start_node_id,
        gp.end_node,
        gp.path_labels,
        gp.path_types,
        gp.path_relations,
        gp.total_weight
    FROM graph_paths gp
    -- Filter out trivial 1-hop connections (we want 2nd and 3rd degree links)
    WHERE gp.depth >= 2 
    ORDER BY gp.total_weight DESC;
END;
$function$


