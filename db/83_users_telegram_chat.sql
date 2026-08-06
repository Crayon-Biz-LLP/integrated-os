-- 83_users_telegram_chat.sql
-- ============================================================================
-- M4: per-user Telegram chat id (cron fan-out, plans/69-multi-tenant-product-plan.md)
--
-- The cron pulses (sentinel / decision-pulse / roundup) iterate all active
-- users. Telegram is an OPTIONAL per-user channel — the Android app is the
-- primary one. This column lets each tenant have their own chat id (or NULL
-- = app-only, skip Telegram sends).
--
-- Resolution (core/services/db.py resolve_telegram_chat_id):
--   1. users.telegram_chat_id for the tenant being served
--   2. env TELEGRAM_CHAT_ID while exactly one active user exists (legacy
--      single-user world — the env chat belongs to that one user)
--   3. env TELEGRAM_CHAT_ID on unscoped legacy paths (pre-db/78)
--
-- No backfill needed: Danny's env chat keeps working via rule 2 until his
-- row is given an explicit telegram_chat_id.
-- ============================================================================

alter table public.users
    add column telegram_chat_id text;
