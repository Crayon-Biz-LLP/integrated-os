-- 107_drop_old_columns.sql
-- ============================================================================
-- M17: Drop legacy domains + personal_orgs columns (Phase 8 of user_orgs refactor)
--
-- ONLY run AFTER all code is deployed and verified to read/write user_orgs.
-- The user_orgs column (added by migration 106) is the single source of truth.
-- ============================================================================

-- Safety check: ensure no rows have NULL user_orgs where old columns had data
DO $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM user_settings
    WHERE user_orgs IS NULL
      AND (domains IS NOT NULL OR personal_orgs IS NOT NULL)
  ) THEN
    RAISE EXCEPTION 'Cannot drop columns: some rows have NULL user_orgs with non-NULL legacy data. Run migration 106 first.';
  END IF;
END $$;

ALTER TABLE public.user_settings DROP COLUMN IF EXISTS domains;
ALTER TABLE public.user_settings DROP COLUMN IF EXISTS personal_orgs;
