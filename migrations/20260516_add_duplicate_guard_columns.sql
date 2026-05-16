-- Migration: Add duplicate guard columns to email_pending_tasks
-- Run this in Supabase Dashboard SQL Editor

ALTER TABLE email_pending_tasks 
ADD COLUMN IF NOT EXISTS possible_duplicate boolean DEFAULT NULL,
ADD COLUMN IF NOT EXISTS duplicate_of_title text DEFAULT NULL;
