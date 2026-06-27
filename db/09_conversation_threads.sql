-- Phase 1 & 2 & 3: Persistent Threads and Workflows
-- Adds durable context for notes, follow-ups, and stateful chat flows

CREATE TYPE thread_type AS ENUM ('general', 'entity', 'workflow');
CREATE TYPE workflow_status AS ENUM ('active', 'resolved', 'cancelled', 'expired');

CREATE TABLE conversation_threads (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    chat_id BIGINT NOT NULL,
    thread_type thread_type NOT NULL DEFAULT 'general',
    entity_type TEXT,
    entity_id UUID,
    entity_label TEXT,
    active_anchor JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_active_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    archived_at TIMESTAMPTZ,
    summary TEXT,
    routing_confidence TEXT
);

-- Exact-match uniqueness constraint: max one active entity thread per chat_id/entity
CREATE UNIQUE INDEX idx_unique_active_entity_thread 
ON conversation_threads (chat_id, thread_type, entity_type, entity_id) 
WHERE archived_at IS NULL AND entity_id IS NOT NULL;

-- Fast lookup for routing
CREATE INDEX idx_conversation_threads_chat ON conversation_threads (chat_id, last_active_at DESC) WHERE archived_at IS NULL;

CREATE TABLE conversation_workflows (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    chat_id BIGINT NOT NULL,
    thread_id UUID NOT NULL REFERENCES conversation_threads(id) ON DELETE CASCADE,
    workflow_type TEXT NOT NULL,
    status workflow_status NOT NULL DEFAULT 'active',
    awaiting_user_input BOOLEAN NOT NULL DEFAULT false,
    payload JSONB,
    expires_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    resolved_at TIMESTAMPTZ
);

-- Max one active workflow per thread
CREATE UNIQUE INDEX idx_unique_active_workflow_per_thread
ON conversation_workflows (thread_id)
WHERE status = 'active';

-- Link existing conversations table
ALTER TABLE conversations ADD COLUMN thread_id UUID REFERENCES conversation_threads(id) ON DELETE CASCADE;
ALTER TABLE conversations ADD COLUMN workflow_id UUID REFERENCES conversation_workflows(id) ON DELETE SET NULL;
