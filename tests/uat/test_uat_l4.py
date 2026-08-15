"""L4 — UAT scenarios as pytest (plans/75 §13: wrap, don't rewrite).

Thin adapter over tests/uat/run_uat.py: the 22 scenario bodies run UNCHANGED
(the existing async functions and their UatResult container), wrapped in a
pytest session with:

  - TEST-tenant scope: every insert/read goes through tenant_scope(test_uid)
    — never the channel tenant (the leak M3 prevents).
  - Outbound-mock patchers (same list the harness's run_all uses): nothing
    leaves the process.
  - Owner-scoped cleanup after the session ([UAT] rows, eq('owner_id', uid)).
  - Per-scenario primary aspect marks (exclusive-primary per test), so
    `-m decision` selects S1/S5/S6/S8/S15/S19/… and the marker lint passes.

Run (nightly only — live DB, real LLM, pacing sleeps):
    pytest tests/uat/test_uat_l4.py -q        (needs LIVE_DB=true + TEST_CHAT_IDS)
"""

import os
from unittest.mock import AsyncMock, patch

import pytest

from core.services.db import tenant_scope
from tests.fixtures.test_tenant import resolve_test_tenant_uid
from tests.uat import run_uat as uat

pytestmark = pytest.mark.skipif(
    os.getenv("LIVE_DB") != "true",
    reason="L4 UAT needs LIVE_DB=true (real Supabase, TEST tenant)",
)

# Primary aspect per scenario id — exclusive-primary per TEST (plans/75 §3).
_SCENARIO_ASPECT = {
    "S1": "decision", "S2": "ingest", "S3": "ingest", "S4": "ingest",
    "S5": "decision", "S6": "decision", "S7": "ingest", "S8": "decision",
    "S9": "ingest", "S10": "graph", "S11": "retrieval", "S12": "retrieval",
    "S13": "ingest", "S14": "pulse", "S15": "decision", "S16": "sentinel",
    "S17": "pulse", "S18": "graph", "S19": "decision", "S19b": "decision",
    "S20": "decision", "S21": "calendar", "S22": "briefing",
}

_SCENARIOS = [
    pytest.param(
        sc_id, sc_name, sc_func,
        marks=getattr(pytest.mark, _SCENARIO_ASPECT.get(sc_id, "ingest")),
        id=sc_id,
    )
    for sc_id, sc_name, sc_func in uat.ALL_SCENARIOS
]


@pytest.fixture(scope="session")
def _uat_tenant():
    """Resolve the TEST tenant; skip (never leak) when unresolvable."""
    uid = resolve_test_tenant_uid()
    if not uid:
        pytest.skip("no TEST tenant resolvable — L4 refuses to run unscoped")
    return uid


@pytest.fixture(scope="session", autouse=True)
def _uat_scope(_uat_tenant):
    """Run the whole L4 session inside tenant_scope(test_uid); owner-scoped
    cleanup after — [UAT] rows only, eq('owner_id', uid). Also admits this
    run's per-run chat id (X4) through the webhook gate: TEST_CHAT_IDS env
    gets CHAT_ID appended (nightly.yml already pins the legacy 909999999)."""
    prev_chats = os.environ.get("TEST_CHAT_IDS")
    os.environ["TEST_CHAT_IDS"] = ",".join(
        filter(None, [prev_chats or "", str(uat.CHAT_ID)])
    )
    try:
        with tenant_scope(_uat_tenant):
            yield
            uat.cleanup_uat_rows(uid=_uat_tenant)
    finally:
        if prev_chats is None:
            os.environ.pop("TEST_CHAT_IDS", None)
        else:
            os.environ["TEST_CHAT_IDS"] = prev_chats


@pytest.fixture(scope="session", autouse=True)
def _uat_patches():
    """Same outbound-mock patchers the harness's run_all applies — nothing
    leaves the process (Telegram sends, push notifications, callback acks)."""
    patchers = [
        patch("core.webhook.telegram.send_telegram", new=uat._mock_send_telegram),
        patch("core.webhook.handler.send_telegram", new=uat._mock_send_telegram),
        patch("core.webhook.dispatch.send_telegram", new=uat._mock_send_telegram),
        patch("core.webhook.telegram.answer_callback_query", new=AsyncMock()),
        patch("core.actions.executor.send_telegram", new=uat._mock_send_telegram),
        patch("core.pulse.sentinel.send_telegram", new=uat._mock_send_telegram),
        patch("core.pulse.briefing.send_telegram", new=uat._mock_send_telegram),
        patch("core.pulse.briefing.send_push_notification", new=AsyncMock()),
        patch("core.pulse.decision_pulse.send_telegram", new=uat._mock_send_telegram),
        patch("core.pulse.decision_pulse.send_push_notification", new=AsyncMock()),
    ]
    for p in patchers:
        p.start()
    yield
    for p in patchers:
        p.stop()


@pytest.fixture(scope="session")
def uat_seed(_uat_tenant):
    """Seed [UAT] orgs + projects once (harness does the same per run)."""
    return uat.seed_uat_orgs(uid=_uat_tenant)


@pytest.mark.asyncio
@pytest.mark.parametrize("sc_id, sc_name, sc_func", _SCENARIOS)
async def test_scenario(sc_id, sc_name, sc_func, uat_seed):
    """Run one UAT scenario body unchanged; fail with its error list."""
    result = await sc_func(uat_seed)
    assert result.passed, f"[{sc_id}] {sc_name}: " + "; ".join(result.errors)
