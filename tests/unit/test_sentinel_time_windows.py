"""Boundary-clock tests for sentinel calendar time windows (sentinel aspect).

Covers `core/pulse/sentinel.py`:
  - `get_recently_ended_events`: the post-meeting capture window. Filters
    API events by ACTUAL end time (5–30 min ago) because Google only
    filters by start. Boundary-inclusive; events without an end time are
    skipped; no-creds → [].
  - `get_upcoming_events`: the pre-meeting nudge window (0–60 min ahead).
    Asserts the exact timeMin/timeMax sent to the API and the no-creds skip.

Clock frozen with freezegun (UTC — the functions anchor on
datetime.now(timezone.utc)); the Google service is mocked.
"""

import pytest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from core.pulse.sentinel import get_recently_ended_events, get_upcoming_events

pytestmark = pytest.mark.sentinel

UTC = timezone.utc
# Fixed instant: Monday 2026-01-05 10:00:00 UTC
_FROZEN = datetime(2026, 1, 5, 10, 0, 0, tzinfo=UTC)


def _ended_event(end_iso: str):
    return {"id": f"evt-{end_iso}", "summary": "Meeting", "end": {"dateTime": end_iso}}


def _service_with(events):
    service = MagicMock()
    service.events.return_value.list.return_value.execute.return_value = {"items": events}
    return service


# ------------------------------------------------- recently-ended window

def test_recently_ended_filters_by_actual_end_time():
    from freezegun import freeze_time
    events = [
        _ended_event("2026-01-05T09:50:00Z"),   # 10 min ago → in window
        _ended_event("2026-01-05T09:58:00Z"),   # 2 min ago → too recent
        _ended_event("2026-01-05T09:15:00Z"),   # 45 min ago → too old
        _ended_event("2026-01-05T09:55:00Z"),   # exactly 5 min → boundary in
        _ended_event("2026-01-05T09:30:00Z"),   # exactly 30 min → boundary in
    ]
    service = _service_with(events)

    with freeze_time(_FROZEN), \
         patch("core.pulse.sentinel.get_cached_service", return_value=service):
        ended = get_recently_ended_events()

    ids = sorted(e["id"] for e in ended)
    assert ids == sorted(["evt-2026-01-05T09:50:00Z", "evt-2026-01-05T09:55:00Z", "evt-2026-01-05T09:30:00Z"])


def test_recently_ended_skips_events_without_end_time():
    from freezegun import freeze_time
    service = _service_with([
        {"id": "no-end", "summary": "All-day"},
        _ended_event("2026-01-05T09:50:00Z"),
    ])
    with freeze_time(_FROZEN), \
         patch("core.pulse.sentinel.get_cached_service", return_value=service):
        ended = get_recently_ended_events()
    assert [e["id"] for e in ended] == ["evt-2026-01-05T09:50:00Z"]


def test_recently_ended_fetches_wider_window_by_start_time():
    from freezegun import freeze_time
    service = _service_with([])
    with freeze_time(_FROZEN), \
         patch("core.pulse.sentinel.get_cached_service", return_value=service):
        get_recently_ended_events()

    list_kwargs = service.events.return_value.list.call_args.kwargs
    # timeMin = now − (30+120) min = 07:30 UTC
    assert list_kwargs["timeMin"] == "2026-01-05T07:30:00+00:00"
    assert list_kwargs["singleEvents"] is True


def test_recently_ended_no_creds_returns_empty():
    with patch("core.pulse.sentinel.get_cached_service", return_value=None):
        assert get_recently_ended_events() == []


# ------------------------------------------------------ upcoming window

def test_upcoming_events_bounds_are_now_to_now_plus_ahead():
    from freezegun import freeze_time
    service = _service_with([{"id": "e1", "summary": "Standup", "start": {"dateTime": "2026-01-05T10:30:00Z"}}])
    with freeze_time(_FROZEN), \
         patch("core.pulse.sentinel.get_cached_service", return_value=service):
        events = get_upcoming_events(minutes_ahead=60)

    assert [e["id"] for e in events] == ["e1"]
    list_kwargs = service.events.return_value.list.call_args.kwargs
    assert list_kwargs["timeMin"] == "2026-01-05T10:00:00+00:00"
    assert list_kwargs["timeMax"] == "2026-01-05T11:00:00+00:00"


def test_upcoming_events_respects_custom_ahead():
    from freezegun import freeze_time
    service = _service_with([])
    with freeze_time(_FROZEN), \
         patch("core.pulse.sentinel.get_cached_service", return_value=service):
        get_upcoming_events(minutes_ahead=15)
    list_kwargs = service.events.return_value.list.call_args.kwargs
    assert list_kwargs["timeMax"] == "2026-01-05T10:15:00+00:00"


def test_upcoming_events_no_creds_returns_empty():
    # Both providers must be neutralized: sentinel falls back to Outlook when
    # Google has no creds, and a real OUTLOOK_ACCESS_TOKEN in the environment
    # would leak live events into this hermetic test.
    with patch("core.pulse.sentinel.get_cached_service", return_value=None), \
         patch("core.services.outlook_service.get_outlook_calendar_events_range", return_value=[]):
        assert get_upcoming_events() == []
