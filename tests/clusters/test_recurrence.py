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

def test_recurrence_boundary_handling():
    # 3. Recurrence boundary (UNTIL date) - the "infinite loop" check
    # If a recurring task has reached its UNTIL date in Google Calendar,
    # there are no more upcoming instances.
    # Marking it 'done' should completely close the master task in Supabase,
    # rather than pretending the series continues.
    
    task = factory.create_task(
        title="[TEST] Boundary Reached Task",
        recurrence="RRULE:FREQ=WEEKLY;UNTIL=20260601T000000Z",
        google_event_id="mock_expired_recurring_event"
    )
    
    # We mock google api instances() to return empty list,
    # simulating that the UNTIL date has passed.
    mock_events = MagicMock()
    mock_service = MagicMock()
    mock_service.events.return_value = mock_events
    
    # Return an empty list of instances
    mock_events.instances.return_value.execute.return_value = {"items": []}

    with patch("googleapiclient.discovery.build", return_value=mock_service):
        res = update_task_status(task["id"], status="done")
        
    # Document the finding:
    # Does it successfully mark the task as "done" completely,
    # or does it say "The series continues" and leave the task as "todo"?
    db_task = supabase.table("tasks").select("*").eq("id", task["id"]).eq("is_current", True).execute().data[0]
    
    # Expected correct behavior: if there are no more instances, the master task should be marked 'done'
    # Actual current behavior: leaves it 'todo' and says 'The series continues'.
    if db_task["status"] == "todo" and "The series continues" in res:
        pytest.fail("Finding: Expired recurring tasks infinitely loop as 'todo' when completed, polluting the task board forever.")
        
    assert db_task["status"] == "done", "Master task should be completed when series is exhausted."
