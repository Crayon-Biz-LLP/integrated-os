"""
tests/clusters — DB-backed behavioral suite.

Every test in this directory inserts rows into the Supabase project named by
SUPABASE_URL and runs real pipeline code against it (workflows, note capture,
merge/dedup, deletion/calendar sync, temporal lineage). They are integration
tests, not unit tests: when the project is unreachable (local dev without a
live DB, CI sandbox without network), the whole suite is skipped instead of
crashing with httpx.ConnectError.

TENANT SAFETY: every test runs inside the dedicated TEST TENANT's owner
scope (tests/fixtures/test_tenant.py). Seeds go through the owner-scoped
task factory, and the suite skips when no test tenant is resolvable — it
NEVER falls back to the channel tenant (oldest active user = Danny), which
would be a cross-tenant leak.

Mirrors the identical live-DB guard in tests/sim/conftest.py — a faithful
probe that requires a fully verified TLS handshake, not a bare TCP connect.
"""

from pathlib import Path

import pytest

from core.services.db import tenant_scope
from tests.fixtures.test_tenant import fresh_supabase, resolve_test_tenant_uid

# ── Live-Supabase integration guard ───────────────────────────────────────
def _live_db_reachable() -> bool:
    """Faithful probe: can the supabase-py client actually talk to this host?

    This runs the REAL client through the REAL path the tests use — a
    read-only select against the users table — and requires a REAL
    PostgREST response shape. Two failure modes are covered:

    1. Unreachable host → httpx raises → False.
    2. Mocked client (some test suites replace get_supabase() with a
       MagicMock): a mock chain never raises, so a bare try/except would
       wrongly report "reachable". We therefore require the response's
       .data to be a real list (or None) — a MagicMock's .data is itself a
       MagicMock, which fails the isinstance check → False.
    """
    try:
        res = fresh_supabase().table("users").select("id").limit(1).execute()
        # Real PostgREST responses: .data is a list of rows, or None on
        # zero rows. Any other type (MagicMock, etc.) is not a live DB.
        return isinstance(res.data, (list, type(None)))
    except Exception:
        return False


def _suite_available() -> bool:
    """The suite runs only when BOTH a live DB and a resolvable test tenant
    exist. A reachable DB without a test tenant still skips — running inside
    the channel tenant (Danny) would leak test data across the tenant wall."""
    if not _live_db_reachable():
        return False
    return bool(TEST_TENANT_UID)


@pytest.fixture(autouse=True)
def _test_tenant_scope():
    """Run every clusters test inside the TEST TENANT's owner scope.

    The test files bind tenant_aware_client() (auto-injects owner_id) and
    task_factory (explicit owner_id), so all rows land in the test tenant.
    Without the scope the tenant-aware facade would fail closed — or worse,
    unscoped get_supabase() paths would target the channel tenant (Danny).
    The collection hook already skipped the suite when no tenant exists, so
    here we only need to enter the scope.
    """
    if not TEST_TENANT_UID:
        pytest.skip("test tenant unresolvable — refusing unscoped run")
    with tenant_scope(TEST_TENANT_UID):
        yield


# Resolve once at import: the fixture and the collection hook share it, so
# the tenants lookup happens a single time per session, not per test.
# (Module-level so both the autouse fixture and _suite_available see it.)
_TEST_TENANT_UID_CACHE: str | None = None


def _test_tenant_uid() -> str | None:
    global _TEST_TENANT_UID_CACHE
    if _TEST_TENANT_UID_CACHE is None:
        _TEST_TENANT_UID_CACHE = resolve_test_tenant_uid()
    return _TEST_TENANT_UID_CACHE


TEST_TENANT_UID = _test_tenant_uid()


def pytest_collection_modifyitems(config, items):
    """Skip the whole suite when live Supabase is unreachable OR no test
    tenant is resolvable.

    Applies the integration skip to every collected test in THIS directory
    (and its subdirectories) so the 10+ test modules don't each need a
    @requires_live_db decorator. The path guard matters: this hook fires
    session-wide, and without it a single unreachable DB would silently skip
    the unit suites in sibling directories.
    """
    if _suite_available():
        return
    here = str(Path(__file__).resolve().parent)
    skip = pytest.mark.skip(reason="live Supabase / test tenant unavailable — integration test")
    for item in items:
        # pytest 7+: item.path (pathlib.Path). Older: item.fspath. Both are
        # absolute; resolve() normalizes symlinks so the prefix match is exact.
        try:
            raw = getattr(item, "path", None) or item.fspath
            item_path = str(Path(raw).resolve())
        except Exception:
            continue
        if item_path.startswith(here):
            item.add_marker(skip)
