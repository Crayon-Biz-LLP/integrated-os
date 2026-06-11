-- Phase 1: Create unified messages table and migrate data

BEGIN;

CREATE TABLE public.messages (
    id BIGSERIAL PRIMARY KEY,
    channel TEXT NOT NULL CHECK (channel IN ('whatsapp','email','call','teams')),
    source TEXT NOT NULL DEFAULT 'whatsapp',
    direction TEXT DEFAULT 'incoming' CHECK (direction IN ('incoming','outgoing')),
    processing_status TEXT DEFAULT 'pending' CHECK (processing_status IN ('pending','classified','completed','failed','embedding_failed')),

    message_id TEXT,
    thread_id TEXT,
    sender_name TEXT,
    sender_id TEXT,
    subject TEXT,
    body TEXT,

    classification TEXT CHECK (classification IN ('actionable','fyi','ignored','error','unhandled')),
    summary TEXT,
    suggested_title TEXT,
    suggested_project TEXT,

    is_human_sender BOOLEAN DEFAULT false,
    has_memory_value BOOLEAN DEFAULT false,
    needs_draft BOOLEAN DEFAULT false,
    danny_decision TEXT,
    decided_at TIMESTAMPTZ,
    shown_in_brief BOOLEAN DEFAULT false,

    linked_person_id INT8 REFERENCES public.people(id) ON DELETE SET NULL,
    linked_project_id INT8 REFERENCES public.projects(id) ON DELETE SET NULL,
    recording_id INT8 REFERENCES public.call_recordings(id) ON DELETE SET NULL,

    possible_duplicate BOOLEAN DEFAULT false,
    duplicate_of_title TEXT,
    project_confidence DOUBLE PRECISION,
    project_mapping_reason TEXT,

    metadata JSONB DEFAULT '{}'::jsonb,
    raw_payload JSONB,
    embedding vector(768),

    received_at TIMESTAMPTZ DEFAULT NOW(),
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),

    CONSTRAINT unique_channel_message UNIQUE (channel, message_id)
);

CREATE INDEX idx_messages_channel_class ON public.messages(channel, classification);
CREATE INDEX idx_messages_decision ON public.messages(danny_decision, channel) WHERE danny_decision IS NOT NULL;
CREATE INDEX idx_messages_pending ON public.messages(created_at) WHERE danny_decision IS NULL AND classification IN ('actionable','fyi');
CREATE INDEX idx_messages_retry ON public.messages(processing_status) WHERE processing_status IN ('pending', 'failed');

COMMENT ON COLUMN public.messages.metadata IS 'Channel-specific JSONB overflow. Expected schemas:
- whatsapp: {"sender_phone": "...", "linked_person_name": "..."}
- email: {"body_summary": "...", "gmail_labels": [...], "status": "..."}
- call: {"action_type": "...", "speaker_name": "...", "people_mentioned": [...]}
- teams: {"chat_id": "...", "chat_topic": "...", "attachments": [...]}';

COMMENT ON COLUMN public.messages.raw_payload IS 'Original webhook payload for debugging. Suggested retention: 30 days.';

-- 1b. Migrate whatsapp_messages -> messages
INSERT INTO public.messages (
    channel, source, message_id, sender_name, sender_id, body,
    classification, summary, suggested_title, suggested_project,
    has_memory_value, danny_decision, decided_at, shown_in_brief,
    embedding, received_at, created_at, updated_at,
    processing_status,
    metadata
)
SELECT 
    'whatsapp', 'whatsapp', 'wa_' || id::text, sender_name, sender_phone, message_text,
    classification, summary, suggested_title, suggested_project,
    has_memory_value, danny_decision, decided_at, shown_in_brief,
    embedding, received_at, created_at, created_at,
    'completed',
    jsonb_build_object('sender_phone', sender_phone, 'linked_person_name', linked_person_name)
FROM public.whatsapp_messages;

-- 1c. Migrate call_pending_items -> messages
INSERT INTO public.messages (
    channel, source, body, classification, summary, suggested_title,
    suggested_project, recording_id, danny_decision, decided_at, shown_in_brief,
    possible_duplicate, created_at, updated_at,
    processing_status,
    metadata
)
SELECT 
    'call', 'call_recording', suggested_title, 'actionable', summary, suggested_title,
    suggested_project, recording_id, danny_decision, decided_at, shown_in_brief,
    COALESCE(possible_duplicate, false), created_at, created_at,
    'completed',
    jsonb_build_object('action_type', action_type, 'people_mentioned', people_mentioned)
FROM public.call_pending_items;

-- 1d. Migrate emails + email_pending_tasks -> messages
INSERT INTO public.messages (
    channel, source, direction, message_id, thread_id, sender_name, sender_id,
    subject, body, classification, summary, suggested_title, suggested_project,
    linked_person_id, linked_project_id,
    is_human_sender, danny_decision, shown_in_brief,
    possible_duplicate, duplicate_of_title, project_confidence, project_mapping_reason,
    embedding, received_at, created_at, updated_at,
    processing_status,
    metadata
)
SELECT
    'email', e.source, 
    COALESCE(e.direction, 'incoming'),
    e.message_id, e.thread_id, e.sender, e.sender_email,
    e.subject, 
    COALESCE(e.body_raw, e.body_summary),
    e.classification, 
    NULL AS summary, 
    et.suggested_title, 
    et.suggested_project,
    e.linked_person_id, e.linked_project_id,
    COALESCE(et.is_human_sender, false), 
    CASE WHEN et.danny_decision = 'yes' THEN 'approved'
         WHEN et.danny_decision = 'no' THEN 'rejected'
         ELSE et.danny_decision END,
    COALESCE(et.shown_in_brief, false),
    COALESCE(et.possible_duplicate, false), et.duplicate_of_title,
    et.project_confidence, et.project_mapping_reason,
    e.embedding, e.received_at, e.created_at, e.created_at,
    CASE WHEN e.classification = 'error' THEN 'failed' ELSE 'completed' END,
    jsonb_build_object('body_summary', e.body_summary, 'gmail_labels', e.gmail_labels, 'status', e.status)
FROM public.emails e
LEFT JOIN public.email_pending_tasks et ON et.email_id = e.id;

-- 1e. email_drafts FK rename and remap
-- email_drafts has email_id -> emails.id
-- Since emails and messages have different IDs now, we need to map the email_id to the newly generated messages.id.
-- We can do this by matching the message_id (which is emails.message_id).
ALTER TABLE public.email_drafts RENAME COLUMN email_id TO old_email_id;
ALTER TABLE public.email_drafts ADD COLUMN message_id BIGINT;

UPDATE public.email_drafts ed
SET message_id = m.id
FROM public.emails e
JOIN public.messages m ON m.message_id = e.message_id AND m.channel = 'email'
WHERE ed.old_email_id = e.id;

ALTER TABLE public.email_drafts ADD CONSTRAINT email_drafts_message_id_fkey FOREIGN KEY (message_id) REFERENCES public.messages(id) ON DELETE CASCADE;

-- 1f. Rewrite match_emails_hybrid
CREATE OR REPLACE FUNCTION public.match_messages_email(query_embedding vector, match_count integer DEFAULT 5, match_threshold double precision DEFAULT 0.5)
 RETURNS TABLE(id bigint, subject text, sender text, body_summary text, classification text, received_at timestamp with time zone, similarity double precision)
 LANGUAGE plpgsql
AS $function$
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
  ORDER BY similarity DESC
  LIMIT match_count;
END;
$function$;

-- Update the existing match_emails_hybrid to use the new table so nothing breaks before Python is deployed
DROP FUNCTION IF EXISTS public.match_emails_hybrid(vector, integer, double precision);
CREATE OR REPLACE FUNCTION public.match_emails_hybrid(query_embedding vector, match_count integer DEFAULT 5, match_threshold double precision DEFAULT 0.5)
 RETURNS TABLE(id bigint, subject text, sender text, body_summary text, classification text, received_at timestamp with time zone, similarity double precision)
 LANGUAGE plpgsql
AS $function$
BEGIN
  RETURN QUERY
  SELECT * FROM public.match_messages_email(query_embedding, match_count, match_threshold);
END;
$function$;

-- 1g. Rewrite match_whatsapp_hybrid
CREATE OR REPLACE FUNCTION public.match_messages_whatsapp(query_embedding vector, match_count integer DEFAULT 5, match_threshold double precision DEFAULT 0.5)
 RETURNS TABLE(id bigint, sender_name text, sender_phone text, message_text text, summary text, classification text, received_at timestamp with time zone, similarity double precision)
 LANGUAGE plpgsql
AS $function$
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
  ORDER BY similarity DESC
  LIMIT match_count;
END;
$function$;

-- Update the existing match_whatsapp_hybrid for backwards compatibility
DROP FUNCTION IF EXISTS public.match_whatsapp_hybrid(vector, integer, double precision);
CREATE OR REPLACE FUNCTION public.match_whatsapp_hybrid(query_embedding vector, match_count integer DEFAULT 5, match_threshold double precision DEFAULT 0.5)
 RETURNS TABLE(id bigint, sender_name text, sender_phone text, message_text text, summary text, classification text, received_at timestamp with time zone, similarity double precision)
 LANGUAGE plpgsql
AS $function$
BEGIN
  RETURN QUERY
  SELECT * FROM public.match_messages_whatsapp(query_embedding, match_count, match_threshold);
END;
$function$;

COMMIT;
