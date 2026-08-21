-- db/105: Guaranteed org linkage for all entities
--
-- Problem: Tasks, notes, and persons can be created without org linkage.
-- When the org is new (not in graph_nodes yet), there's nothing to link to.
-- The entity exists, the task exists, but they're disconnected.
--
-- Solution: Add pending_org_id columns so entities can link to pending orgs.
-- On approval, pending_org_id resolves to organization_id.
--
-- Also adds entity context columns to the enrichment queue so the queue
-- can use pre-extracted entities instead of re-extracting.

-- ════════════════════════════════════════════════════════════════════════
-- Part 1: pending_org_id on tasks and memories
-- ════════════════════════════════════════════════════════════════════════

ALTER TABLE tasks ADD COLUMN IF NOT EXISTS pending_org_id BIGINT;
ALTER TABLE memories ADD COLUMN IF NOT EXISTS pending_org_id BIGINT;

-- FK to pending_nodes (nullable — only set for newly proposed orgs)
-- ON DELETE SET NULL is a safety net; the rejection handler is the primary mechanism.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'fk_tasks_pending_org'
    ) THEN
        ALTER TABLE tasks ADD CONSTRAINT fk_tasks_pending_org
            FOREIGN KEY (pending_org_id) REFERENCES public.pending_nodes(id)
            ON DELETE SET NULL;
    END IF;
END $$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'fk_memories_pending_org'
    ) THEN
        ALTER TABLE memories ADD CONSTRAINT fk_memories_pending_org
            FOREIGN KEY (pending_org_id) REFERENCES public.pending_nodes(id)
            ON DELETE SET NULL;
    END IF;
END $$;

-- Indexes for the approval resolution query
CREATE INDEX IF NOT EXISTS idx_tasks_pending_org
    ON tasks(pending_org_id) WHERE pending_org_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_memories_pending_org
    ON memories(pending_org_id) WHERE pending_org_id IS NOT NULL;

-- ════════════════════════════════════════════════════════════════════════
-- Part 2: Entity context on enrichment queue
-- ════════════════════════════════════════════════════════════════════════

-- full_text: original message text (not just title) for better entity detection
-- pending_org_id: pending org from EntityContext
-- entity_context: serialized EntityContext dict (JSONB)

ALTER TABLE pending_enrichment_jobs
    ADD COLUMN IF NOT EXISTS full_text TEXT,
    ADD COLUMN IF NOT EXISTS pending_org_id BIGINT,
    ADD COLUMN IF NOT EXISTS entity_context JSONB;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'fk_enrichment_pending_org'
    ) THEN
        ALTER TABLE pending_enrichment_jobs
            ADD CONSTRAINT fk_enrichment_pending_org
            FOREIGN KEY (pending_org_id) REFERENCES pending_nodes(id)
            ON DELETE SET NULL;
    END IF;
END $$;

-- ════════════════════════════════════════════════════════════════════════
-- Verification
-- ════════════════════════════════════════════════════════════════════════

SELECT 'tasks pending_org_id: ' || COUNT(*)::TEXT FROM tasks WHERE pending_org_id IS NOT NULL;
SELECT 'memories pending_org_id: ' || COUNT(*)::TEXT FROM memories WHERE pending_org_id IS NOT NULL;
SELECT 'enrichment jobs with entity_context: ' || COUNT(*)::TEXT FROM pending_enrichment_jobs WHERE entity_context IS NOT NULL;
