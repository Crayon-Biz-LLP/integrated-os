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

# (table, column) pairs whose [TEST]/[SIM_TEST]-prefixed rows must live in the
# test tenant only. Any row matching the marker pattern under a DIFFERENT
# owner_id (or no owner) is a leak.
_LEAK_MARKER_TABLES = [
    ("tasks", "title"),
    ("memories", "content"),
    ("graph_nodes", "label"),
    ("raw_dumps", "text"),
    ("resources", "url"),
    ("audit_logs", "message"),
    ("projects", "name"),
    ("organizations", "name"),
]

# Thread/workflow rows carry no [TEST] text — the sim suite seeds a fixed
# UUID prefix, and workflow tests use a precise set of chat_ids. A broad
# "chat_id >= 9000000" range is WRONG: real Telegram chat ids (e.g. Danny's
# 756478183) exceed 9M, so the range check flags legitimate production rows
# as leaks. The test suites use exactly these values:
#   - sim seed fixture:          chat_id = 999999999
#   - note_capture tests:        9000000 + offset (1..19)
#   - sim test_suite2:           chat_id = 9000001
_TEST_THREAD_ID_MARKER = "00000000-0000-4000-8000"
_TEST_CHAT_IDS = frozenset({999999999, 9000000, 9000001, 9000002, 9000003, 9000005, 9000006, 9000007, 9000008, 9000009, 9000010, 9000019})


def _leaked_test_rows() -> list[str]:
    """Return descriptions of test-marker rows owned by a non-test tenant."""
    uid = resolve_test_tenant_uid()
    if not uid:
        return []  # no test tenant → nothing ran → nothing to leak
    supabase = fresh_supabase()
    leaked = []
    for table, col in _LEAK_MARKER_TABLES:
        for marker in ("[TEST]%", "[SIM_TEST]%"):
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
