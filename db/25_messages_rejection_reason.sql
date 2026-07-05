-- db/25_messages_rejection_reason.sql
-- Adds rejection_reason column to the messages table so Rhodey can learn
-- *why* you rejected an item, not just that you rejected it.
--
-- Rejection reasons are inferred from available context at decision time:
--   wrong_project    — item has a suggested_project but it doesn't match the channel
--   unknown_sender   — sender is not in the people graph
--   duplicate        — similar content already processed
--   no_content       — empty or useless body/summary
--   not_actionable   — classified as FYI / informational
--   other            — catch-all

ALTER TABLE public.messages
    ADD COLUMN IF NOT EXISTS rejection_reason TEXT;

COMMENT ON COLUMN public.messages.rejection_reason IS
    'Inferred reason for rejection: wrong_project, unknown_sender, duplicate, no_content, not_actionable, other. Used by pattern learner to split on why, not just whether.';
