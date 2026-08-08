-- 93: user_settings.persona — the onboarding role choice (M15).
--
-- A vocabulary layer ONLY: the persona renames the app's surfaces and
-- onboarding copy (Today/Inbox/Entities/History, focal, pulse). The engine,
-- briefings, focal-card decisions and approvals stay shared — one voice for
-- everyone until the per-persona server copy ships in a later release.
-- Additive + safe: absent for all existing tenants (they keep today's
-- 'chief_staff' vocabulary), set only by new onboarding journeys.

alter table public.user_settings
    add column if not exists persona text;
