"""Extraction resilience regression tests (Sep 14 permanent fixes).

Four failure shapes from the live incident, now locked down:
  1. Mode A — planner returned `reschedule` with EMPTY params for a date-only
     request ("Move my rental agreement signing task to tomorrow") → the
     date-word backstop injects the date; preserve-time keeps the task's
     clock time. The action validates; the user is never asked for a time
     their task already has.
  2. Mode B — planner LLM safe-hold (all providers failed) → ExtractionDegraded
     propagates (never swallowed into the 0-actions note terminal), callers
     keep pending state alive and tell the truth.
  3. L3 — every failure message owns the failure ("my side…"), never implies
     the user's wording was the problem.
  4. L2 (writes) — log_exchange retries once on a transient Supabase insert
     failure so both sides of an exchange aren't eaten by a blip.
Plus: the planner has its own limiter pool (#2) so a pulse burst can't
starve it into safe-holds.

Aspect marker: webhook
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import core.actions.models as amodels
import core.lib.suggestion_extractor as se
import core.webhook.workflows as workflows
import core.lib.conversation as conversation
from core.lib.time_utils import now_for_user

pytestmark = pytest.mark.webhook


# ── Fix #1: date-word backstop (deterministic, no LLM) ──


def test_date_words_injects_reminder_preserving_clock_time():
    # Anchored to "now" so the test never expires with the calendar date.
    tomorrow = now_for_user().date() + timedelta(days=1)
    action = {"operation": "reschedule", "target_id": 5966, "params": {}}
    current = datetime(2026, 9, 14, 11, 30, tzinfo=timezone.utc)  # 5pm IST
    out = amodels.inject_deterministic_date_words(
        action, "Move my rental agreement signing task to tomorrow",
        current_reminder_at=current.isoformat())
    assert "new_reminder_at" in out["params"]
    injected = datetime.fromisoformat(out["params"]["new_reminder_at"])
    assert injected.date() == tomorrow
    assert (injected.hour, injected.minute) == (11, 30)      # task's 5pm IST preserved


def test_date_words_respects_explicit_tomorrow_tonight_today():
    today = now_for_user().date()
    expected = {"tomorrow": today + timedelta(days=1), "tonight": today, "today": today}
    current = datetime(2026, 9, 14, 11, 30, tzinfo=timezone.utc)
    for word in ("tomorrow", "tonight", "today"):
        out = amodels.inject_deterministic_date_words(
            {"operation": "reschedule", "target_id": 1, "params": {}},
            f"move it to {word}", current_reminder_at=current.isoformat())
        injected = datetime.fromisoformat(out["params"]["new_reminder_at"])
        assert injected.date() == expected[word]


def test_date_words_noop_when_planner_produced_time_or_delta():
    current = datetime(2026, 9, 14, 11, 30, tzinfo=timezone.utc)
    action = {"operation": "reschedule", "target_id": 1,
              "params": {"new_reminder_at": "2026-09-15T18:00:00+05:30"}}
    out = amodels.inject_deterministic_date_words(
        action, "move it to tomorrow", current_reminder_at=current.isoformat())
    assert out["params"]["new_reminder_at"] == "2026-09-15T18:00:00+05:30"  # untouched


def test_date_words_noop_without_day_word():
    out = amodels.inject_deterministic_date_words(
        {"operation": "reschedule", "target_id": 1, "params": {}},
        "move it to next Tuesday sometime", current_reminder_at=None)
    assert "new_reminder_at" not in out["params"]  # left to the LLM/clarification


def test_date_words_noop_for_non_reschedule():
    action = {"operation": "create_task", "target_id": None, "params": {}}
    out = amodels.inject_deterministic_date_words(action, "create a task tomorrow")
    assert "new_reminder_at" not in out["params"]


def test_date_words_without_current_time_falls_to_deadline():
    out = amodels.inject_deterministic_date_words(
        {"operation": "reschedule", "target_id": 1, "params": {}},
        "move it to tomorrow", current_reminder_at=None)
    assert out["params"].get("deadline") is not None  # date injected, time fail-closed


def test_empty_params_reschedule_now_validates_end_to_end():
    """The exact incident repro: empty-params reschedule + 'tomorrow' in text
    + the task's current reminder → the injected action passes validation."""
    action = {"operation": "reschedule", "target_id": "5966", "params": {}}
    current = datetime(2026, 9, 14, 11, 30, tzinfo=timezone.utc)
    action = amodels.inject_deterministic_date_words(
        action, "Move my rental agreement signing task to tomorrow",
        current_reminder_at=current.isoformat())
    action = amodels.inject_deterministic_preserve_time(
        action, current_reminder_at=current.isoformat())
    validated = amodels.PLAN_ACTION_ADAPTER.validate_python(action)  # must not raise
    tomorrow = (now_for_user().date() + timedelta(days=1)).isoformat()
    assert validated.params["new_reminder_at"].date().isoformat() == tomorrow


# ── Fix #2: the planner has its own limiter pool ──


def test_planner_uses_dedicated_limiter_pool():
    assert se.planner_flash_limiter is not None
    assert se.planner_flash_limiter.prefix == "planner_flash"


# ── L2: ExtractionDegraded contract ──


@pytest.mark.asyncio
async def test_safe_hold_raises_extraction_degraded_not_empty_plan():
    """The 14:07 shape: degraded LLM response → ExtractionDegraded (never a
    silent empty plan that becomes a note)."""
    degraded_resp = MagicMock(success=False, degraded=True,
                              degraded_reason="all_providers_failed")
    with patch.object(se, "tenant_aware_client") as mt, \
         patch("core.services.google_service.get_upcoming_calendar_events", return_value=[]), \
         patch.object(se, "generate_content_with_fallback", new=AsyncMock(return_value=degraded_resp)), \
         patch("core.lib.learning_hints.get_action_planner_hint", new=AsyncMock(return_value="")), \
         patch("core.services.user_settings.resolve_timezone", side_effect=Exception("no db")):
        # tasks query → no open tasks
        tbl = MagicMock()
        chain = tbl.table.return_value.select.return_value
        chain.eq.return_value.not_.in_.return_value.execute.return_value.data = []
        chain.eq.return_value.neq.return_value.neq.return_value.execute.return_value.data = []
        mt.return_value = tbl
        with pytest.raises(se.ExtractionDegraded):
            await se.extract_suggestions("move my task to tomorrow", intent="TASK")


@pytest.mark.asyncio
async def test_extractor_does_not_swallow_extraction_degraded():
    """The extractor's generic exception handler must re-raise ExtractionDegraded."""
    calls = []

    async def boom(*a, **k):
        calls.append(1)
        raise se.ExtractionDegraded("planner LLM degraded: test")

    with patch.object(se, "tenant_aware_client") as mt, \
         patch("core.services.google_service.get_upcoming_calendar_events", return_value=[]), \
         patch.object(se, "generate_content_with_fallback", new=AsyncMock(side_effect=boom)), \
         patch("core.lib.learning_hints.get_action_planner_hint", new=AsyncMock(return_value="")), \
         patch("core.services.user_settings.resolve_timezone", side_effect=Exception("no db")):
        tbl = MagicMock()
        chain = tbl.table.return_value.select.return_value
        chain.eq.return_value.not_.in_.return_value.execute.return_value.data = []
        chain.eq.return_value.neq.return_value.neq.return_value.execute.return_value.data = []
        mt.return_value = tbl
        with pytest.raises(se.ExtractionDegraded):
            await se.extract_suggestions("move my task to tomorrow", intent="TASK")
    assert calls  # the failing call was actually attempted


@pytest.mark.asyncio
async def test_workflow_resume_on_degradation_keeps_workflow_active():
    """The 13:52 shape: reply to a parked clarification during an LLM outage →
    honest hiccup message, workflow stays ACTIVE (never cancelled), no notes."""
    wf = {"id": "wf-1", "payload": {"original_text": "move my task to tomorrow",
                                    "intent": "TASK", "title": "t", "entity": "e"}}
    with patch.object(workflows, "tenant_aware_client") as mt, \
         patch.object(workflows, "send_telegram", new=AsyncMock()) as st, \
         patch.object(workflows, "log_exchange"), \
         patch.object(workflows, "_emit_clarification_observation", new=AsyncMock()), \
         patch("core.lib.suggestion_extractor.extract_suggestions",
               new=AsyncMock(side_effect=se.ExtractionDegraded("x"))):
        upd = MagicMock()
        mt.return_value.table.return_value.update.return_value.eq.return_value.eq.return_value.execute = upd
        consumed, reply = await workflows._resume_action_clarification(1, "tomorrow 5pm", "t1", wf)
        assert consumed is True
        text = st.await_args.args[1]
        assert "still pending" in text          # L3: honest, state-preserving
        assert "from what you said" not in text  # never blames the wording
        upd.assert_not_called()                  # workflow NOT cancelled


@pytest.mark.asyncio
async def test_workflow_resume_zero_actions_keeps_workflow_active():
    """0 actions after a successful plan → keep alive + honest message (never
    the old 'I couldn't work that out from what you said' + cancel)."""
    wf = {"id": "wf-1", "payload": {"original_text": "move my task to tomorrow",
                                    "intent": "TASK", "title": "t", "entity": "e"}}
    with patch.object(workflows, "tenant_aware_client") as mt, \
         patch.object(workflows, "send_telegram", new=AsyncMock()) as st, \
         patch.object(workflows, "log_exchange"), \
         patch.object(workflows, "_emit_clarification_observation", new=AsyncMock()), \
         patch("core.lib.suggestion_extractor.extract_suggestions",
               new=AsyncMock(return_value=([], {}))):
        upd = MagicMock()
        mt.return_value.table.return_value.update.return_value.eq.return_value.eq.return_value.execute = upd
        consumed, reply = await workflows._resume_action_clarification(1, "tomorrow 5pm", "t1", wf)
        assert consumed is True
        assert "still pending" in st.await_args.args[1]
        upd.assert_not_called()


# ── L2 (writes): log_exchange bounded retry ──


def test_log_exchange_retries_once_on_transient_failure():
    calls = []
    real_insert = conversation._log_exchange_insert

    def flaky(*a, **k):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("JSON could not be generated")  # the Supabase blip
        return real_insert(*a, **k)

    with patch.object(conversation, "_log_exchange_insert", side_effect=flaky), \
         patch.object(conversation, "tenant_aware_client"), \
         patch.object(conversation, "_touch_thread"), \
         patch("core.lib.audit_logger.audit_log_sync"):
        conversation.log_exchange("s1", "user", "TEST", "hello", 1)
    assert len(calls) == 2  # first attempt failed, retry succeeded


def test_log_exchange_fails_open_after_retry():
    with patch.object(conversation, "_log_exchange_insert",
                      side_effect=RuntimeError("down")), \
         patch("core.lib.audit_logger.audit_log_sync") as la:
        conversation.log_exchange("s1", "user", "TEST", "hello", 1)  # must not raise
    assert la.called


# ── L4: per-provider failure cause logging ──


def test_fallback_logs_provider_failure_causes():
    import core.llm.fallback as fb

    async def fail(*a, **k):
        raise RuntimeError("API key invalid")

    captured = []

    def fake_audit(service, level, message, metadata=None):
        if service == "llm":
            captured.append((level, message))

    with patch.object(fb, "call_gemini", side_effect=fail), \
         patch.object(fb, "call_openrouter", side_effect=fail), \
         patch.object(fb, "audit_log_sync", side_effect=fake_audit), \
         patch.object(fb.gemini_breaker, "is_open", return_value=False), \
         patch.object(fb.gemini_breaker, "record_failure"), \
         patch.object(fb.gemini_breaker, "record_success"), \
         patch.object(fb, "log_llm_outcome"), \
         patch.object(fb.WorkloadProfile.INTERACTIVE.__class__, "max_retries",
                      new_callable=lambda: property(lambda self: 1), create=True):
        import asyncio

        async def run():
            return await fb.generate_content_with_fallback(prompt="p", workload=fb.WorkloadProfile.INTERACTIVE)

        resp = asyncio.run(run())
    assert resp.degraded and resp.degraded_reason == "all_providers_failed"
    joined = " | ".join(m for _, m in captured)
    assert "gemini/" in joined and "API key invalid" in joined  # cause surfaced
