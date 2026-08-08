-- 90_rls_tenant_isolation.sql
-- W1 (enterprise-grade hardening): DB-ENFORCED tenant isolation — RLS as the
-- last line of defense behind the application facade.
--
-- Why: isolation was previously app-enforced only (TenantTable facade +
-- owner_id). A raw client, leaked credential, or forgotten scope could cross
-- tenants. Worse, 43 of the 59 owner tables already had RLS *enabled* but with
-- Supabase's auto-generated junk policies ("Allow anon read on graph_nodes"
-- with qual=true — a full cross-tenant read for ANY role), and 0 tables had
-- FORCE RLS. This migration makes the database itself refuse cross-tenant
-- access for a dedicated non-bypass role.
--
-- Design:
--   1. Create `rhodey_app` (LOGIN, NOBYPASSRLS) — the ONLY role that must
--      enforce isolation. service_role/postgres keep BYPASSRLS for admin ops.
--   2. Drop every pre-existing policy on the owner-scoped tables (they are
--      all junk qual=true or ours).
--   3. Create owner-scoped policies TO rhodey_app:
--        USING/WITH CHECK (owner_id = current_setting('app.tenant_id')::uuid)
--      and user_id/id variants for the tenant-keyed tables (users,
--      user_settings, user_oauth_tokens).
--   4. ENABLE + FORCE ROW LEVEL SECURITY on all of them (FORCE subjects even
--      the table owner to policies).
--
-- Fail-closed by construction: with app.tenant_id unset, the expression is
-- NULL → every row fails the policy → 0 rows visible, inserts rejected.
--
-- Prerequisite (password never lives in the repo):
--     export RHODEY_APP_DB_PASSWORD="$(openssl rand -hex 24)"
--     psql "$DSN" -v ON_ERROR_STOP=1 -v rhodey_pw="$RHODEY_APP_DB_PASSWORD" \
--          -f db/90_rls_tenant_isolation.sql
-- Idempotent: role/grants/policies all guarded; re-running re-syncs state.-- ── 0. Pre-flight: a real password must be supplied via psql -v ───────────
-- psql variables are NOT interpolated inside $$...$$ blocks, so the length
-- check uses psql's own \if/\quit instead of a DO block.
\set ON_ERROR_STOP on
SELECT length(:'rhodey_pw') AS rhodey_pw_len \gset
\if :rhodey_pw_len < 24
\echo ERROR: RHODEY_APP_DB_PASSWORD must be >= 24 chars — generate with: openssl rand -hex 24 (pass via -v rhodey_pw=...)
\quit
\endif

-- ── 1. Role: idempotent create + enforce attributes ────────────────────────
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'rhodey_app') THEN
        CREATE ROLE rhodey_app LOGIN;
    END IF;
END
$$;

ALTER ROLE rhodey_app LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS
    PASSWORD :'rhodey_pw';

-- ── 2. Grants (connect + schema + tables + sequences) ──────────────────────
GRANT CONNECT ON DATABASE postgres TO rhodey_app;
GRANT USAGE ON SCHEMA public TO rhodey_app;

DO $$
DECLARE t text;
BEGIN
    FOR t IN
        SELECT table_name FROM information_schema.columns
        WHERE table_schema = 'public' AND column_name = 'owner_id'
        UNION
        SELECT unnest(ARRAY['users', 'user_settings', 'user_oauth_tokens'])
    LOOP
        EXECUTE format('GRANT ALL ON TABLE public.%I TO rhodey_app', t);
    END LOOP;
END
$$;

-- Sequences needed for serial id columns on insert.
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO rhodey_app;

-- ── 3. RLS: drop junk policies, create tenant-scoped ones, FORCE ───────────
DO $$
DECLARE
    t text;
    c text;
    p text;
BEGIN
    FOR t, c IN
        SELECT table_name, 'owner_id' FROM information_schema.columns
        WHERE table_schema = 'public' AND column_name = 'owner_id'
        UNION ALL
        SELECT 'users', 'id'
        UNION ALL
        SELECT 'user_settings', 'user_id'
        UNION ALL
        SELECT 'user_oauth_tokens', 'user_id'
    LOOP
        -- Drop every pre-existing policy (junk qual=true or previous runs).
        FOR p IN
            SELECT policyname FROM pg_policies
            WHERE schemaname = 'public' AND tablename = t
        LOOP
            EXECUTE format('DROP POLICY IF EXISTS %I ON public.%I', p, t);
        END LOOP;

        EXECUTE format('ALTER TABLE public.%I ENABLE ROW LEVEL SECURITY', t);
        EXECUTE format('ALTER TABLE public.%I FORCE ROW LEVEL SECURITY', t);

        EXECUTE format(
            'CREATE POLICY rls_tenant_select ON public.%I FOR SELECT TO rhodey_app USING (%I = current_setting(''app.tenant_id'', true)::uuid)',
            t, c);
        EXECUTE format(
            'CREATE POLICY rls_tenant_insert ON public.%I FOR INSERT TO rhodey_app WITH CHECK (%I = current_setting(''app.tenant_id'', true)::uuid)',
            t, c);
        EXECUTE format(
            'CREATE POLICY rls_tenant_update ON public.%I FOR UPDATE TO rhodey_app USING (%I = current_setting(''app.tenant_id'', true)::uuid) WITH CHECK (%I = current_setting(''app.tenant_id'', true)::uuid)',
            t, c, c);
        EXECUTE format(
            'CREATE POLICY rls_tenant_delete ON public.%I FOR DELETE TO rhodey_app USING (%I = current_setting(''app.tenant_id'', true)::uuid)',
            t, c);
    END LOOP;
END
$$;

-- ── 4. Verification helper (runs in SQL editor as postgres) ─────────────────
--   select tablename, count(*) filter (where policyname like 'rls_tenant_%')
--   from pg_policies where schemaname='public'
--   group by 1 having count(*) filter (where policyname like 'rls_tenant_%') <> 4
--   order by 1;
