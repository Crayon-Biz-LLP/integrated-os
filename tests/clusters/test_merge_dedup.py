import pytest
from tests.fixtures.task_factory import factory
from core.webhook.utils import is_already_in_tasks_table

@pytest.fixture(autouse=True)
def cleanup():
    yield
    factory.cleanup_by_title_prefix("[TEST]")

def test_dedup_exact_match():
    # Create a floating task
    factory.create_task(title="[TEST] Exact Match Dedup")
    
    # Check if duplicate is caught
    result = is_already_in_tasks_table("[TEST] Exact Match Dedup")
    assert result["result"] in ["block", "flag"], f"Expected to catch exact duplicate, got: {result}"

def test_dedup_case_sensitivity():
    factory.create_task(title="[TEST] CASE Sensitivity task")
    
    # Different case
    result = is_already_in_tasks_table("[TEST] case sensitivity task")
    # This will document whether the match is case-insensitive or not.
    # We assert it SHOULD catch it if case-insensitive logic is properly implemented.
    assert result["result"] in ["block", "flag"], f"Expected to catch case-insensitive duplicate, got: {result}"

def test_dedup_time_bound_vs_floating():
    # Create a time-bound task
    factory.create_task(title="[TEST] Time Bound Sync", reminder_at="2026-06-25T10:00:00Z")
    
    # If a generic task arrives without time, does it dedup based on title?
    # is_already_in_tasks_table only checks title currently.
    result = is_already_in_tasks_table("[TEST] Time Bound Sync")
    assert result["result"] in ["block", "flag"], f"Expected title-only dedup to catch time-bound task, got: {result}"
