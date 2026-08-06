-- db/87 — Grant API roles on tenant tables created by db/78–86.
--
-- Root cause (found during cutover Step 9): the db/78–86 migrations were
-- applied over the Supabase pooler connection with psql. Supabase's
-- automatic default privileges only apply to objects created through its
-- own tooling, so the NEW tables (users, user_settings, user_oauth_tokens,
-- llm_spend) had ZERO grants for the PostgREST API roles. Every API query
-- against them failed with 42501 "permission denied for table users" —
-- visible live as resolve_user_by_api_key() failing → per-user API key
-- auth returning 401.
--
-- Old tables (created via the dashboard) carry anon/authenticated/service_role
-- grants; this migration restores that identical posture for the new tables.
-- RLS is still disabled project-wide (planned later), matching the existing
-- posture for every other table.
--
-- Idempotent — safe to re-run.

grant usage on schema public to anon, authenticated, service_role;

grant all on table public.users, public.user_settings,
            public.user_oauth_tokens, public.llm_spend
    to anon, authenticated, service_role;

-- llm_spend.id is an identity column (db/85) — the API roles need the
-- sequence for inserts. Harmless on tables with uuid keys.
grant all on all sequences in schema public to anon, authenticated, service_role;

-- Future-proof: tables created by later pooler/psql migrations get the same
-- grants automatically (this is what Supabase's own defaults should have done).
alter default privileges in schema public grant all on tables to anon, authenticated, service_role;
alter default privileges in schema public grant all on sequences to anon, authenticated, service_role;
alter default privileges in schema public grant all on functions to anon, authenticated, service_role;
