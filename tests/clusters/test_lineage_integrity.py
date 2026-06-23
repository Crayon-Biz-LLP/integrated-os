import concurrent.futures

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


def test_rapid_fire_lineage_integrity(mock_google_apis):
    task = factory.create_task(
        title="[TEST] Rapid Fire Task",
        priority="normal",
        recurrence="none"
    )
    task_id = task["id"]

    def update_status():
        return update_task_status(task_id, status="done")

    def update_reminder():
        return update_task_status(task_id, status="todo", reminder_at="2026-10-10T10:00:00Z")

    def update_cancel():
        return update_task_status(task_id, status="cancelled")

    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
        f1 = executor.submit(update_status)
        f2 = executor.submit(update_reminder)
        f3 = executor.submit(update_cancel)
        # Consume results to surface any exceptions from threads
        for f in [f1, f2, f3]:
            f.result()

    final_task_res = supabase.table("tasks").select("*").eq("id", task_id).execute()
    tasks = final_task_res.data
    assert len(tasks) == 1, "There should only be one row with the original ID"
    final_task = tasks[0]

    history_res = (
        supabase.table("tasks")
        .select("*")
        .ilike("title", "[TEST] Rapid Fire Task")
        .eq("is_current", False)
        .execute()
    )
    historical_tasks = history_res.data

    assert final_task["version"] == 1 + len(historical_tasks)

    versions = [t["version"] for t in historical_tasks] + [final_task["version"]]
    assert len(versions) == len(set(versions)), "Versions should be strictly unique across lineage"
