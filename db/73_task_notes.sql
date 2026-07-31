-- 73_task_notes.sql
-- Adds a free-text `notes` column to tasks so the app's focal card can show
-- the original message context ("what you said when this task was created")
-- alongside the task title. Populated at creation time by create_task_direct
-- from the executor's original message text.
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS notes TEXT DEFAULT NULL;
