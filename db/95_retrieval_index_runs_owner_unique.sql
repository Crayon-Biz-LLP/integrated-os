-- db/95: owner-scope the retrieval_index_runs unique key (M16 fan-out prerequisite)
--
-- Root Cause: before M16 the retrieval backfill ran only for the channel
-- tenant (or failed closed unscoped), so the global unique
-- (source_type, source_id, index_version) never collided. M16 makes the
-- backfill run per active tenant; two tenants indexing the same source
-- key (e.g. memory id "500" exists in both worlds) would collide on the
-- global unique — and the tenant-aware upsert would then update the OTHER
-- tenant's row (cross-tenant write).
--
-- Fix: make the unique key composite with owner_id, matching the db/89
-- pattern applied to every other owner-scoped table. No pre-existing
-- cross-tenant duplicates exist (verified live: all rows owned by one
-- tenant), so the constraint is created without a data migration.

-- Drop either form (constraint or plain unique index) — idempotent.
ALTER TABLE public.retrieval_index_runs
  DROP CONSTRAINT IF EXISTS retrieval_index_runs_source_type_source_id_index_version_key;
DROP INDEX IF EXISTS retrieval_index_runs_source_type_source_id_index_version_key;

-- Owner-scoped unique (order matches the app's on_conflict target:
-- "owner_id, source_type, source_id, index_version").
ALTER TABLE public.retrieval_index_runs
  ADD CONSTRAINT retrieval_index_runs_owner_source_type_source_id_index_version_key
  UNIQUE (owner_id, source_type, source_id, index_version);
