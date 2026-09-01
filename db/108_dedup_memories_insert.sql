-- 108_dedup_memories_insert.sql
--
-- Root Cause: Multiple producers (web ingest, confirm flow, Guard B,
-- pulse sweep, ingest(), email/whatsapp) process the same user message
-- independently. Each does a fresh INSERT into memories, creating 4-5
-- duplicate note chains for one logical statement. Graph nodes already
-- have DB-level dedup (upsert with on_conflict); notes have none.
--
-- Fix: BEFORE INSERT trigger that checks if a recent current note with
-- the same normalized content + org + tenant already exists. The 10-minute
-- window distinguishes true duplicates (producers, same message, seconds
-- apart) from legitimate recurring notes (same content, hours/days apart).
--
-- Pattern: mirrors the existing temporal_*_update triggers (db/02, db/31,
-- db/91) — Postgres enforces the invariant at the storage layer.

CREATE OR REPLACE FUNCTION dedup_memories_insert()
RETURNS TRIGGER AS $$
DECLARE
  normalized_new TEXT;
  content_hash TEXT;
  dedup_window INTERVAL := INTERVAL '10 minutes';
BEGIN
  -- Only dedup user-facing note types (skip outcome, briefing, etc.)
  IF NEW.memory_type NOT IN ('note', 'relationship_note') THEN
    RETURN NEW;
  END IF;

  -- Only dedup within same tenant
  IF NEW.owner_id IS NULL THEN
    RETURN NEW;
  END IF;

  -- Normalize: lowercase, collapse whitespace, strip leading source prefix
  -- ("teams: Hello world" → "hello world")
  normalized_new := lower(regexp_replace(NEW.content, '\s+', ' ', 'g'));
  normalized_new := regexp_replace(normalized_new, '^[a-z_]+:\s*', '');
  normalized_new := trim(normalized_new);

  -- Skip trivially short content (tokens, single words)
  IF length(normalized_new) < 10 THEN
    RETURN NEW;
  END IF;

  -- Compute content hash for comparison
  content_hash := md5(normalized_new);

  -- Check if a RECENT current note with same normalized content + org + tenant exists.
  -- The 10-minute window is critical:
  --   - True duplicates (web ingest → confirm → pulse, same message): seconds apart ✅ caught
  --   - Recurring notes (daily standup, weekly recap): hours/days apart ✅ allowed
  --   - Same content from different threads: different sender/session, hours apart ✅ allowed
  IF EXISTS (
    SELECT 1 FROM memories
    WHERE is_current = true
      AND owner_id = NEW.owner_id
      AND memory_type = NEW.memory_type
      AND created_at >= NOW() - dedup_window
      AND md5(
            lower(regexp_replace(
              regexp_replace(content, '^[a-z_]+:\s*', ''),
              '\s+', ' ', 'g'
            ))
          ) = content_hash
      AND (
        (organization_id IS NOT NULL AND organization_id = NEW.organization_id)
        OR (organization_id IS NULL AND NEW.organization_id IS NULL)
      )
  ) THEN
    -- Duplicate detected — swallow the INSERT
    RAISE NOTICE 'dedup_memories: swallowed duplicate note (content_hash=%, org=%, window=%)',
      content_hash, NEW.organization_id, dedup_window;
    RETURN NULL;
  END IF;

  -- No duplicate — allow the INSERT
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- Create the trigger (idempotent)
DROP TRIGGER IF EXISTS trg_dedup_memories_insert ON memories;
CREATE TRIGGER trg_dedup_memories_insert
  BEFORE INSERT ON memories
  FOR EACH ROW
  EXECUTE FUNCTION dedup_memories_insert();

-- Index to speed up the EXISTS check (content hash + tenant + type + current + time)
-- Partial index: only covers the note types we dedup, only current rows
CREATE INDEX IF NOT EXISTS idx_memories_dedup_lookup
  ON memories (owner_id, memory_type, is_current, created_at, organization_id)
  WHERE is_current = true AND memory_type IN ('note', 'relationship_note');
