import pytest
from unittest.mock import patch, MagicMock
from core.pulse.tools import update_task_status
from tests.fixtures.task_factory import factory
from core.services.db import get_supabase

supabase = get_supabase()

@pytest.fixture(autouse=True)
def cleanup():
    yield
    factory.cleanup_by_title_prefix("[TEST]")

def test_telegram_cancellation_deletes_calendar(mock_google_apis):
    # 2a - Telegram cancellation -> Calendar deletion
    task = factory.create_task(
        title="[TEST] Cancellation Task",
        google_event_id="mock_event_123"
    )
    
    # Mark cancelled
    res = update_task_status(task["id"], status="cancelled")
    assert "updated successfully" in res
    
    # Verify the mock was called to delete the calendar event
    mock_google_apis["delete_calendar_event"].assert_called_once_with("mock_event_123")

def test_external_deletion_404_handling():
    # 2b - External deletion (404 handling)
    task = factory.create_task(
        title="[TEST] 404 Handling Task",
        google_event_id="mock_deleted_event_123"
    )

    class MockHttpError(Exception):
        pass
        
    mock_events = MagicMock()
    mock_events.patch.side_effect = MockHttpError("404 Not Found")
    mock_events.insert.return_value.execute.return_value = {"id": "new_healed_event_id"}
    
    mock_service = MagicMock()
    mock_service.events.return_value = mock_events

    with patch("core.services.google_service.get_cached_service", return_value=mock_service):
        with patch("core.pulse.tools.sync_to_google"):
            # Call the real update_task_status, which calls the real sync_to_calendar
            res = update_task_status(task["id"], status="todo", reminder_at="2026-10-10T10:00:00Z")

    assert "updated successfully" in res

    db_task = supabase.table("tasks").select("*").eq("id", task["id"]).eq("is_current", True).execute().data[0]
    assert db_task["google_event_id"] == "new_healed_event_id", "DB was not healed and provisioned with new event ID"

def test_reminder_at_removed_deletes_calendar(mock_google_apis):
    # 2c - reminder_at removed -> intentional deletion
    task = factory.create_task(
        title="[TEST] Reminder Removed Task",
        google_event_id="mock_event_to_delete"
    )
    
    # Call update_task_status with a status update but no reminder_at
    # Default args: status="done", reminder_at=None
    # But wait, bug C5 is about when you just update to todo and omit reminder_at.
    update_task_status(task["id"], status="todo")

    # This will call delete_calendar_event because of:
    # elif e_id: delete_calendar_event(e_id)
    # We want to verify that this happens.
    mock_google_apis["delete_calendar_event"].assert_called_once_with("mock_event_to_delete")
    
    # But wait, does it actually null out google_event_id in the DB?
    db_task = supabase.table("tasks").select("*").eq("id", task["id"]).eq("is_current", True).execute().data[0]
    assert db_task["google_event_id"] is None, "google_event_id should be nulled out in DB"
