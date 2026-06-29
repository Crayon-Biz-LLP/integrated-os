import pytest
from unittest.mock import patch
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
    #
    # We mock skip_recurring_instance to return "No upcoming instances found",
    # which triggers the permanent close path in update_task_status.
    
    task = factory.create_task(
        title="[TEST] Boundary Reached Task",
        recurrence="RRULE:FREQ=WEEKLY;UNTIL=20260601T000000Z",
        google_event_id="mock_expired_recurring_event"
    )
    
    with patch("core.pulse.tools.skip_recurring_instance", return_value="No upcoming instances found for recurring event '[TEST] Boundary Reached Task'."):
        res = update_task_status(task["id"], status="done")
        
    db_task = supabase.table("tasks").select("*").eq("id", task["id"]).eq("is_current", True).execute().data[0]
    
    if db_task["status"] == "todo" and "The series continues" in res:
        pytest.fail("Finding: Expired recurring tasks infinitely loop as 'todo' when completed, polluting the task board forever.")
        
    assert db_task["status"] == "done", "Master task should be completed when series is exhausted."
