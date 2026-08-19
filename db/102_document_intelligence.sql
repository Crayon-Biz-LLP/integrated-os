-- Migration 102: Document Intelligence
-- Stores uploaded documents and their parsed breakdowns for the
-- document review flow (extract → parse → show breakdown → user confirms → batch create).

CREATE TABLE IF NOT EXISTS documents (
  id SERIAL PRIMARY KEY,
  owner_id UUID NOT NULL,
  filename TEXT,
  mime_type TEXT,
  extracted_text TEXT,
  parsed_breakdown JSONB,  -- { document_type, summary, key_facts, suggested_actions }
  created_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS document_items (
  id SERIAL PRIMARY KEY,
  document_id INT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
  owner_id UUID NOT NULL,           -- denormalized for RLS (db/90 auto-policies)
  item_type TEXT NOT NULL,          -- 'task', 'event', 'note'
  item_data JSONB NOT NULL,         -- { title, owner, deadline, date, ... }
  created_entity_id UUID,           -- linked task/event/note after confirmation
  was_edited BOOLEAN DEFAULT false, -- did user edit this before confirming?
  created_at TIMESTAMPTZ DEFAULT now()
);

-- Patch: add owner_id to document_items if missing (migration ran before
-- the column was added to the migration file).
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_name = 'document_items' AND column_name = 'owner_id'
  ) THEN
    ALTER TABLE document_items ADD COLUMN owner_id UUID NOT NULL DEFAULT '00000000-0000-0000-0000-000000000000';
  END IF;
END $$;

-- Index for querying documents by owner
CREATE INDEX IF NOT EXISTS idx_documents_owner ON documents(owner_id, created_at DESC);

-- Index for querying items by document
CREATE INDEX IF NOT EXISTS idx_document_items_doc ON document_items(document_id);

-- Grant access to the API role
GRANT SELECT, INSERT, UPDATE, DELETE ON documents TO authenticated;
GRANT SELECT, INSERT, UPDATE, DELETE ON document_items TO authenticated;
GRANT USAGE, SELECT ON SEQUENCE documents_id_seq TO authenticated;
GRANT USAGE, SELECT ON SEQUENCE document_items_id_seq TO authenticated;
