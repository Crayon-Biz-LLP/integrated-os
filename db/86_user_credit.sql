-- 86_user_credit.sql — per-user monthly LLM credit (M6 cost controls, v2).
--
-- The product model: each user is allocated a monthly credit (USD) that the
-- OPERATOR edits in the users table (or via scripts/set_user_credit.py).
-- The credit cycle resets on the user's signup day-of-month (anniversary
-- billing). Spend is read from the llm_spend ledger (db/85); the budget is
-- whatever the operator set — nothing cost-related lives in code defaults
-- except a NULL fallback for legacy/unset rows.
--
--    users.monthly_credit_usd  — the per-user monthly credit (NULL = use
--                                 the code default; set per row to control)
--    users.credit_cycle_day    — day-of-month the cycle resets (1-31,
--                                 clamped to month length); NULL = signup day
--                                 (created_at::date day-of-month)
--
-- Editable via: Supabase table editor, or
--   python scripts/set_user_credit.py --user Danny --usd 5 --apply

alter table public.users
    add column if not exists monthly_credit_usd numeric(10, 2);

alter table public.users
    add column if not exists credit_cycle_day int;

-- Idempotent CHECK (Postgres has no ADD CONSTRAINT IF NOT EXISTS).
do $$
begin
    if not exists (
        select 1 from pg_constraint
        where conrelid = 'public.users'::regclass
          and conname = 'users_monthly_credit_usd_nonneg'
    ) then
        alter table public.users
            add constraint users_monthly_credit_usd_nonneg
            check (monthly_credit_usd is null or monthly_credit_usd >= 0);
    end if;
end
$$;

-- Existing rows: cycle = their signup day (backfill for clarity; the code
-- falls back to created_at day-of-month when NULL anyway).
update public.users
set credit_cycle_day = extract(day from created_at)::int
where credit_cycle_day is null;
