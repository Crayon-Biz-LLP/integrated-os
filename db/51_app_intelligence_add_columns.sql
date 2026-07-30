-- Add missing columns to app_intelligence table for Phase 2 v2 features.
-- 
-- home_mode:        Controls the Flutter home screen layout (proceed|decide|sprint|catch_up|wrap)
-- top_focal_item:   JSON payload from the LLM's top focal item selection
-- transparency_report: Weekly "What I Learned This Week" report (Sundays only)
-- context_bar:      Short contextual label for the app header

ALTER TABLE app_intelligence 
  ADD COLUMN IF NOT EXISTS home_mode TEXT DEFAULT 'proceed',
  ADD COLUMN IF NOT EXISTS top_focal_item JSONB DEFAULT NULL,
  ADD COLUMN IF NOT EXISTS transparency_report TEXT DEFAULT NULL,
  ADD COLUMN IF NOT EXISTS context_bar TEXT DEFAULT NULL;
