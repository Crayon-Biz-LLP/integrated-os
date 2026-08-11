-- 80_rpc_owner_scoping_2.sql
-- M3 sweep: scope the remaining tenant-data RPCs onto the tenant layer.
--
-- Every function gains `owner_id uuid DEFAULT NULL` as its LAST parameter
-- (the TenantAwareClient facade injects the current tenant under that exact
-- name, and PostgREST passes named args). Legacy unscoped calls (owner_id
-- omitted → NULL) behave exactly as before.
--
-- PL/pgSQL caution (verified on the copy DB): an unqualified `owner_id`
-- reference in a body that also has an owner_id COLUMN in scope is a
-- hard ERROR on PG17 ("column reference owner_id is ambiguous"). So every
-- plpgsql function snapshots the param into a local `p_owner` in DECLARE
-- and filters with `(p_owner IS NULL OR <t>.owner_id = p_owner)`.
--
-- The old overload is DROPPED first: CREATE OR REPLACE with an added param
-- creates a duplicate overload instead of replacing (db/79 lesson).
--
-- NOT in this file (global, no tenant data): next_clarification_shortcode
-- (pure sequence), run_sql (admin). The facade exempts them via _GLOBAL_RPCS.

-- ── match_memories ────────────────────────────────────────────────────────
DROP FUNCTION IF EXISTS public.match_memories(jsonb, double precision, integer);
CREATE OR REPLACE FUNCTION public.match_memories(query_embedding jsonb, match_threshold double precision, match_count integer, owner_id uuid DEFAULT NULL::uuid)
 RETURNS TABLE(id bigint, content text, memory_type text, metadata jsonb, similarity double precision, created_at timestamp with time zone)
 LANGUAGE plpgsql
AS $function$
DECLARE
    q_vec vector(768);
    p_owner uuid := owner_id;
BEGIN
    q_vec := query_embedding::text::vector(768);
    RETURN QUERY
    SELECT
        m.id,
        m.content,
        m.memory_type,
        m.metadata,
        1 - (m.embedding <=> q_vec) AS similarity,
        m.created_at
    FROM memories m
    WHERE m.embedding IS NOT NULL
        AND (m.embedding <=> q_vec) IS NOT NULL
        AND (m.embedding <=> q_vec) < 2
        AND (1 - (m.embedding <=> q_vec)) > match_threshold
        AND (m.expires_at IS NULL OR m.expires_at > now())
        AND (p_owner IS NULL OR m.owner_id = p_owner)
    ORDER BY similarity DESC
    LIMIT match_count;
END;
$function$;

-- ── match_memories_hybrid ─────────────────────────────────────────────────
DROP FUNCTION IF EXISTS public.match_memories_hybrid(vector, double precision, integer, double precision, double precision);
CREATE OR REPLACE FUNCTION public.match_memories_hybrid(query_embedding vector, match_threshold double precision, match_count integer, recency_weight double precision DEFAULT 0.3, importance_weight double precision DEFAULT 0.2, owner_id uuid DEFAULT NULL::uuid)
 RETURNS TABLE(id bigint, content text, memory_type text, metadata jsonb, similarity double precision, hybrid_score double precision, created_at timestamp with time zone)
 LANGUAGE plpgsql
AS $function$
DECLARE
    q_vec vector(768);
    now_utc timestamptz;
    p_owner uuid := owner_id;
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
            AND (p_owner IS NULL OR m.owner_id = p_owner)
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

-- ── match_resources (converted to plpgsql) ───────────────────────────────
-- LANGUAGE sql DOES NOT scope here: an unqualified param name that matches
-- a column resolves to the COLUMN, so `owner_id IS NULL OR resources.owner_id
-- = owner_id` became `resources.owner_id = resources.owner_id` — always true,
-- a silent cross-tenant leak (verified on the copy DB). plpgsql + p_owner
-- snapshot is the unambiguous pattern used across this file.
DROP FUNCTION IF EXISTS public.match_resources(vector, double precision, integer);
DROP FUNCTION IF EXISTS public.match_resources(vector, double precision, integer, uuid);
CREATE OR REPLACE FUNCTION public.match_resources(query_embedding vector, match_threshold double precision, match_count integer, owner_id uuid DEFAULT NULL::uuid)
 RETURNS TABLE(id bigint, title text, summary text, similarity double precision)
 LANGUAGE plpgsql
 STABLE
AS $function$
DECLARE
    p_owner uuid := owner_id;
BEGIN
  RETURN QUERY
  SELECT
    r.id,
    r.title,
    r.summary,
    1 - (r.embedding <=> query_embedding) AS similarity
  FROM resources r
  WHERE 1 - (r.embedding <=> query_embedding) > match_threshold
    AND (p_owner IS NULL OR r.owner_id = p_owner)
  ORDER BY r.embedding <=> query_embedding
  LIMIT match_count;
END;
$function$;

-- ── match_raw_dumps ───────────────────────────────────────────────────────
DROP FUNCTION IF EXISTS public.match_raw_dumps(vector, double precision, integer);
CREATE OR REPLACE FUNCTION public.match_raw_dumps(query_embedding vector, match_threshold double precision, match_count integer, owner_id uuid DEFAULT NULL::uuid)
 RETURNS TABLE(id bigint, content text, source text, similarity double precision)
 LANGUAGE plpgsql
AS $function$
DECLARE
    p_owner uuid := owner_id;
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
    AND (p_owner IS NULL OR r.owner_id = p_owner)
  ORDER BY similarity DESC
  LIMIT match_count;
END;
$function$;

-- ── match_messages_email (delegate target) ────────────────────────────────
DROP FUNCTION IF EXISTS public.match_messages_email(vector, integer, double precision);
CREATE OR REPLACE FUNCTION public.match_messages_email(query_embedding vector, match_count integer DEFAULT 5, match_threshold double precision DEFAULT 0.5, owner_id uuid DEFAULT NULL::uuid)
 RETURNS TABLE(id bigint, subject text, sender text, body_summary text, classification text, received_at timestamp with time zone, similarity double precision)
 LANGUAGE plpgsql
AS $function$
DECLARE
    p_owner uuid := owner_id;
BEGIN
  RETURN QUERY
  SELECT
    m.id,
    m.subject,
    m.sender_name AS sender,
    (m.metadata->>'body_summary')::text AS body_summary,
    m.classification,
    m.received_at,
    1 - (m.embedding <=> query_embedding) AS similarity
  FROM public.messages m
  WHERE m.channel = 'email'
    AND 1 - (m.embedding <=> query_embedding) > match_threshold
    AND (p_owner IS NULL OR m.owner_id = p_owner)
  ORDER BY similarity DESC
  LIMIT match_count;
END;
$function$;

-- ── match_emails_hybrid (delegates to match_messages_email) ───────────────
DROP FUNCTION IF EXISTS public.match_emails_hybrid(vector, integer, double precision);
CREATE OR REPLACE FUNCTION public.match_emails_hybrid(query_embedding vector, match_count integer DEFAULT 5, match_threshold double precision DEFAULT 0.5, owner_id uuid DEFAULT NULL::uuid)
 RETURNS TABLE(id bigint, subject text, sender text, body_summary text, classification text, received_at timestamp with time zone, similarity double precision)
 LANGUAGE plpgsql
AS $function$
DECLARE
    p_owner uuid := owner_id;
BEGIN
  RETURN QUERY
  SELECT * FROM public.match_messages_email(query_embedding, match_count, match_threshold, p_owner);
END;
$function$;

-- ── match_messages_whatsapp (delegate target) ─────────────────────────────
DROP FUNCTION IF EXISTS public.match_messages_whatsapp(vector, integer, double precision);
CREATE OR REPLACE FUNCTION public.match_messages_whatsapp(query_embedding vector, match_count integer DEFAULT 5, match_threshold double precision DEFAULT 0.5, owner_id uuid DEFAULT NULL::uuid)
 RETURNS TABLE(id bigint, sender_name text, sender_phone text, message_text text, summary text, classification text, received_at timestamp with time zone, similarity double precision)
 LANGUAGE plpgsql
AS $function$
DECLARE
    p_owner uuid := owner_id;
BEGIN
  RETURN QUERY
  SELECT
    m.id,
    m.sender_name,
    m.sender_id AS sender_phone,
    m.body AS message_text,
    m.summary,
    m.classification,
    m.received_at,
    1 - (m.embedding <=> query_embedding) AS similarity
  FROM public.messages m
  WHERE m.channel = 'whatsapp'
    AND 1 - (m.embedding <=> query_embedding) > match_threshold
    AND (p_owner IS NULL OR m.owner_id = p_owner)
  ORDER BY similarity DESC
  LIMIT match_count;
END;
$function$;

-- ── match_whatsapp_hybrid (delegates to match_messages_whatsapp) ──────────
DROP FUNCTION IF EXISTS public.match_whatsapp_hybrid(vector, integer, double precision);
CREATE OR REPLACE FUNCTION public.match_whatsapp_hybrid(query_embedding vector, match_count integer DEFAULT 5, match_threshold double precision DEFAULT 0.5, owner_id uuid DEFAULT NULL::uuid)
 RETURNS TABLE(id bigint, sender_name text, sender_phone text, message_text text, summary text, classification text, received_at timestamp with time zone, similarity double precision)
 LANGUAGE plpgsql
AS $function$
DECLARE
    p_owner uuid := owner_id;
BEGIN
  RETURN QUERY
  SELECT * FROM public.match_messages_whatsapp(query_embedding, match_count, match_threshold, p_owner);
END;
$function$;

-- ── search_phrase_nodes ───────────────────────────────────────────────────
DROP FUNCTION IF EXISTS public.search_phrase_nodes(text, integer);
CREATE OR REPLACE FUNCTION public.search_phrase_nodes(query_text text, result_limit integer DEFAULT 30, owner_id uuid DEFAULT NULL::uuid)
 RETURNS TABLE(id bigint, normalized_text text, display_text text, node_type text, rank real)
 LANGUAGE plpgsql
 STABLE
AS $function$
DECLARE
    p_owner uuid := owner_id;
BEGIN
    IF query_text IS NULL OR length(trim(query_text)) = 0 THEN
        RETURN;
    END IF;

    RETURN QUERY
    SELECT rpn.id,
           rpn.normalized_text,
           rpn.display_text,
           rpn.node_type,
           ts_rank_cd(rpn.search_vector, to_tsquery('simple', query_text))::REAL AS rank
    FROM public.retrieval_phrase_nodes rpn
    WHERE rpn.search_vector @@ to_tsquery('simple', query_text)
      AND (p_owner IS NULL OR rpn.owner_id = p_owner)
    ORDER BY rank DESC
    LIMIT result_limit;
END;
$function$;

-- ── find_serendipity_paths ────────────────────────────────────────────────
DROP FUNCTION IF EXISTS public.find_serendipity_paths(uuid[], integer);
CREATE OR REPLACE FUNCTION public.find_serendipity_paths(start_node_ids uuid[], max_depth integer DEFAULT 3, owner_id uuid DEFAULT NULL::uuid)
 RETURNS TABLE(start_node_id uuid, end_node_id uuid, path_labels text[], path_types text[], path_relations text[], total_weight numeric)
 LANGUAGE plpgsql
AS $function$
DECLARE
    p_owner uuid := owner_id;
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
          AND (p_owner IS NULL OR e.owner_id = p_owner)
          AND (p_owner IS NULL OR n1.owner_id = p_owner)
          AND (p_owner IS NULL OR n2.owner_id = p_owner)
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
          AND (p_owner IS NULL OR e.owner_id = p_owner)
          AND (p_owner IS NULL OR n2.owner_id = p_owner)
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
$function$;

-- ── claim_pending_enrichment_job ──────────────────────────────────────────
DROP FUNCTION IF EXISTS public.claim_pending_enrichment_job(integer);
CREATE OR REPLACE FUNCTION public.claim_pending_enrichment_job(job_id integer, owner_id uuid DEFAULT NULL::uuid)
 RETURNS SETOF pending_enrichment_jobs
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'public', 'extensions'
AS $function$
DECLARE
    p_owner uuid := owner_id;
BEGIN
    RETURN QUERY
    UPDATE pending_enrichment_jobs
    SET status = 'processing',
        started_at = NOW(),
        retry_count = retry_count + 1
    WHERE id = job_id
      AND status = 'pending'
      AND (p_owner IS NULL OR pending_enrichment_jobs.owner_id = p_owner)
    RETURNING *;
END;
$function$;

-- ── detect_drift ──────────────────────────────────────────────────────────
DROP FUNCTION IF EXISTS public.detect_drift(text, integer);
CREATE OR REPLACE FUNCTION public.detect_drift(project_name text, hours_window integer DEFAULT 48, owner_id uuid DEFAULT NULL::uuid)
 RETURNS TABLE(update_count bigint, first_update timestamp with time zone, last_update timestamp with time zone)
 LANGUAGE plpgsql
AS $function$
DECLARE
    p_owner uuid := owner_id;
BEGIN
    RETURN QUERY
    SELECT
        COUNT(*) as update_count,
        MIN(created_at) as first_update,
        MAX(created_at) as last_update
    FROM memories
    WHERE metadata->>'project' = project_name
      AND created_at > NOW() - (hours_window || ' hours')::INTERVAL
      AND metadata->>'type' = 'project_goal_update'
      AND (p_owner IS NULL OR memories.owner_id = p_owner);
END;
$function$;

-- ── cleanup_expired_clarifications ────────────────────────────────────────
DROP FUNCTION IF EXISTS public.cleanup_expired_clarifications();
CREATE OR REPLACE FUNCTION public.cleanup_expired_clarifications(owner_id uuid DEFAULT NULL::uuid)
 RETURNS integer
 LANGUAGE plpgsql
AS $function$
DECLARE
    cleaned INTEGER;
    p_owner uuid := owner_id;
BEGIN
    UPDATE pending_graph_clarifications
    SET status = 'expired', resolved_at = now()
    WHERE status = 'active' AND expires_at < now()
      AND (p_owner IS NULL OR pending_graph_clarifications.owner_id = p_owner);
    GET DIAGNOSTICS cleaned = ROW_COUNT;
    RETURN cleaned;
END;
$function$;

-- ── expire_stale_graph_edges ──────────────────────────────────────────────
DROP FUNCTION IF EXISTS public.expire_stale_graph_edges(integer);
CREATE OR REPLACE FUNCTION public.expire_stale_graph_edges(expiry_days integer DEFAULT 90, owner_id uuid DEFAULT NULL::uuid)
 RETURNS integer
 LANGUAGE plpgsql
AS $function$
DECLARE
    expired_count INTEGER;
    p_owner uuid := owner_id;
BEGIN
    UPDATE graph_edges
    SET metadata = jsonb_set(
        COALESCE(metadata, '{}'::jsonb),
        '{expired}',
        'true'
    )
    WHERE (valid_until IS NOT NULL AND valid_until < NOW())
      AND (metadata->>'expired' IS DISTINCT FROM 'true')
      AND (p_owner IS NULL OR graph_edges.owner_id = p_owner);

    GET DIAGNOSTICS expired_count = ROW_COUNT;
    RETURN expired_count;
END;
$function$;

-- ── archive_terminal_pending_edges ────────────────────────────────────────
DROP FUNCTION IF EXISTS public.archive_terminal_pending_edges();
DROP FUNCTION IF EXISTS public.archive_terminal_pending_edges(uuid);
-- Param named p_owner (not owner_id): this function's INSERT target list
-- contains the literal column `owner_id`, which a same-named PL/pgSQL param
-- would make ambiguous (PG17 hard-error). The facade injects p_owner for
-- this RPC (see _RPC_OWNER_PARAM in core/services/db.py).
CREATE OR REPLACE FUNCTION public.archive_terminal_pending_edges(p_owner uuid DEFAULT NULL::uuid)
 RETURNS void
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'public', 'extensions'
AS $function$
BEGIN
    -- Insert terminal rows older than 24 hours into the archive table.
    -- Explicit column list (NOT SELECT *) — immune to column-order drift
    -- between the active and archive tables (db/72 snoozed_until gap).
    INSERT INTO pending_graph_edges_archive (
        id, source_label, target_label, relationship, source_text, confidence,
        status, source_node_id, target_node_id, created_at, source_table,
        clarification_status, eval_context, shortcode, evaluated_at,
        epistemic_status, valid_from, source_ref, source_type, target_type,
        approval_source, owner_id, snoozed_until
    )
    SELECT
        id, source_label, target_label, relationship, source_text, confidence,
        status, source_node_id, target_node_id, created_at, source_table,
        clarification_status, eval_context, shortcode, evaluated_at,
        epistemic_status, valid_from, source_ref, source_type, target_type,
        approval_source, owner_id, snoozed_until
    FROM pending_graph_edges
    WHERE status IN ('approved', 'rejected', 'archived', 'skipped')
      AND created_at < NOW() - INTERVAL '24 hours'
      AND (p_owner IS NULL OR pending_graph_edges.owner_id = p_owner)
    ON CONFLICT DO NOTHING;

    -- Delete the archived rows from the active table
    DELETE FROM pending_graph_edges
    WHERE status IN ('approved', 'rejected', 'archived', 'skipped')
      AND created_at < NOW() - INTERVAL '24 hours'
      AND (p_owner IS NULL OR pending_graph_edges.owner_id = p_owner);
END;
$function$;

-- ── Schema gap surfaced by the sweep: archive_terminal_pending_edges did
-- INSERT ... SELECT * from pending_graph_edges, which breaks on column drift
-- (db/72 added snoozed_until to the ACTIVE table only — the RPC silently
-- failed inside sentinel's try/except). Add the column (nullable) AND switch
-- the INSERT to an explicit column list so it's immune to column-order drift.
ALTER TABLE public.pending_graph_edges_archive
    ADD COLUMN IF NOT EXISTS snoozed_until timestamp with time zone;

-- ── batch_whatsapp_message ────────────────────────────────────────────────
DROP FUNCTION IF EXISTS public.batch_whatsapp_message(text, text, text, timestamp with time zone, text, text, text, text, boolean, text, timestamp with time zone);
DROP FUNCTION IF EXISTS public.batch_whatsapp_message(text, text, text, timestamp with time zone, text, text, text, text, boolean, text, timestamp with time zone, uuid);
DROP FUNCTION IF EXISTS public.batch_whatsapp_message(text, text, text, timestamp with time zone, text, text, text, text, boolean, text, timestamp with time zone, text, text);
DROP FUNCTION IF EXISTS public.batch_whatsapp_message(text, text, text, timestamp with time zone, text, text, text, text, boolean, text, timestamp with time zone, text, text, uuid);
-- Param named p_owner (not owner_id): this function's INSERT target list
-- contains the literal column `owner_id`, which a same-named PL/pgSQL param
-- would make ambiguous (PG17 hard-error). The facade injects p_owner for
-- this RPC (see _RPC_OWNER_PARAM in core/services/db.py).
--
-- Phase 1 hardening (thread-aware classification): the app now passes
-- p_chat_id + p_participant (Stage-0 split, db/21). Merging/locking is by
-- CHAT, not sender, so a group's rapid replies from DIFFERENT participants
-- batch into one episode row; the split is persisted into metadata. Owner
-- scoping kept (p_owner injected by the facade).
CREATE OR REPLACE FUNCTION public.batch_whatsapp_message(p_sender_id text, p_sender_name text, p_body text, p_received_at timestamp with time zone, p_classification text, p_summary text, p_suggested_title text, p_suggested_project text, p_has_memory_value boolean, p_linked_person_name text, p_expires_at timestamp with time zone, p_chat_id text DEFAULT NULL::text, p_participant text DEFAULT NULL::text, p_owner uuid DEFAULT NULL::uuid)
 RETURNS jsonb
 LANGUAGE plpgsql
AS $function$
DECLARE
    lock_key   BIGINT;
    existing   messages;
    is_upgrade BOOLEAN;
    inserted_id BIGINT;
    merge_key  TEXT;
BEGIN
    -- Merge key: chat_id when provided (new pipeline), else sender_id (legacy).
    merge_key := COALESCE(NULLIF(p_chat_id, ''), p_sender_id);

    -- Advisory lock: serializes per-CHAT within the transaction.
    lock_key := hashtext(merge_key)::bigint;
    PERFORM pg_advisory_xact_lock(lock_key);

    -- Look for existing pending row within 3-minute window (same chat)
    SELECT * INTO existing
    FROM messages
    WHERE channel = 'whatsapp'
      AND COALESCE(metadata->>'chat_id', sender_id) = merge_key
      AND danny_decision IS NULL
      AND received_at >= NOW() - INTERVAL '3 minutes'
      AND (p_owner IS NULL OR messages.owner_id = p_owner)
    ORDER BY received_at DESC
    LIMIT 1;

    IF FOUND THEN
        is_upgrade := (p_classification = 'actionable' AND existing.classification != 'actionable');

        UPDATE messages
        SET body = existing.body || E'\n---\n' || p_body,
            classification = CASE WHEN is_upgrade THEN 'actionable' ELSE existing.classification END,
            summary         = CASE WHEN is_upgrade THEN p_summary         ELSE existing.summary END,
            suggested_title = CASE WHEN is_upgrade THEN p_suggested_title ELSE existing.suggested_title END,
            suggested_project= CASE WHEN is_upgrade THEN p_suggested_project ELSE existing.suggested_project END,
            has_memory_value= existing.has_memory_value OR p_has_memory_value,
            updated_at = NOW()
        WHERE id = existing.id;

        RETURN jsonb_build_object(
            'action', 'batched',
            'message_id', existing.id,
            'classification', CASE WHEN is_upgrade THEN 'actionable' ELSE existing.classification END
        );
    ELSE
        INSERT INTO messages (
            channel, source, sender_name, sender_id, body,
            classification, summary, suggested_title, suggested_project,
            has_memory_value, received_at, processing_status, metadata, expires_at,
            owner_id
        ) VALUES (
            'whatsapp', 'whatsapp', p_sender_name, p_sender_id, p_body,
            p_classification, p_summary, p_suggested_title, p_suggested_project,
            p_has_memory_value, p_received_at, 'completed',
            jsonb_build_object(
                'sender_phone', p_sender_id,
                'linked_person_name', p_linked_person_name,
                'chat_id', merge_key,
                'participant', p_participant
            ),
            p_expires_at,
            p_owner
        )
        RETURNING id INTO inserted_id;

        RETURN jsonb_build_object(
            'action', 'inserted',
            'message_id', inserted_id,
            'classification', p_classification
        );
    END IF;
END;
$function$;
