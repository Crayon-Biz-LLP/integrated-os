-- Atomic batch-or-insert for WhatsApp messages.
-- Uses pg_advisory_xact_lock on sender_id hash to serialize concurrent
-- messages from the same sender during the 3-min batch window.
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
    p_expires_at      TIMESTAMPTZ
) RETURNS JSONB
LANGUAGE plpgsql
AS $$
DECLARE
    lock_key   BIGINT;
    existing   messages;
    is_upgrade BOOLEAN;
    inserted_id BIGINT;
BEGIN
    -- Advisory lock: serializes per-sender within the transaction.
    -- Use hashtext to get a 32-bit int, cast to bigint to match lock key requirements
    lock_key := hashtext(p_sender_id)::bigint;
    PERFORM pg_advisory_xact_lock(lock_key);

    -- Look for existing pending row within 3-minute window
    SELECT * INTO existing
    FROM messages
    WHERE channel = 'whatsapp'
      AND sender_id = p_sender_id
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
                'linked_person_name', p_linked_person_name
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