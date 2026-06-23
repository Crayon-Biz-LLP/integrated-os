import pytest
from unittest.mock import patch
from tests.fixtures.task_factory import factory
from core.webhook.completion_handler import execute_completion_closure
from core.services.db import get_supabase

supabase = get_supabase()

@pytest.fixture(autouse=True)
def cleanup():
    yield
    factory.cleanup_by_title_prefix("[TEST]")

@pytest.mark.asyncio
async def test_batch_partial_sync_failure():
    # Create 3 tasks
    t1 = factory.create_task("[TEST] Batch Task 1")
    t2 = factory.create_task("[TEST] Batch Task 2")
    t3 = factory.create_task("[TEST] Batch Task 3")
    
    task_ids = [t1["id"], t2["id"], t3["id"]]
    active_tasks = [{"id": tid, "title": f"Task {tid}"} for tid in task_ids]
    
    # We will mock update_task_status to succeed for 1 and 2, but fail for 3
    # We want to see if tasks 1 and 2 are committed to the DB while 3 is not
    # Actually, execute_completion_closure calls update_task_status. Since
    # update_task_status COMMITS to DB immediately, 1 and 2 will be saved,
    # and 3 will fail. This means split state!
    
    call_count = 0
    def mock_update_task_status(task_id, status):
        nonlocal call_count
        call_count += 1
        if call_count == 3:
            return "Error updating task: Simulated 500 API Error"
        
        # Manually do the DB update for success so we can observe the split state
        supabase.table("tasks").update({"status": "done"}).eq("id", task_id).execute()
        return "Task updated successfully."
        
    with patch("core.pulse.tools.update_task_status", side_effect=mock_update_task_status):
        # dump_id is required, we can mock it by passing a non-existent one
        # but that would fail the _park function. So let's mock _park and _send too
        with patch("core.webhook.completion_handler._park"), patch("core.webhook.completion_handler._send"):
            await execute_completion_closure(
                dump_id=99999, 
                validated_ids=task_ids, 
                chat_id=123, 
                receipt="Receipt", 
                entity="Entity", 
                active_tasks=active_tasks
            )
            
    # Check DB state
    t1_db = supabase.table("tasks").select("status").eq("id", t1["id"]).eq("is_current", True).execute().data[0]
    t2_db = supabase.table("tasks").select("status").eq("id", t2["id"]).eq("is_current", True).execute().data[0]
    t3_db = supabase.table("tasks").select("status").eq("id", t3["id"]).eq("is_current", True).execute().data[0]
    
    # Documenting the finding: split state occurs
    assert t1_db["status"] == "done"
    assert t2_db["status"] == "done"
    assert t3_db["status"] == "todo"
    
    # If this assertion passes, we have confirmed that the batch is NOT transactional.
    # It leaves the system in a split state where partial syncs are permanently recorded.
