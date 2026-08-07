-- 89_entity_briefs_owner_pk.sql
-- M9.2 tenant-safety hygiene (plans/70 §M9.2 Step 0).
--
-- `entity_briefs` was created (db/48) with a GLOBAL primary key on
-- entity_name. db/88 converted the graph-unique indexes to (owner_id, ...)
-- composites but did NOT touch this table — two tenants' sentinels writing
-- a brief for the same entity name would collide on the PK, and the S2
-- importance gate must never read another tenant's rows.
--
-- This migration:
--   1. Guards: every existing row must have owner_id (the
--      migrate_danny_to_tenant1.py backfill guarantees this) — aborts loudly
--      instead of silently dropping a PK.
--   2. Drops the global entity_name PK.
--   3. Sets owner_id NOT NULL (db/78 added it nullable).
--   4. Recreates the PK as (owner_id, entity_name) — same entity name is
--      fine across tenants, never within one.
--
-- Idempotent: safe to re-run; constraint guards on existence.

-- 1. Fail loudly if any row predates tenant attribution.
DO $$
DECLARE
    null_owners bigint;
BEGIN
    SELECT count(*) INTO null_owners
    FROM public.entity_briefs
    WHERE owner_id IS NULL;
    IF null_owners > 0 THEN
        RAISE EXCEPTION 'entity_briefs has % row(s) with NULL owner_id — run scripts/migrate_danny_to_tenant1.py --apply first', null_owners;
    END IF;
END
$$;

-- 2. Drop the global PK if it still exists.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'entity_briefs_pkey'
          AND conrelid = 'public.entity_briefs'::regclass
    ) THEN
        ALTER TABLE public.entity_briefs DROP CONSTRAINT entity_briefs_pkey;
    END IF;
END
$$;

-- 3. owner_id becomes mandatory.
ALTER TABLE public.entity_briefs ALTER COLUMN owner_id SET NOT NULL;

-- 4. Composite PK — the tenant-scoped identity.
ALTER TABLE public.entity_briefs ADD PRIMARY KEY (owner_id, entity_name);
