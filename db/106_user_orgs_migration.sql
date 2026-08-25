-- 106_user_orgs_migration.sql
-- ============================================================================
-- M17: Consolidate domains + personal_orgs → user_orgs (with is_personal flag)
--
-- Phase 1 of the user_orgs refactor. Run BEFORE deploying code changes.
-- Phase 8 (drop old columns) runs AFTER all code is verified.
-- ============================================================================

-- Step 1: Add user_orgs column (idempotent)
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name = 'user_settings' AND column_name = 'user_orgs') THEN
    ALTER TABLE public.user_settings ADD COLUMN user_orgs jsonb;
  END IF;
END $$;

-- Step 2: Migrate data from domains
-- Handles legacy string-array format (tenant #1)
-- COALESCE prevents NULL agg when domains is empty/NULL
UPDATE public.user_settings
SET user_orgs = (
  SELECT COALESCE(jsonb_agg(
    jsonb_build_object(
      'name', elem->>'name',
      'keywords', COALESCE(
        CASE WHEN jsonb_typeof(elem->'keywords') = 'array' THEN elem->'keywords' ELSE '[]'::jsonb END,
        '[]'::jsonb
      ),
      'is_personal', CASE
        WHEN elem->>'name' = ANY(
          SELECT jsonb_array_elements_text(
            CASE WHEN jsonb_typeof(personal_orgs) = 'array' THEN personal_orgs ELSE '[]'::jsonb END
          )
        ) THEN true
        ELSE false
      END
    )
  ), '[]'::jsonb)
  FROM jsonb_array_elements(
    CASE WHEN jsonb_typeof(domains) = 'array' THEN domains ELSE '[]'::jsonb END
  ) AS elem
);

-- Step 3: Add personal_orgs-only names not already in user_orgs
-- Catches sub-orgs like "Ashraya Chennai", "Chennai North" that exist in
-- personal_orgs but not in domains. These must be preserved for the
-- briefing work/life filter.
UPDATE public.user_settings
SET user_orgs = user_orgs || (
  SELECT COALESCE(jsonb_agg(
    jsonb_build_object('name', po, 'keywords', '[]'::jsonb, 'is_personal', true)
  ), '[]'::jsonb)
  FROM jsonb_array_elements_text(
    CASE WHEN jsonb_typeof(personal_orgs) = 'array' THEN personal_orgs ELSE '[]'::jsonb END
  ) AS po
  WHERE po NOT IN (
    SELECT elem->>'name' FROM jsonb_array_elements(
      CASE WHEN jsonb_typeof(user_orgs) = 'array' THEN user_orgs ELSE '[]'::jsonb END
    ) AS elem
  )
);

-- Step 4: Catch any remaining NULLs (NULL || anything = NULL in PostgreSQL)
UPDATE public.user_settings SET user_orgs = '[]'::jsonb WHERE user_orgs IS NULL;

-- Step 5: Verify migration
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM user_settings WHERE user_orgs IS NULL AND (domains IS NOT NULL OR personal_orgs IS NOT NULL)) THEN
    RAISE EXCEPTION 'Migration incomplete: some rows have NULL user_orgs';
  END IF;
END $$;

-- Step 6: Drop old columns (after code deployment — deferred to Phase 8)
-- ALTER TABLE public.user_settings DROP COLUMN domains;
-- ALTER TABLE public.user_settings DROP COLUMN personal_orgs;
