"""Hermetic orchestration tests for Google Tasks two-way sync (`sync_to_google`).

Covers `core/services/google_service.sync_to_google`:
  - completion patch (done/cancelled → status: completed)
  - insert-vs-patch routing by presence of a google task id
  - date-only vs datetime `due` formatting (the len<=10 all-day path)
  - the time-visibility title hack (🕒 HH:MM prefix for explicit times)

This is the "calendar/task two-way sync orchestration" half of the sync
aspect — the calendar half lives in test_google_orchestration.py. Google
API surface is mocked; no network or DB.
"""

import pytest
from unittest.mock import MagicMock

from core.services.google_service import sync_to_google

pytestmark = pytest.mark.sync


def _mock_service():
    return MagicMock()


# ----------------------------------------------------------- completion

def test_sync_to_google_completes_task_on_done():
    service = _mock_service()
    service.tasks.return_value.patch.return_value.execute.return_value = {"id": "task-9"}

    result = sync_to_google(service, task_id="task-9", status="done", title="Old title")

    assert result == "task-9"
    service.tasks.return_value.patch.assert_called_once_with(
        tasklist="@default", task="task-9", body={"status": "completed"}
    )


def test_sync_to_google_completes_task_on_cancelled():
    service = _mock_service()
    service.tasks.return_value.patch.return_value.execute.return_value = {"id": "task-9"}

    sync_to_google(service, task_id="task-9", status="cancelled")

    service.tasks.return_value.patch.assert_called_once_with(
        tasklist="@default", task="task-9", body={"status": "completed"}
    )


def test_sync_to_google_completion_failure_returns_none():
    service = _mock_service()
    service.tasks.return_value.patch.return_value.execute.side_effect = Exception("api down")

    assert sync_to_google(service, task_id="task-9", status="done") is None


# ------------------------------------------------- insert vs patch

def test_sync_to_google_inserts_when_no_task_id():
    service = _mock_service()
    service.tasks.return_value.insert.return_value.execute.return_value = {"id": "new-task"}

    result = sync_to_google(service, title="Buy milk", due_at="2026-06-25")

    assert result == "new-task"
    body = service.tasks.return_value.insert.call_args.kwargs["body"]
    assert body["title"] == "Buy milk"
    # date-only (len<=10) → all-day-style midnight UTC
    assert body["due"] == "2026-06-25T00:00:00.000Z"


def test_sync_to_google_patches_when_task_id_given():
    service = _mock_service()
    service.tasks.return_value.patch.return_value.execute.return_value = {"id": "task-5"}

    result = sync_to_google(service, title="Reschedule", due_at="2026-06-25", task_id="task-5")

    assert result == "task-5"
    service.tasks.return_value.insert.assert_not_called()
    body = service.tasks.return_value.patch.call_args.kwargs["body"]
    assert body["title"] == "Reschedule"
    assert body["due"] == "2026-06-25T00:00:00.000Z"


def test_sync_to_google_datetime_due_keeps_offset():
    service = _mock_service()
    service.tasks.return_value.insert.return_value.execute.return_value = {"id": "t"}

    sync_to_google(service, title="With time", due_at="2026-06-25T18:30:00+05:30")

    body = service.tasks.return_value.insert.call_args.kwargs["body"]
    assert body["due"] == "2026-06-25T18:30:00+05:30"


# ------------------------------------------- time-visibility title hack

def test_sync_to_google_explicit_time_prefixes_title_with_ist_clock():
    """explicit_time → the title is prefixed 🕒 HH:MM (IST) so the phone shows
    the time without needing to open the task."""
    service = _mock_service()
    service.tasks.return_value.insert.return_value.execute.return_value = {"id": "t"}

    sync_to_google(
        service,
        title="Standup",
        due_at="2026-06-25T09:30:00+05:30",
        explicit_time=True,
    )

    body = service.tasks.return_value.insert.call_args.kwargs["body"]
    assert body["title"] == "🕒 09:30 | Standup"


def test_sync_to_google_explicit_time_does_not_double_prefix():
    service = _mock_service()
    service.tasks.return_value.insert.return_value.execute.return_value = {"id": "t"}

    sync_to_google(
        service,
        title="🕒 09:30 | Standup",
        due_at="2026-06-25T09:30:00+05:30",
        explicit_time=True,
    )

    body = service.tasks.return_value.insert.call_args.kwargs["body"]
    assert body["title"] == "🕒 09:30 | Standup"


def test_sync_to_google_invalid_due_skips_hack_and_due_key():
    """format_rfc3339 returns None for garbage → no clock to derive, no due key."""
    service = _mock_service()
    service.tasks.return_value.insert.return_value.execute.return_value = {"id": "t"}

    sync_to_google(
        service,
        title="No due",
        due_at="not-a-date",
        explicit_time=True,
    )

    body = service.tasks.return_value.insert.call_args.kwargs["body"]
    assert body["title"] == "No due"
    assert "due" not in body
