-- db/91: Revoke anon/authenticated from the public schema (hardening).
--
-- Context: the app talks to the DB only via:
--   • service_role (PostgREST/supabase-py)  — BYPASSRLS, has its own grants
--   • rhodey_app (asyncpg, db/90)           — NOBYPASSRLS, RLS-enforced
-- The Flutter app and web dashboard authenticate through Modal with per-user
-- API keys; the dashboard's data queries use SUPABASE_SERVICE_ROLE_KEY. The
-- anon key is used only for Supabase Auth's session exchange (auth schema,
-- untouched here).
--
-- anon/authenticated currently hold Supabase's default table grants on the
-- public schema. Those grants are INERT today (FORCE RLS + zero policies for
-- these roles → 0 rows), but they become a LIVE read/write path the moment a
-- future table is created without RLS (Supabase auto-grants to anon by
-- default). This migration makes the deny explicit and permanent — and sets
-- default privileges so future objects never re-open the door.
--
-- Guarded for idempotency + environments where the Supabase roles don't exist
-- (local scratch DBs): each statement runs only if the roles exist.

-- 1. Existing objects: drop every privilege anon/authenticated hold.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon')
       AND EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
        EXECUTE 'REVOKE ALL ON ALL TABLES IN SCHEMA public FROM anon, authenticated';
        EXECUTE 'REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM anon, authenticated';
        EXECUTE 'REVOKE ALL ON ALL FUNCTIONS IN SCHEMA public FROM anon, authenticated';
    END IF;
END $$;

-- 2. Future objects: default privileges for the role that creates tables
--    (postgres, via SQL editor / migrations) — no silent re-grant.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon')
       AND EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
        EXECUTE 'ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON TABLES FROM anon, authenticated';
        EXECUTE 'ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON SEQUENCES FROM anon, authenticated';
        EXECUTE 'ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON FUNCTIONS FROM anon, authenticated';
    END IF;
END $$;

-- Notes on remaining default ACLs (verified live, safe to leave):
--   postgres|storage, supabase_admin|graphql/graphql_public/supabase_functions
-- still grant anon/authenticated — these are Supabase platform schemas whose
-- defaults are owned by supabase_admin (superuser); the app never reads them
-- and the platform requires them for Storage/GraphQL/Functions to work.
--
--   supabase_admin also owns 3 PUBLIC-schema default ACLs (r/S/f) that the
--   pooler cannot revoke (permission denied — needs superuser). Optional
--   cleanup via the Supabase SQL editor (once, as superuser):
--     ALTER DEFAULT PRIVILEGES FOR ROLE supabase_admin IN SCHEMA public
--       REVOKE ALL ON TABLES FROM anon, authenticated;
--     ALTER DEFAULT PRIVILEGES FOR ROLE supabase_admin IN SCHEMA public
--       REVOKE ALL ON SEQUENCES FROM anon, authenticated;
--     ALTER DEFAULT PRIVILEGES FOR ROLE supabase_admin IN SCHEMA public
--       REVOKE ALL ON FUNCTIONS FROM anon, authenticated;
--   Effect is limited: migrations run as postgres (defaults now clean), so
--   future app tables are already covered; this only closes the edge case of
--   a table created AS supabase_admin.
--
-- Verification (expected: 0 rows both queries):
--   select count(*) from information_schema.role_table_grants
--    where table_schema='public' and grantee in ('anon','authenticated');
--   select count(*) from pg_default_acl d join pg_namespace n on n.oid=d.defaclnamespace
--    where n.nspname='public' and d.defaclacl::text ilike '%anon%';
