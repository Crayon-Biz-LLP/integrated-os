# 70. Multi-Tenant Architecture (M3)

> Verified against code 2026-08-15. M3 shipped Aug 2026; DB grants hardened in
> db/87–91, RPC owner-scoping in db/78–84.

## The isolation model

Every data table carries an **`owner_id UUID`** column naming its tenant. The
app never touches tables directly — all access flows through the tenant facade
in `core/services/db.py`:

- **`set_tenant(user_id)` / `get_tenant()`** — ambient tenant for the current
  execution context (a `contextvars.ContextVar`).
- **`require_tenant()`** — fail-closed: raises `TenantRequiredError` when
  tenant-scoped access is attempted outside a tenant context.
- **`tenant_scope(user_id)`** — context manager: runs a block under a tenant,
  restores the previous tenant on exit. This is how the webhook, the Pulse, and
  the API routes bound work to a user.
- **`TenantAwareClient` / `tenant_table(name)`** — the facade. On writes it
  calls `_inject_owner(data, uid)` to stamp `owner_id`; on reads it filters by
  the ambient tenant. The webhook resolves which tenant owns an incoming chat
  via `resolve_channel_tenant()` (channel → tenant binding, e.g.
  `users.telegram_chat_id`).

## The two escape hatches (deliberate)

1. **`_TENANT_KEYED_TABLES = {users, user_settings, user_oauth_tokens}`** — these
   are *keyed by the tenant itself* (no `owner_id` column), so the facade passes
   them through unscoped; callers always filter on `user_id`/`id` explicitly.
2. **`_GLOBAL_RPCS = {"run_sql"}`** — the only unscoped RPC surface (admin
   diagnostics). Everything else in the RPC layer is owner-scoped (db/78–84
   rework); `tests/tenants/test_db_isolation.py` enforces that every scoped RPC
   carries an owner parameter.

## Database layer

- **RLS grants reworked (db/87–91):** the anon role was revoked; per-tenant
  roles were introduced. The service-role key still bypasses RLS server-side.
- **`llm_spend` + `users.monthly_credit_usd` / `credit_cycle_day`** — per-tenant
  LLM cost metering and spend caps (M6).
- **`MAX_TENANTS`** env (0 = unlimited) caps self-serve sign-up at the
  provisioning layer (`core/services/auth.py`), failing with a clear
  "cap reached" error instead of silently widening the fleet.

## Cross-tenant leak enforcement (test suite)

- `tests/tenants/test_db_isolation.py` — DB-level isolation matrix on a copy DB:
  user A sees exactly their marker rows, never user B's, for every key tenant
  table, plus the owner-param RPC matrix.
- The live-suite **leak guard** (`tests/conftest.py`) fails the whole session if
  any `[TEST]`/`[SIM_TEST]`/`[UAT]` marker row is owned by a non-test tenant —
  the sandbox contract that makes concurrent CI/local runs safe.

## What is NOT multi-tenant

- `login_otps` (auth state, not tenant data), `core_config` rows are per-owner.
- Canonical pages and memories carry `owner_id` like everything else; brain
  synthesis is per-tenant, never cross-tenant.
