-- db/96: awaiting_reply — tenant-scoped tracker for open asks (Phase A,
-- Beeper messaging layer).
--
-- Purpose: whenever the user sends an ask (via Rhodey or by approving an
-- item as "I'll reply"), mark the chat as awaiting a reply. When an
-- incoming message lands in that chat (minutes or days later), it can be
-- linked to the open question — fixing the gap where the OS never knew
-- what the user had asked, so replies from hours later floated loose.
--
-- Also registers nothing on messages: `direction` already exists on
-- messages (db/01, 'incoming'/'outgoing') and 'responded' is a plain
-- TEXT value with no CHECK constraint (verified live), so the auto-resolve
-- rule writes it directly.

CREATE TABLE IF NOT EXISTS public.awaiting_reply (
    id                BIGSERIAL PRIMARY KEY,
    owner_id          UUID NOT NULL,
    chat_id           TEXT NOT NULL,
    channel           TEXT NOT NULL,
    question          TEXT,
    status            TEXT NOT NULL DEFAULT 'awaiting'
                          CHECK (status IN ('awaiting', 'answered', 'expired')),
    linked_message_id BIGINT,          -- the pending messages.id that spawned the ask
    asked_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    replied_at        TIMESTAMPTZ,
    expires_at        TIMESTAMPTZ,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Exactly ONE row per (owner, chat): the tracker keeps only the CURRENT
-- ask, and status flips awaiting -> answered/expired -> awaiting on a new
-- ask. A PLAIN unique constraint (not partial) is deliberate: PostgREST's
-- on_conflict param can only express a column list / constraint name, NOT
-- a WHERE predicate — a partial index would make the app's
-- .upsert(row, on_conflict='owner_id,chat_id') fail at plan time.
ALTER TABLE public.awaiting_reply
    ADD CONSTRAINT awaiting_reply_owner_chat_key UNIQUE (owner_id, chat_id);

CREATE INDEX IF NOT EXISTS idx_awaiting_reply_owner_chat
    ON public.awaiting_reply (owner_id, chat_id);

CREATE INDEX IF NOT EXISTS idx_awaiting_reply_open_expiry
    ON public.awaiting_reply (expires_at)
    WHERE status = 'awaiting';

-- API-role grants (same posture as db/87: every new pooler-created table
-- needs explicit grants, since Supabase default privileges don't cover
-- objects created outside its own tooling).
GRANT USAGE ON SCHEMA public TO anon, authenticated, service_role;
GRANT ALL ON TABLE public.awaiting_reply TO anon, authenticated, service_role;
