-- db/97: batch_whatsapp_message gains p_message_id (native Matrix event id)
-- + a DB-level exact-dedup backstop for re-delivered events.
--
-- Root cause: the Beeper bridge (Phase B1) delivers messages with native
-- Matrix event ids. The messages table has unique_channel_message
-- UNIQUE(channel, message_id) — but the old RPC never set message_id, so a
-- re-delivered sync event would either batch into a 3-min window row (wrong:
-- the same event re-appended) or violate the unique constraint (loud error,
-- not a clean skip).
--
-- Fix: p_message_id is stored in messages.message_id, and the RPC first
-- checks for an EXISTING row with that event id — if present, it returns
-- {'action':'duplicate'} immediately (no batching, no error). This is the
-- exact-dedup the plan promised (replaces the 24h body-match as the primary
-- guard for Beeper-sourced rows; body-match stays as the MacroDroid-era
-- fallback).
--
-- Idempotent: drops every legacy signature then recreates with the new one.

-- Legacy signatures (with/without p_owner, with/without p_chat_id/participant)
DROP FUNCTION IF EXISTS public.batch_whatsapp_message(text, text, text, timestamp with time zone, text, text, text, text, boolean, text, timestamp with time zone);
DROP FUNCTION IF EXISTS public.batch_whatsapp_message(text, text, text, timestamp with time zone, text, text, text, text, boolean, text, timestamp with time zone, uuid);
DROP FUNCTION IF EXISTS public.batch_whatsapp_message(text, text, text, timestamp with time zone, text, text, text, text, boolean, text, timestamp with time zone, text, text);
DROP FUNCTION IF EXISTS public.batch_whatsapp_message(text, text, text, timestamp with time zone, text, text, text, text, boolean, text, timestamp with time zone, text, text, uuid);
-- New signature (db/97 adds p_message_id)
DROP FUNCTION IF EXISTS public.batch_whatsapp_message(text, text, text, timestamp with time zone, text, text, text, text, boolean, text, timestamp with time zone, text, text, text, uuid);

CREATE OR REPLACE FUNCTION public.batch_whatsapp_message(
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
    p_participant     TEXT DEFAULT NULL,
    p_message_id      TEXT DEFAULT NULL,
    p_owner           UUID DEFAULT NULL
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
    -- ── Exact dedup: native event id already ingested? Skip silently. ──
    IF p_message_id IS NOT NULL AND p_message_id <> '' THEN
        PERFORM 1 FROM messages
        WHERE channel = 'whatsapp'
          AND message_id = p_message_id
          AND (p_owner IS NULL OR messages.owner_id = p_owner)
        LIMIT 1;
        IF FOUND THEN
            RETURN jsonb_build_object('action', 'duplicate', 'message_id', NULL);
        END IF;
    END IF;

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
            message_id, owner_id
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
            NULLIF(p_message_id, ''),
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
$$;
