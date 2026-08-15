"""Hermetic tests for the health-check wrapper (ops, no aspect).

The health/validate surface is L3 covered-by-workflow (health.yml →
scripts/run_health.py --force → run_full_health_check(); validate_deployment.yml
→ scripts/validate_deployment.py) — this suite does NOT duplicate the check
itself. It pins the testable WRAPPER behavior that workflows rely on:

  - `is_business_hours()` boundary matrix (UTC 03:00–17:00 = IST 08:30–22:30;
    the health.yml schedule + Telegram alert depend on this skip gate).
  - `scripts/run_health.py` CLI: skip outside hours, all-clear → exit 0 +
    silent, issues → alert + exit 1, `--force` bypasses the hours gate.
  - `run_full_health_check()` M6 fan-out: one tenant's failure is isolated
    into an issue — never aborts the other tenants.

Frozen clock + mocks only; no DB, no network, no Telegram.
"""

import asyncio
import importlib.util
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

from core.pulse.pipeline import run_full_health_check

# Ops surface — exempt from the aspect-marker lint (see check_marker_presence.py)

ROOT = Path(__file__).resolve().parent.parent.parent
_SPEC = importlib.util.spec_from_file_location("run_health", ROOT / "scripts" / "run_health.py")
run_health = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(run_health)


# ------------------------------------------------- business-hours gate

def _utc(hour: int, minute: int = 0):
    return datetime(2026, 1, 5, hour, minute, tzinfo=timezone.utc)


def test_business_hours_boundaries():
    from freezegun import freeze_time
    # 03:00 UTC exactly → inside (3.0 <= hour)
    with freeze_time(_utc(3, 0)):
        assert run_health.is_business_hours() is True
    with freeze_time(_utc(2, 59)):
        assert run_health.is_business_hours() is False
    # 17:00 UTC exactly → inside (hour <= 17.0)
    with freeze_time(_utc(17, 0)):
        assert run_health.is_business_hours() is True
    with freeze_time(_utc(17, 1)):
        assert run_health.is_business_hours() is False
    # middle of the window
    with freeze_time(_utc(10, 30)):
        assert run_health.is_business_hours() is True


# ------------------------------------------------- CLI behavior

def _run_main(argv):
    with patch.object(sys, "argv", ["run_health.py"] + argv):
        try:
            asyncio.run(run_health.main())
            return 0
        except SystemExit as e:
            return e.code


def test_cli_skips_outside_business_hours():
    from freezegun import freeze_time
    with freeze_time(_utc(1, 0)), \
         patch("core.pulse.pipeline.run_full_health_check", new=AsyncMock()) as hc, \
         patch("core.webhook.telegram.send_telegram", new=AsyncMock()) as tg:
        code = _run_main([])
    assert code == 0
    hc.assert_not_awaited()  # skipped, never ran
    tg.assert_not_awaited()


def test_cli_all_clear_exits_zero_silently():
    from freezegun import freeze_time
    with freeze_time(_utc(10, 0)), \
         patch("core.pulse.pipeline.run_full_health_check",
               new=AsyncMock(return_value={"issues": [], "report": "ok", "counts": {}})), \
         patch("core.webhook.telegram.send_telegram", new=AsyncMock()) as tg:
        code = _run_main([])
    assert code == 0
    tg.assert_not_awaited()  # no alert fatigue — silent when healthy


def test_cli_issues_alert_and_exit_one():
    from freezegun import freeze_time
    issues = ["stuck raw_dumps: 3 pending > 2h"]
    with freeze_time(_utc(10, 0)), \
         patch.dict("os.environ", {"TELEGRAM_CHAT_ID": "1234"}, clear=False), \
         patch("core.pulse.pipeline.run_full_health_check",
               new=AsyncMock(return_value={"issues": issues, "report": "x", "counts": {}})), \
         patch("core.webhook.telegram.send_telegram", new=AsyncMock()) as tg:
        code = _run_main([])
    assert code == 1  # GHA sees the failure
    tg.assert_awaited_once()
    # args[0] is the int chat_id, args[1] is the alert text
    assert "Health Check" in tg.await_args.args[1]


def test_cli_issues_without_chat_id_exits_one_no_alert():
    from freezegun import freeze_time
    with freeze_time(_utc(10, 0)), \
         patch.dict("os.environ", {}, clear=True), \
         patch("core.pulse.pipeline.run_full_health_check",
               new=AsyncMock(return_value={"issues": ["boom"], "report": "x", "counts": {}})), \
         patch("core.webhook.telegram.send_telegram", new=AsyncMock()) as tg:
        code = _run_main([])
    assert code == 1
    tg.assert_not_awaited()


def test_cli_force_bypasses_hours_gate():
    from freezegun import freeze_time
    with freeze_time(_utc(1, 0)), \
         patch("core.pulse.pipeline.run_full_health_check",
               new=AsyncMock(return_value={"issues": [], "report": "ok", "counts": {}})) as hc:
        code = _run_main(["--force"])
    assert code == 0
    hc.assert_awaited_once()  # --force ran despite the hour


# ------------------------------------------- M6 fan-out failure isolation

def test_fanout_no_users_runs_unscoped_once():
    with patch("core.pulse.pipeline.active_user_ids", return_value=[]), \
         patch("core.pulse.pipeline._run_full_health_check_impl",
               new=AsyncMock(return_value={"issues": [], "report": "", "counts": {}})) as impl:
        result = asyncio.run(run_full_health_check())
    impl.assert_awaited_once()
    assert result["issues"] == []
    # legacy (no active users) runs the impl once, unscoped — no fan-out keys


def test_fanout_isolates_tenant_failure():
    ok_report = {"issues": [], "report": "tenant A ok", "counts": {"a": 1}}
    fail_side_effect = Exception("tenant B db down")

    async def _impl():
        # first call (tenant A) succeeds, second (tenant B) raises
        if not hasattr(_impl, "_n"):
            _impl._n = 0
        _impl._n += 1
        if _impl._n == 1:
            return ok_report
        raise fail_side_effect

    with patch("core.pulse.pipeline.active_user_ids", return_value=["uid-a", "uid-b"]), \
         patch("core.pulse.pipeline.tenant_scope"), \
         patch("core.pulse.pipeline._run_full_health_check_impl", side_effect=_impl), \
         patch("core.pulse.pipeline.audit_log_sync") as audit:
        result = asyncio.run(run_full_health_check())

    # tenant B's failure became an issue, not an abort
    assert any("tenant uid-b" in i for i in result["issues"])
    assert result["tenants"] == 2
    # the failure was logged (args[0] = subsystem)
    assert any(call.args[0] == "health_check" for call in audit.call_args_list)
