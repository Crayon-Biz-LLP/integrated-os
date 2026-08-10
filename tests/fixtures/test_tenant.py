"""Shared test-tenant resolution for live-DB integration suites.

The integration suites (tests/sim, tests/clusters) insert real rows into the
Supabase project named by SUPABASE_URL. To guarantee zero cross-tenant
leakage, every insert and every cleanup must be scoped to ONE dedicated
tenant — the "Test" user. That tenant doubles as the manual-testing user on
the second phone, so its rows are real user data as far as the app is
concerned.

Resolution order:
  1. TEST_TENANT_UID env var (explicit — CI sets this)
  2. users row with name='Test' and status='active' (the canonical test tenant)

If neither exists the integration suites SKIP — they must never fall back to
the channel tenant (Danny's oldest-user fallback), because that is exactly
the cross-tenant leak the M3 refactor was built to prevent.
"""

import os

import httpx
from supabase import create_client
from supabase.lib.client_options import SyncClientOptions

# The canonical test-tenant user name. Change via env for CI isolation.
TEST_TENANT_NAME = os.getenv("TEST_TENANT_NAME", "Test")

# Shared transport: supabase-py 2.29+ passes deprecated timeout/verify kwargs
# to the (lazy) PostgREST client unless an explicit http_client is provided
# (silences ~2 DeprecationWarnings per query builder). One module-level client
# per process — env is constant per pytest run — so live-DB suites (which call
# fresh_supabase() in probes, seed, and cleanup dozens of times) don't leak a
# connection pool per call.
_HTTPX = httpx.Client(timeout=120.0)


def fresh_supabase():
    """Build a supabase client straight from the environment.

    Deliberately does NOT use core.services.db.get_supabase(): that function
    caches a module-level singleton, and tests/unit/test_url_shortcut.py
    replaces that singleton with a MagicMock at import time (and never
    restores it). Test infrastructure that must talk to a REAL database —
    tenant resolution, live-DB probes, leak verification — therefore builds
    its own client from env so no other test can mock it out from under us.
    """
    options = SyncClientOptions(httpx_client=_HTTPX)
    return create_client(
        os.environ.get("SUPABASE_URL"),
        os.environ.get("SUPABASE_SERVICE_ROLE_KEY"),
        options=options,
    )


def resolve_test_tenant_uid() -> str | None:
    """Return the test tenant's user id, or None when it cannot be resolved.

    Never falls back to the channel tenant: when there is no resolvable test
    tenant the integration suites skip instead of risking another tenant's
    data (oldest-active-user fallback would silently target Danny).
    """
    explicit = os.getenv("TEST_TENANT_UID")
    if explicit:
        return explicit.strip() or None
    try:
        res = (
            fresh_supabase()
            .table("users")
            .select("id")
            .eq("name", TEST_TENANT_NAME)
            .eq("status", "active")
            .limit(1)
            .maybe_single()
            .execute()
        )
        if res and res.data and res.data.get("id"):
            return res.data["id"]
    except Exception:
        pass  # unreachable / pre-db/78 — caller decides to skip
    return None
