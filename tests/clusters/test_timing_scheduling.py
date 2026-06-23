from unittest.mock import patch, MagicMock
from core.services.google_service import sync_to_calendar

def test_timezone_handling_documents_current_behaviour():
    """
    Documents the current timezone handling in format_rfc3339 and sync_to_calendar.

    Key finding: 'Z' (UTC) strings are intentionally passed through untouched.
    This is safe in practice because the LLM is prompted to output IST times,
    so Z-strings never appear in the real task creation flow.
    Naive strings (no offset) correctly get +05:30 appended — this is the live path.

    If the LLM is ever changed or an external source feeds UTC strings,
    this test will catch the regression point.
    """
    mock_events = MagicMock()
    mock_service = MagicMock()
    mock_service.events.return_value = mock_events

    with patch("core.services.google_service.get_cached_service", return_value=mock_service):
        # Case A: Z (UTC) strings pass through unchanged — the LLM never produces these in practice.
        sync_to_calendar("Test UTC", "2026-06-25T15:00:00Z")
        call_args_a = mock_events.insert.call_args[1]["body"]
        assert call_args_a["start"]["dateTime"] == "2026-06-25T15:00:00Z"

        # Case B: Naive strings (no offset) get +05:30 appended. This is the real production path.
        sync_to_calendar("Test Naive", "2026-06-25T15:00:00")
        call_args_b = mock_events.insert.call_args[1]["body"]
        assert call_args_b["start"]["dateTime"] == "2026-06-25T15:00:00+05:30"

        # Case C: Explicit IST strings are preserved as-is.
        sync_to_calendar("Test IST", "2026-06-25T15:00:00+05:30")
        call_args_c = mock_events.insert.call_args[1]["body"]
        assert call_args_c["start"]["dateTime"] == "2026-06-25T15:00:00+05:30"
