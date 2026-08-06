-- 84_user_oauth_tokens.sql
-- ============================================================================
-- M5: Per-tenant Google OAuth — plans/69-multi-tenant-product-plan.md §8
--
-- Replaces the single global GOOGLE_REFRESH_TOKEN env credential with a
-- per-user refresh token. Each tenant connects their own Google account
-- during onboarding (M5); the pulse/sentinel/ingest paths then build
-- Google API credentials for the tenant they are currently serving.
--
-- The env var stays as the LEGACY fallback (single-user pre-M0 mode and
-- Danny's pre-cutover behaviour) — see google_service.get_google_creds().
--
--   user_oauth_tokens (user_id, provider, refresh_token, scopes, updated_at)
--   users.google_connected  (bootstrap/OAuth script flips it on success)
-- ============================================================================

create table if not exists public.user_oauth_tokens (
    id            uuid primary key default gen_random_uuid(),
    user_id       uuid not null references public.users(id) on delete cascade,
    provider      text not null default 'google',
    refresh_token text not null,
    scopes        text,
    created_at    timestamptz not null default now(),
    updated_at    timestamptz not null default now()
);

create unique index if not exists idx_user_oauth_tokens_user_provider
    on public.user_oauth_tokens (user_id, provider);

-- Fast lookup of a tenant's refresh token per provider.
create index if not exists idx_user_oauth_tokens_user
    on public.user_oauth_tokens (user_id);

-- Onboarding marker: lets the app/briefing tell a "connected" tenant apart
-- from one that still needs the Google step.
alter table public.users
    add column if not exists google_connected boolean not null default false;
