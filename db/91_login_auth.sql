-- M11: Sign-in auth (Google + email/OTP).
--
-- `login_otps` is AUTH state, not tenant data — it lives outside the
-- owner-scoped 59-table set because OTP rows are created BEFORE a tenant
-- is resolved (email → code → match → key issuance). Keep it out of RLS
-- tenant policies; the service_role key is the only writer/reader.
--
-- The unique partial index on users.email makes sign-in-by-email
-- unambiguous. Provisioning (bootstrap_tenant.py --email) must supply
-- distinct addresses; the index fails loudly if an operator creates a
-- duplicate.

-- ── 1. login_otps ─────────────────────────────────────────────────────────
create table if not exists public.login_otps (
    id           uuid primary key default gen_random_uuid(),
    email        text not null,
    code_hash    text not null,          -- sha256(email:code:pepper), never the code
    attempts     int  not null default 0, -- brute-force counter (max 5)
    expires_at   timestamptz not null,    -- 10-minute TTL
    consumed_at  timestamptz,             -- single-use marker
    created_at   timestamptz not null default now()
);

-- Fast lookup of the latest OTP per email (rate-limit + verify).
create index if not exists login_otps_email_created_idx
    on public.login_otps (email, created_at desc);

-- ── 2. users.email uniqueness ─────────────────────────────────────────────
-- One provisioned account per address → sign-in resolution is 1:1.
-- Partial so existing rows with NULL email (Danny-era) stay untouched.
create unique index if not exists users_email_uq
    on public.users (lower(email)) where email is not null;
