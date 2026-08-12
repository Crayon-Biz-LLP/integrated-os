-- Atomic batch-or-insert for WhatsApp messages.
-- Uses pg_advisory_xact_lock on CHAT id hash to serialize concurrent
-- messages from the same CHAT (group or 1:1) during the 3-min batch window.
--
-- Phase 1 hardening (thread-aware classification):
--   - Merges by chat_id, NOT sender_id, so a group's rapid-fire replies
--     from DIFFERENT participants (Henry's ask + Sunjula's "ok noted")
--     batch into one episode row instead of scattering.
--   - Persists the Stage-0 split (chat_id + participant) into metadata.
CREATE OR REPLACE FUNCTION batch_whatsapp_message(
    p_sender_id       TEXT,
    p_sender_name     TEXT,
    p_body            TEXT,
    p_received_at     TIMESTAMPTZ,
    p_classification  TEXT,
    p_summary         TEXT,
    p_suggested_title TEXT,
    p_suggested_project TEXT,
    p_has_memory_value BOOLEAN,
    p_linked_person_name TEXT,
    p_expires_at      TIMESTAMPTZ,
    p_chat_id         TEXT DEFAULT NULL,
    p_participant     TEXT DEFAULT NULL
) RETURNS JSONB
LANGUAGE plpgsql
AS $$
DECLARE
    lock_key   BIGINT;
    existing   messages;
    is_upgrade BOOLEAN;
    inserted_id BIGINT;
    merge_key  TEXT;
BEGIN
    -- Merge key: chat_id when provided (new pipeline), else sender_id (legacy)
    merge_key := COALESCE(NULLIF(p_chat_id, ''), p_sender_id);

    -- Advisory lock: serializes per-chat within the transaction.
    -- Use hashtext to get a 32-bit int, cast to bigint to match lock key requirements
    lock_key := hashtext(merge_key)::bigint;
    PERFORM pg_advisory_xact_lock(lock_key);

    -- Look for existing pending row within 3-minute window (same chat)
    SELECT * INTO existing
    FROM messages
    WHERE channel = 'whatsapp'
      AND COALESCE(metadata->>'chat_id', sender_id) = merge_key
      AND danny_decision IS NULL
      AND received_at >= NOW() - INTERVAL '3 minutes'
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
            has_memory_value, received_at, processing_status, metadata, expires_at
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
            p_expires_at
        )
        RETURNING id INTO inserted_id;

        RETURN jsonb_build_object(
            'action', 'inserted',
            'message_id', inserted_id,
            'classification', p_classification
        );
    END IF;
END;
$$;
