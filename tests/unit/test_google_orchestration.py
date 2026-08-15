"""Hermetic orchestration tests for Google Calendar sync internals.

Covers `core/services/google_service.py`:
  - sync_to_calendar: event-body construction (priority prefix, description,
    reminders, end = start + duration, timezone), insert-vs-patch routing,
    recurrence pass-through, the 404 heal-and-reprovision path, non-404
    error propagation, and the no-creds skip (return None, never crash).
  - delete_calendar_event / delete_google_task: no-op guards.
  - check_conflict / get_google_calendar_events: response parsing.

All Google API surface is replaced with mocks — no network, no DB rows
(this is the D1 "mock-orchestration-only" default; the real-API contract
is gated behind the opt-in `google_live` marker).
"""

import pytest
from unittest.mock import MagicMock, patch

from core.services.google_service import (
    sync_to_calendar,
    delete_calendar_event,
    delete_calendar_instance,
    delete_google_task,
    check_conflict,
    get_google_calendar_events,
    get_upcoming_calendar_events,
)

pytestmark = pytest.mark.calendar


# ---------------------------------------------------------------- helpers

def _mock_service():
    """Deep mock service where events() and tasks() return configured mocks."""
    service = MagicMock()
    return service


# ------------------------------------------------- sync_to_calendar: insert

def test_sync_to_calendar_insert_builds_event_body():
    service = _mock_service()
    service.events.return_value.insert.return_value.execute.return_value = {"id": "evt-1"}

    with patch("core.services.google_service.get_cached_service", return_value=service):
        event_id = sync_to_calendar("Send Q3 deck", "2026-06-25T15:00:00+05:30", duration_mins=30)

    assert event_id == "evt-1"
    insert_call = service.events.return_value.insert.call_args
    assert insert_call.kwargs["calendarId"] == "primary"
    body = insert_call.kwargs["body"]
    # Default priority prefix
    assert body["summary"] == "⚡ ACTION: Send Q3 deck"
    assert body["description"] == "Rhodey created this for you."
    assert body["start"] == {"dateTime": "2026-06-25T15:00:00+05:30", "timeZone": "Asia/Kolkata"}
    # end = start + duration_mins
    assert body["end"]["dateTime"] == "2026-06-25T15:30:00+05:30"
    # popup reminders at 60 + 15 minutes
    assert body["reminders"] == {
        "useDefault": False,
        "overrides": [
            {"method": "popup", "minutes": 60},
            {"method": "popup", "minutes": 15},
        ],
    }
    # no recurrence key unless requested
    assert "recurrence" not in body


def test_sync_to_calendar_priority_prefixes():
    service = _mock_service()

    with patch("core.services.google_service.get_cached_service", return_value=service):
        sync_to_calendar("Deploy hotfix", "2026-06-25T15:00:00+05:30", priority="urgent")
        sync_to_calendar("Coffee chat", "2026-06-25T16:00:00+05:30", priority="low")
        sync_to_calendar("Standup", "2026-06-25T17:00:00+05:30")

    summaries = [c.kwargs["body"]["summary"] for c in service.events.return_value.insert.call_args_list]
    assert summaries == [
        "🔥 CRITICAL: Deploy hotfix",
        "☕ INFO: Coffee chat",
        "⚡ ACTION: Standup",
    ]


def test_sync_to_calendar_strips_existing_prefix_before_repairing():
    """A title that already carries the prefix must not double-prefix."""
    service = _mock_service()

    with patch("core.services.google_service.get_cached_service", return_value=service):
        sync_to_calendar("⚡ ACTION: Already formatted", "2026-06-25T15:00:00+05:30")

    body = service.events.return_value.insert.call_args.kwargs["body"]
    assert body["summary"] == "⚡ ACTION: Already formatted"


def test_sync_to_calendar_passes_recurrence_through():
    service = _mock_service()
    with patch("core.services.google_service.get_cached_service", return_value=service):
        sync_to_calendar(
            "Weekly sync",
            "2026-06-25T15:00:00+05:30",
            recurrence="RRULE:FREQ=WEEKLY;BYDAY=TH",
        )
    body = service.events.return_value.insert.call_args.kwargs["body"]
    assert body["recurrence"] == ["RRULE:FREQ=WEEKLY;BYDAY=TH"]


# ------------------------------------------------- sync_to_calendar: patch

def test_sync_to_calendar_with_event_id_patches_instead_of_inserts():
    service = _mock_service()
    service.events.return_value.patch.return_value.execute.return_value = {"id": "evt-9"}

    with patch("core.services.google_service.get_cached_service", return_value=service):
        event_id = sync_to_calendar("Move standup", "2026-06-25T18:00:00+05:30", event_id="evt-9")

    assert event_id == "evt-9"
    service.events.return_value.insert.assert_not_called()
    patch_call = service.events.return_value.patch.call_args
    assert patch_call.kwargs["eventId"] == "evt-9"
    assert patch_call.kwargs["body"]["summary"] == "⚡ ACTION: Move standup"


class _FakeHttpError404(Exception):
    def __init__(self):
        super().__init__("404 Not Found")
        self.resp = type("Resp", (), {"status": 404})()


def test_sync_to_calendar_404_heals_db_and_reprovisions():
    """Event deleted externally → heal DB (null google_event_id), then insert fresh."""
    service = _mock_service()
    service.events.return_value.patch.return_value.execute.side_effect = _FakeHttpError404()
    service.events.return_value.insert.return_value.execute.return_value = {"id": "new-evt-1"}

    mock_tenant = MagicMock()

    with patch("core.services.google_service.get_cached_service", return_value=service), \
         patch("core.services.db.tenant_aware_client", return_value=mock_tenant), \
         patch("core.services.google_service.audit_log_sync") as audit:
        event_id = sync_to_calendar("Healed event", "2026-06-25T15:00:00+05:30", event_id="gone-1")

    assert event_id == "new-evt-1"
    # Heal: null out the dangling external id on the tenant's rows
    heal_update = mock_tenant.table.return_value.update
    heal_update.assert_called_once_with({"google_event_id": None})
    heal_update.return_value.eq.assert_called_once_with("google_event_id", "gone-1")
    heal_update.return_value.eq.return_value.eq.assert_called_once_with("is_current", True)
    heal_update.return_value.eq.return_value.eq.return_value.execute.assert_called_once()
    # The 404 was logged as a healing event
    assert any(call.args[0] == "google_service" for call in audit.call_args_list)
    # Provisioned a fresh event
    assert service.events.return_value.insert.called


def test_sync_to_calendar_non_404_error_propagates():
    """Temporary errors (500/429/403) must raise — never null the DB on a lie."""
    service = _mock_service()
    service.events.return_value.patch.return_value.execute.side_effect = ValueError("boom")

    with patch("core.services.google_service.get_cached_service", return_value=service):
        with pytest.raises(ValueError, match="boom"):
            sync_to_calendar("Fail", "2026-06-25T15:00:00+05:30", event_id="evt-1")


def test_sync_to_calendar_no_creds_returns_none():
    with patch("core.services.google_service.get_cached_service", return_value=None):
        assert sync_to_calendar("No creds", "2026-06-25T15:00:00+05:30") is None


# ----------------------------------------------------------- deletions

def test_delete_calendar_event_noop_guards():
    # no event id → no call
    delete_calendar_event(None)
    # no creds → no call
    with patch("core.services.google_service.get_cached_service", return_value=None):
        delete_calendar_event("evt-1")
    # happy path
    service = _mock_service()
    with patch("core.services.google_service.get_cached_service", return_value=service):
        delete_calendar_event("evt-1")
    service.events.return_value.delete.assert_called_once_with(calendarId="primary", eventId="evt-1")


def test_delete_google_task_noop_guards():
    delete_google_task(None)
    with patch("core.services.google_service.get_tasks_service", return_value=None):
        delete_google_task("task-1")
    service = _mock_service()
    with patch("core.services.google_service.get_tasks_service", return_value=service):
        delete_google_task("task-1")
    service.tasks.return_value.delete.assert_called_once_with(tasklist="@default", task="task-1")


# ------------------------------------------------------- read paths

def test_check_conflict_returns_blocking_event_summary():
    service = _mock_service()
    service.events.return_value.list.return_value.execute.return_value = {
        "items": [{"summary": "Deep work block"}]
    }
    with patch("core.services.google_service.get_cached_service", return_value=service):
        assert check_conflict("2026-06-25T15:00:00+05:30") == "Deep work block"


def test_check_conflict_returns_none_when_free():
    service = _mock_service()
    service.events.return_value.list.return_value.execute.return_value = {"items": []}
    with patch("core.services.google_service.get_cached_service", return_value=service):
        assert check_conflict("2026-06-25T15:00:00+05:30") is None


def test_get_google_calendar_events_parses_date_and_datetime():
    service = _mock_service()
    service.events.return_value.list.return_value.execute.return_value = {
        "items": [
            {"id": "e1", "summary": "Timed", "start": {"dateTime": "2026-06-25T15:00:00+05:30"}},
            {"id": "e2", "summary": "All-day", "start": {"date": "2026-06-26"}},
        ]
    }
    with patch("core.services.google_service.get_cached_service", return_value=service):
        # a plain date (not datetime) must scope the whole day — it used to
        # crash on .replace(hour=...) and silently return []
        events = get_google_calendar_events(__import__("datetime").date(2026, 6, 25))

    assert events == [
        {"time": "2026-06-25T15:00:00+05:30", "title": "Timed", "source": "google", "id": "e1"},
        {"time": "2026-06-26", "title": "All-day", "source": "google", "id": "e2"},
    ]
    # the list call is bounded to the target day
    list_kwargs = service.events.return_value.list.call_args.kwargs
    assert list_kwargs["singleEvents"] is True
    assert list_kwargs["timeMin"].startswith("2026-06-25")
    assert list_kwargs["timeMax"].startswith("2026-06-26")


def test_get_upcoming_calendar_events_bounds_and_parsing():
    """14-day lookahead from the frozen clock; parses the same event shape.
    (Deferred-ledger X7's second half — folded into the #6 clock work.)"""
    from freezegun import freeze_time
    from datetime import datetime as _dt, timezone as _tz
    service = _mock_service()
    service.events.return_value.list.return_value.execute.return_value = {
        "items": [
            {"id": "u1", "summary": "Next week", "start": {"dateTime": "2026-01-10T10:00:00+05:30"}},
        ]
    }
    frozen = _dt(2026, 1, 5, 4, 30, tzinfo=_tz.utc)  # 10:00 IST Monday
    with freeze_time(frozen), \
         patch("core.services.google_service.get_cached_service", return_value=service):
        events = get_upcoming_calendar_events(days=14)
    assert events == [
        {"time": "2026-01-10T10:00:00+05:30", "title": "Next week", "source": "google", "id": "u1"},
    ]
    list_kwargs = service.events.return_value.list.call_args.kwargs
    assert list_kwargs["timeMin"] == "2026-01-05T04:30:00+00:00"
    assert list_kwargs["timeMax"] == "2026-01-19T04:30:00+00:00"  # +14 days
    assert list_kwargs["maxResults"] == 100


def test_delete_calendar_instance_guards_and_call():
    delete_calendar_instance(None, "i1")   # no series id → no-op
    delete_calendar_instance("r1", None)   # no instance id → no-op
    service = _mock_service()
    with patch("core.services.google_service.get_cached_service", return_value=service):
        delete_calendar_instance("r1", "i1")
    service.events.return_value.delete.assert_called_once_with(calendarId="primary", eventId="i1")
