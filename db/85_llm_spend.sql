-- 85_llm_spend.sql — per-LLM-call spend ledger (M6 cost controls,
-- plans/69-multi-tenant-product-plan.md §M6).
--
-- One row per LLM call outcome. Doubles as:
--   1. the durable source for the per-tenant daily budget check
--      (SUM(est_cost_usd) WHERE owner_id = ? AND ts >= today)
--   2. the spend telemetry (cost-per-user per day/week).
--
-- owner_id is the tenant (users.id). The M3 tenant facade stamps it on
-- insert and filters on select, so rows are per-tenant by construction.

create table if not exists public.llm_spend (
    id            bigint generated always as identity primary key,
    owner_id      uuid not null,
    ts            timestamptz not null default now(),
    model         text not null,
    provider      text,
    workload      text,
    input_tokens  int not null default 0,
    output_tokens int not null default 0,
    est_cost_usd  numeric(10, 6) not null default 0,
    outcome       text
);

create index if not exists llm_spend_owner_ts_idx
    on public.llm_spend (owner_id, ts);

-- Grants mirror the other data tables (service-role does the work; anon
-- never reads another tenant's spend).
alter table public.llm_spend enable row level security;

create policy "llm_spend owner access"
    on public.llm_spend
    for all
    using (owner_id = auth.uid())
    with check (owner_id = auth.uid());
