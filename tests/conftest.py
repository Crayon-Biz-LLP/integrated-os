import os
from dotenv import load_dotenv

# If LIVE_DB is set, force load real credentials from .env
# This overwrites the dummy values set by pytest.ini (via pytest-env)
if os.getenv("LIVE_DB") == "true":
    load_dotenv(override=True)

# Re-export fixtures so all cluster tests can use them without importing directly.
# This keeps cluster files clean and avoids ruff F401/F811 false positives on
# pytest fixture imports.
from tests.fixtures.google_api_mocks import mock_google_apis  # noqa: F401, E402


# ── Cross-tenant leak guard ──────────────────────────────────────────────────
# After the whole session, verify NO test-marker rows exist outside the test
# tenant. This is the hard guarantee behind "no test records leak into other
# tenants": even a buggy test that slipped an owner_id could leave a marker row
# behind — this sweep finds it and fails the session instead of silently
# polluting another tenant.
import pytest  # noqa: E402

from tests.fixtures.test_tenant import fresh_supabase, resolve_test_tenant_uid  # noqa: E402
from tests.fixtures.run_isolation import (  # noqa: E402
    RUN_CHAT_BASE,
    RUN_CHAT_SPAN,
    SandboxLockHeldError,
    acquire_sandbox_lock,
    pre_delete_test_rows,
    release_sandbox_lock,
)


# ── Clock determinism (plans/75 §8.2) ───────────────────────────────────────
# One frozen-clock fixture for the whole suite. Anchored to a FIXED instant in
# Asia/Kolkata (the repo's canonical timezone — see AGENTS.md timezone
# hygiene) so pulse windows, sentinel nudge timing, and briefing-mode branches
# are deterministic instead of wall-clock-flaky. Use it in any test that
# depends on datetime.now()/today():
#
#     def test_x(self, frozen_clock):
#         ...
#
# The yielded value is the frozen aware datetime. freezegun must be installed
# (requirements.txt) — the fixture raises a clear error otherwise.
@pytest.fixture
def frozen_clock():
    try:
        from freezegun import freeze_time
    except ImportError as e:  # pragma: no cover
        raise RuntimeError(
            "frozen_clock fixture requires freezegun — add it to requirements.txt"
        ) from e
    from datetime import datetime
    from zoneinfo import ZoneInfo

    # Monday 2026-01-05 09:30 IST — a normal business-week morning, inside the
    # briefing window. Tests that need a different instant pass their own
    # freezegun.freeze_time(...) with an Asia/Kolkata-anchored datetime.
    fixed = datetime(2026, 1, 5, 9, 30, tzinfo=ZoneInfo("Asia/Kolkata"))
    with freeze_time(fixed):
        yield fixed

# (table, column) pairs whose [TEST]/[SIM_TEST]/[UAT]-prefixed rows must
# live in the test tenant only. Any row matching the marker pattern under a
# DIFFERENT owner_id (or no owner) is a leak. [UAT] added per the
# leak-guard table growth rule (plans/75 §5): the L4 UAT suite writes
# [UAT]-prefixed rows and its cleanup is owner-scoped — anything left under
# another owner is exactly the 08-13 leak shape the guard must catch.
_LEAK_MARKER_TABLES = [
    ("tasks", "title"),
    ("memories", "content"),
    ("graph_nodes", "label"),
    ("raw_dumps", "content"),
    ("resources", "url"),
    ("audit_logs", "message"),
    ("projects", "name"),
    ("organizations", "name"),
    # decisions: the per-item undo + learning-loop work writes decision rows
    # (titles, learn_features metadata) — a [TEST]/[SIM_TEST]/[UAT]-titled
    # decision owned outside the test tenant is a leak signal.
    ("decisions", "title"),
]

# Thread/workflow rows carry no [TEST] text — the sim suite seeds a fixed
# UUID prefix, and workflow tests use a precise set of chat_ids. A broad
# "chat_id >= 9000000" range is WRONG: real Telegram chat ids (e.g. Danny's
# 756478183) exceed 9M, so the range check flags legitimate production rows
# as leaks. The chat ids the suites write are:
#   - legacy fixed ids (guard still knows pre-X4 rows on the sandbox):
#     sim seed 999999999, note_capture 9000000+offset, suite2 9000001,
#     UAT 909999999
#   - the per-run band (X4): range(RUN_CHAT_BASE, RUN_CHAT_BASE + 32) —
#     sim seed +0, suite2 +1, UAT +2, note_capture +0..+19
_TEST_THREAD_ID_MARKER = "00000000-0000-4000-8000"
_LEGACY_TEST_CHAT_IDS = frozenset({999999999, 9000000, 9000001, 9000002, 9000003, 9000005, 9000006, 9000007, 9000008, 9000009, 9000010, 9000019, 909999999})
_TEST_CHAT_IDS = _LEGACY_TEST_CHAT_IDS | frozenset(range(RUN_CHAT_BASE, RUN_CHAT_BASE + RUN_CHAT_SPAN))


def _leaked_test_rows() -> list[str]:
    """Return descriptions of test-marker rows owned by a non-test tenant."""
    uid = resolve_test_tenant_uid()
    if not uid:
        return []  # no test tenant → nothing ran → nothing to leak
    supabase = fresh_supabase()
    leaked = []
    for table, col in _LEAK_MARKER_TABLES:
        for marker in ("[TEST]%", "[SIM_TEST]%", "[UAT]%"):
            try:
                res = (
                    supabase.table(table)
                    .select("id, owner_id")
                    .ilike(col, marker)
                    .or_(f"owner_id.neq.{uid},owner_id.is.null")
                    .limit(5)
                    .execute()
                )
                for row in (res.data or []):
                    leaked.append(f"{table}.{col} id={row.get('id')} owner={row.get('owner_id')}")
            except Exception:
                continue  # table missing / column mismatch → not a leak signal
    # Fixed sim thread id prefix (any owner other than the test tenant = leak)
    try:
        res = (
            supabase.table("conversation_threads")
            .select("id, owner_id")
            .ilike("id", f"{_TEST_THREAD_ID_MARKER}%")
            .or_(f"owner_id.neq.{uid},owner_id.is.null")
            .limit(5)
            .execute()
        )
        for row in (res.data or []):
            leaked.append(f"conversation_threads.id id={row.get('id')} owner={row.get('owner_id')}")
    except Exception:
        pass
    # Workflow rows created by the test suites use exact test chat_ids
    # (see _TEST_CHAT_IDS above) — a row with one of those chat_ids owned by
    # anyone other than the test tenant is a leak.
    chat_ids = sorted(_TEST_CHAT_IDS)
    try:
        res = (
            supabase.table("conversation_workflows")
            .select("id, chat_id, owner_id")
            .in_("chat_id", chat_ids)
            .or_(f"owner_id.neq.{uid},owner_id.is.null")
            .limit(5)
            .execute()
        )
        for row in (res.data or []):
            leaked.append(f"conversation_workflows chat_id={row.get('chat_id')} owner={row.get('owner_id')}")
    except Exception:
        pass
    return leaked


@pytest.fixture(scope="session", autouse=True)
def _verify_no_cross_tenant_leaks():
    """Fail the session if any test-marker row leaked into another tenant."""
    yield
    if os.getenv("LIVE_DB") != "true":
        return  # no live DB touched → nothing to verify
    leaked = _leaked_test_rows()
    assert not leaked, (
        "CROSS-TENANT LEAK DETECTED — [TEST]/[SIM_TEST] rows outside the test "
        f"tenant: {leaked}"
    )


@pytest.fixture(scope="session", autouse=True)
def _clean_slate_before_live_session():
    """Sandbox serialization (X4 residual) + clean-slate pre-delete (X5).

    1. Acquire the cross-machine Redis lock — a second live run (CI cron +
       local, or two locals) fails fast instead of racing the shared sandbox.
       Marker-title sweeps cross runs regardless of chat allocation, so this
       lock is what makes the residual structurally impossible. TTL
       self-expires on a killed run; Redis-unconfigured environments skip it.
    2. Purge test-tenant marker rows BEFORE the suite — a run killed mid-way
       leaves residue (fixed thread UUIDs, [SIM_TEST] titles, workflow rows)
       that poisons the next run. Rows owned by ANY other tenant are
       deliberately untouched — the leak guard is the enforcement point.
    """
    if os.getenv("LIVE_DB") != "true":
        yield
        return
    uid = resolve_test_tenant_uid()
    if not uid:
        yield
        return  # no test tenant → suites skip → nothing to purge
    try:
        lock = acquire_sandbox_lock()
    except SandboxLockHeldError as e:
        pytest.fail(str(e), pytrace=False)
    pre_delete_test_rows(uid)
    try:
        yield
    finally:
        release_sandbox_lock(lock)
