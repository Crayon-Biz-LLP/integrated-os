"""Executor ack voice regression tests (Sep 15 incident follow-up).

The Sep 15 reschedule ack read "Moved Reschedule rental agreement signing
task to today to Sep 15, 2026." — three defects, each locked down here:

  1. The ack used the planner's echo of the user's phrasing (human_label)
     instead of the task's canonical title from the row the executor had
     just fetched. Locked: executor wiring passes `td['title']`.
  2. The ack showed date only, in no timezone ("Sep 15, 2026") — after an
     incident that was entirely about time preservation, the ack must show
     the moved clock time in the user's timezone (11:30 UTC = 5:00 PM IST,
     never a bare UTC clock).
  3. A date-only `deadline` went stale when the reminder moved past it.
     Locked: stale deadline rolls forward; a future deadline (a real
     commitment) and a time-bearing deadline are never touched.

Aspect marker: webhook
"""

from unittest.mock import MagicMock, patch

import pytest

import core.actions.models as amodels
import core.actions.executor as executor
import core.lib.rhodey_voice as voice
import core.lib.time_utils as time_utils

pytestmark = pytest.mark.webhook


@pytest.fixture(autouse=True)
def _ist_zone(monkeypatch):
    """Pin the tenant zone to IST without touching the DB.

    resolve_timezone would hit Supabase in unit tests; force the env branch
    of get_user_timezone (settings → env → IST) and clear its name cache.
    """
    monkeypatch.setenv("USER_TIMEZONE", "Asia/Kolkata")
    import core.services.user_settings as us
    def _no_db(*a, **k):
        raise RuntimeError("no DB in unit tests")
    monkeypatch.setattr(us, "resolve_timezone", _no_db)
    time_utils._tz_cache.clear()
    yield
    time_utils._tz_cache.clear()


# ── human_datetime: timezone-honest clock rendering ──────────────────────


def test_human_datetime_converts_utc_to_user_zone():
    # The incident's exact value: 11:30 UTC is 5:00 PM IST, not "11:30 AM".
    out = voice.human_datetime("2026-09-15T11:30:00+00:00")
    assert out == "Sep 15 at 5:00 PM"


def test_human_datetime_strips_leading_zero():
    out = voice.human_datetime("2026-09-15T12:35:00+00:00")  # 6:05 PM IST
    assert out == "Sep 15 at 6:05 PM"  # not "06:05 PM"


def test_human_datetime_naive_timestamp_assumed_utc():
    # DB contract: naive timestamps are UTC, never masqueraded as local.
    out = voice.human_datetime("2026-09-15T11:30:00")
    assert out == "Sep 15 at 5:00 PM"


def test_human_datetime_garbage_falls_back_to_date_rendering():
    assert voice.human_datetime("not-a-date") == "not-a-date"


# ── render_acks: canonical title + time shown ─────────────────────────────


def test_reschedule_ack_uses_canonical_title_and_shows_time():
    r = voice.ExecutionResult(
        "reschedule", target_id=5966,
        title="sign the rental agreement with the new landlord",
        values={"new_reminder_at": "2026-09-15T11:30:00+00:00"})
    lines = voice.render_acks([r])
    assert lines == [
        "Moved sign the rental agreement with the new landlord to Sep 15 at 5:00 PM."
    ]


def test_reschedule_ack_without_time_keeps_bare_verb():
    r = voice.ExecutionResult("reschedule", target_id=1, title="Pay rent", values={})
    assert voice.render_acks([r]) == ["Moved Pay rent."]


def test_reschedule_ack_falls_back_to_human_label_when_row_title_missing():
    r = voice.ExecutionResult(
        "reschedule", target_id=1, title=None,
        values={"new_reminder_at": "2026-09-15T11:30:00+00:00"})
    lines = voice.render_acks([r])
    assert lines == ["Moved item 1 to Sep 15 at 5:00 PM."]


# ── deadline_rollover: code owns fact alignment ───────────────────────────


def test_stale_deadline_rolls_to_new_reminder_date():
    assert amodels.deadline_rollover(
        "2026-09-14", "2026-09-15T11:30:00+00:00") == "2026-09-15"


def test_future_deadline_is_a_commitment_and_stays():
    assert amodels.deadline_rollover(
        "2026-09-30", "2026-09-15T11:30:00+00:00") is None


def test_same_day_deadline_not_touched():
    assert amodels.deadline_rollover(
        "2026-09-15", "2026-09-15T11:30:00+00:00") is None


def test_rollover_anchors_to_tenant_calendar_day_not_utc():
    # 19:30 UTC on Sep 15 is already Sep 16 in IST — the deadline must roll
    # to the day the user actually sees, not the UTC day.
    assert amodels.deadline_rollover(
        "2026-09-15", "2026-09-15T19:30:00+00:00") == "2026-09-16"


def test_time_bearing_deadline_never_touched():
    assert amodels.deadline_rollover(
        "2026-09-14T09:00:00+00:00", "2026-09-16T11:30:00+00:00") is None


def test_rollover_garbage_inputs_return_none_not_crash():
    assert amodels.deadline_rollover(None, "2026-09-15T11:30:00+00:00") is None
    assert amodels.deadline_rollover("2026-09-14", None) is None
    assert amodels.deadline_rollover("garbage", "2026-09-15T11:30:00+00:00") is None


# ── executor wiring: canonical title from the fetched row ────────────────


def _mock_supabase(task_row):
    sb = MagicMock()
    (sb.table.return_value.select.return_value.eq.return_value
       .limit.return_value.execute.return_value) = MagicMock(data=[task_row])
    (sb.table.return_value.update.return_value.eq.return_value.execute
       .return_value) = MagicMock(data=[])
    return sb


def _local_client_factory(task_row):
    """The executor binds tenant_aware_client() to a *local* `supabase`
    inside execute_planned_actions — so the mock rides on the factory."""
    client = _mock_supabase(task_row)
    factory = MagicMock(return_value=client)
    factory.mocked_client = client
    return factory


@pytest.mark.asyncio
async def test_executor_reschedule_ack_carries_canonical_title():
    """The exact Sep 15 shape: planner echoed the user's phrasing; the ack
    must carry the task's own title and the new time."""
    row = {"id": 5966, "title": "sign the rental agreement with the new landlord",
           "reminder_at": "2026-09-14T11:30:00+00:00", "deadline": "2026-09-14",
           "google_event_id": "evt1", "google_task_id": "gt1", "duration_mins": 60}
    action = executor.Action(operation="reschedule", target_id="5966",
                             human_label="Reschedule rental agreement signing task to today",
                             params={"new_reminder_at": "2026-09-15T11:30:00+00:00"})
    with patch("core.actions.executor.tenant_aware_client", _local_client_factory(row)), \
         patch("core.services.google_service.format_rfc3339",
               return_value="2026-09-15T11:30:00+05:30"), \
         patch("core.services.google_service.sync_to_calendar",
               return_value="evt1"), \
         patch("core.services.google_service.sync_to_google"), \
         patch("core.services.google_service.get_tasks_service", return_value=MagicMock()):
        results = await executor.execute_planned_actions(
            [action], chat_id=1, text="move it", suppress_telegram=True)
    assert len(results) == 1 and results[0].status == "committed"
    assert results[0].title == "sign the rental agreement with the new landlord"
    assert results[0].values["new_reminder_at"] == "2026-09-15T11:30:00+00:00"


@pytest.mark.asyncio
async def test_executor_reschedule_rolls_stale_deadline_in_patch():
    row = {"id": 1, "title": "T", "reminder_at": "2026-09-14T11:30:00+00:00",
           "deadline": "2026-09-14", "google_event_id": None,
           "google_task_id": None, "duration_mins": 15}
    action = executor.Action(operation="reschedule", target_id="1",
                             params={"new_reminder_at": "2026-09-15T11:30:00+00:00"})
    factory = _local_client_factory(row)
    with patch("core.actions.executor.tenant_aware_client", factory), \
         patch("core.services.google_service.format_rfc3339",
               return_value="2026-09-15T11:30:00+05:30"), \
         patch("core.services.google_service.sync_to_calendar", return_value="evt2"), \
         patch("core.services.google_service.sync_to_google"), \
         patch("core.services.google_service.get_tasks_service", return_value=MagicMock()):
        await executor.execute_planned_actions(
            [action], chat_id=1, text="move it", suppress_telegram=True)
    update_payloads = [c.args[0] for c in factory.mocked_client.table.return_value.update.call_args_list]
    patch_kwargs = next(u for u in update_payloads if "reminder_at" in u)
    assert patch_kwargs["deadline"] == "2026-09-15"  # rolled with the reminder
