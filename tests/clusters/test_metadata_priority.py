import pytest
from dotenv import load_dotenv

load_dotenv()

from core.pulse.tools import update_task_status  # noqa: E402
from core.services.db import get_supabase  # noqa: E402
from tests.fixtures.task_factory import factory  # noqa: E402

supabase = get_supabase()


@pytest.fixture(autouse=True)
def cleanup():
    yield
    factory.cleanup_by_title_prefix("[TEST]")


def test_priority_and_metadata_persistence():
    task = factory.create_task(
        title="[TEST] Metadata Clobbering Task",
        priority="urgent",
        recurrence="none"
    )
    task_id = task["id"]

    res = update_task_status(task_id, status="done")
    assert "updated successfully" in res

    updated_task_res = supabase.table("tasks").select("*").eq("id", task_id).maybe_single().execute()
    updated_task = updated_task_res.data

    assert updated_task is not None
    assert updated_task["status"] == "done"
    assert updated_task["priority"] == "urgent"
    assert updated_task["is_current"] is True
    assert updated_task["version"] == task["version"] + 1

    historical_id = updated_task["supersedes_id"]
    assert historical_id is not None

    historical_task_res = supabase.table("tasks").select("*").eq("id", historical_id).maybe_single().execute()
    historical_task = historical_task_res.data

    assert historical_task is not None
    assert historical_task["status"] == "todo"
    assert historical_task["priority"] == "urgent"
    assert historical_task["is_current"] is False
