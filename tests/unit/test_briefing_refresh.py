"""
Unit tests for core.services.briefing_refresh (Aug 11 fix).

The module is the single source of truth for the live-briefing refresh that
runs after every meaningful user action (task done/added/cancelled, snooze,
decision approve/reject). Tests:

T1 — full rebuild path: invalidates cache first, rebuilds via build_briefing,
     repopulates the cache, and pushes a silent briefing_refresh with the
     headline + insights derived from the payload.
T2 — debounce: a second trigger within the 120s TTL skips the LLM rebuild and
     only sends a bare nudge push (no cost blow-up on rapid actions).
T3 — retry: a first-attempt failure retries once; a second failure is audited
     as ERROR and returns success=False (was: silent print-swallow).
T4 — fire_briefing_refresh in a sync context (no running event loop) logs and
     skips instead of raising.
"""



import asyncio
from unittest.mock import ANY, AsyncMock, MagicMock, patch

import pytest

import core.services.briefing_refresh as mod
pytestmark = pytest.mark.pulse



@pytest.fixture(autouse=True)
def _reset_module_state():
    """Debounce state is module-global — reset between tests."""
    mod._last_rebuild.clear()
    mod._briefing_refresh_tasks.clear()
    yield
    mod._last_rebuild.clear()
    mod._briefing_refresh_tasks.clear()


def _run(coro):
    return asyncio.run(coro)


def _briefing_payload():
    return {
        "voice_line": "Digital Signature Certificate is next.",
        "home_mode": "proceed",
        "greeting": "Hey.",
        "insights": ["⏳ 1 task stale >7d"],
    }


def test_full_rebuild_path_invalidates_cache_builds_and_pushes():
    """T1: rebuild → cache invalidated first → cache repopulated → push sent."""
    with (
        patch.object(mod, "tenant_aware_client", return_value=MagicMock()),
        patch("core.lib.redis_cache.cache_delete") as cache_delete,
        patch("core.lib.redis_cache.cache_set") as cache_set,
        patch("core.services.push_notification.send_silent_push", new=AsyncMock()) as push,
        patch("api.briefing.build_briefing", new=AsyncMock(return_value=_briefing_payload())) as build,
        patch.object(mod, "audit_log_sync") as audit,
    ):
        result = _run(mod.trigger_briefing_refresh(source="task_status_change"))

    assert result["success"] is True
    build.assert_awaited_once()
    # Cache invalidated (the stale pre-action briefing must never be served).
    cache_delete.assert_called_once()
    assert "rhodey:briefing:home_feed:v1" in cache_delete.call_args.args[0]
    # Cache repopulated with the rebuilt payload (fast next fetch).
    cache_set.assert_called_once()
    assert cache_set.call_args.kwargs["ttl"] == 120
    # Silent push carries the type + derived headline + insights.
    push.assert_awaited_once()
    payload = push.await_args.args[0]
    assert payload["type"] == "briefing_refresh"
    assert payload["headline"] == "Digital Signature Certificate is next."
    assert "stale >7d" in payload["insights_json"]
    # Audited as INFO.
    audit.assert_any_call("briefing_refresh", "INFO",
                          "[task_status_change] briefing rebuilt + refresh push sent")


def test_debounce_skips_rebuild_within_ttl():
    """T2: a second trigger inside the 120s debounce window only nudges."""
    with (
        patch.object(mod, "tenant_aware_client", return_value=MagicMock()),
        patch("core.lib.redis_cache.cache_delete"),
        patch("core.lib.redis_cache.cache_set"),
        patch("core.services.push_notification.send_silent_push", new=AsyncMock()) as push,
        patch("api.briefing.build_briefing", new=AsyncMock(return_value=_briefing_payload())) as build,
        patch.object(mod, "audit_log_sync"),
    ):
        first = _run(mod.trigger_briefing_refresh(source="task_status_change"))
        second = _run(mod.trigger_briefing_refresh(source="focal_snooze"))

    assert first["success"] is True
    assert second["success"] is True
    assert second.get("debounced") is True
    # The LLM rebuild happened exactly once for the two rapid actions.
    build.assert_awaited_once()
    # First push carries the full payload; the debounced nudge is the bare
    # refresh signal the app must tolerate (same shape the pulse sends).
    assert push.await_count == 2
    full_payload = push.await_args_list[0].args[0]
    assert "headline" in full_payload and full_payload["headline"]
    nudge_payload = push.await_args_list[1].args[0]
    assert nudge_payload == {"type": "briefing_refresh"}


def test_retry_recovers_when_second_attempt_succeeds():
    """T3a: a transient first-attempt failure retries and succeeds."""
    with (
        patch.object(mod, "tenant_aware_client", return_value=MagicMock()),
        patch("core.lib.redis_cache.cache_delete"),
        patch("core.lib.redis_cache.cache_set"),
        patch("core.services.push_notification.send_silent_push", new=AsyncMock()),
        patch(
            "api.briefing.build_briefing",
            new=AsyncMock(side_effect=[Exception("boom"), _briefing_payload()]),
        ) as build,
        patch.object(mod, "audit_log_sync") as audit,
    ):
        result = _run(mod.trigger_briefing_refresh(source="task_status_change"))

    assert result["success"] is True
    assert build.await_count == 2
    audit.assert_any_call("briefing_refresh", "WARNING", ANY)


def test_retry_gives_up_and_audits_error_after_two_failures():
    """T3b: persistent failure is audited as ERROR and never raises."""
    with (
        patch.object(mod, "tenant_aware_client", return_value=MagicMock()),
        patch("core.lib.redis_cache.cache_delete"),
        patch("core.lib.redis_cache.cache_set"),
        patch("core.services.push_notification.send_silent_push", new=AsyncMock()),
        patch(
            "api.briefing.build_briefing",
            new=AsyncMock(side_effect=[Exception("boom"), Exception("boom again")]),
        ) as build,
        patch.object(mod, "audit_log_sync") as audit,
    ):
        result = _run(mod.trigger_briefing_refresh(source="task_status_change"))

    assert result["success"] is False
    assert "boom again" in result["error"]
    assert build.await_count == 2
    audit.assert_any_call("briefing_refresh", "ERROR", ANY)


def test_fire_briefing_refresh_safe_without_running_loop():
    """T4: fire_briefing_refresh from a sync context logs and skips — no raise."""
    with (
        patch("core.lib.redis_cache.cache_delete"),
        patch.object(mod, "audit_log_sync") as audit,
    ):
        # No running event loop here — create_task raises RuntimeError, which
        # the helper must swallow.
        mod.fire_briefing_refresh(source="task_status_change")

    audit.assert_any_call("briefing_refresh", "WARNING", ANY)
