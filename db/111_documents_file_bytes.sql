-- db/111: Retain uploaded document bytes for Gemini-native document understanding.
--
-- Root cause chain (Sep 2026 document-card investigation): the upload path
-- (api/index.py multimodal_input_route) discarded file bytes after text
-- extraction, so document intelligence ran on lossy PyMuPDF text soup inside
-- the chat mega-prompt. Gemini structured output left the unconstrained
-- params object empty ("params": {}), producing blank suggestion-card rows.
--
-- Fix: store the original document bytes so the parser can send the REAL
-- document to Gemini (native document understanding) instead of re-reading
-- extracted text. Additive + idempotent; NULL for legacy rows (they keep
-- using the text path).

ALTER TABLE documents ADD COLUMN IF NOT EXISTS file_bytes BYTEA;

COMMENT ON COLUMN documents.file_bytes IS
  'Original uploaded document bytes (PDF etc.) for Gemini-native document understanding. NULL = legacy row or non-retained upload; parser falls back to extracted_text.';
