import pytest
import os
from unittest.mock import patch
from core.services.db import get_supabase
from core.retrieval.pipeline import schedule_index_memory, process_pending_index_jobs

skip_unless_live_db = pytest.mark.skipif(
    os.getenv("LIVE_DB") != "true",
    reason="Requires LIVE_DB=true (real Supabase)"
)


def _enable_indexing():
    """Enable retrieval indexing for the duration of a test."""
    # The property reads os.environ each time it's called, so patching
    # the env var is sufficient.
    import os as _os
    _os.environ["RETRIEVAL_INDEXING_ENABLED"] = "true"


def _disable_indexing():
    import os as _os
    _os.environ["RETRIEVAL_INDEXING_ENABLED"] = "false"


@pytest.fixture(autouse=True)
def _ensure_indexing_env():
    """Set RETRIEVAL_INDEXING_ENABLED=true for all tests in this file."""
    _enable_indexing()
    yield
    _disable_indexing()


def _cleanup_jobs(supabase, memory_id: int):
    """Remove any pending jobs for the given memory."""
    try:
        supabase.table("pending_retrieval_index_jobs") \
            .delete() \
            .eq("memory_id", memory_id) \
            .execute()
    except Exception:
        pass


# ── C1: Enqueue ────────────────────────────────────────────────────────


@skip_unless_live_db
@pytest.mark.asyncio
async def test_enqueue_creates_pending_job(seed_test_data):
    """C1 — schedule_index_memory creates a pending job row."""
    supabase = get_supabase()
    mem_id = seed_test_data["memories"][0]

    try:
        schedule_index_memory(mem_id, "test content", "note", "sim_test")

        jobs = supabase.table("pending_retrieval_index_jobs") \
            .select("id, memory_id, status, priority") \
            .eq("memory_id", mem_id) \
            .execute()
        assert len(jobs.data) >= 1, f"No job found for memory {mem_id}"
        assert jobs.data[0]["status"] == "pending"
        assert jobs.data[0]["priority"] == 0
    finally:
        _cleanup_jobs(supabase, mem_id)


# ── C2: Process ────────────────────────────────────────────────────────


@skip_unless_live_db
@pytest.mark.asyncio
async def test_process_completes_job(seed_test_data):
    """C2 — process_pending_index_jobs transitions a pending job to completed.

    index_memory is mocked to return True so we don't need real LLM calls.
    """
    supabase = get_supabase()
    mem_id = seed_test_data["memories"][0]
    mem_content = "test content for indexing"

    try:
        schedule_index_memory(mem_id, mem_content, "note", "sim_test")

        # Verify job exists before processing
        jobs_before = supabase.table("pending_retrieval_index_jobs") \
            .select("id") \
            .eq("memory_id", mem_id) \
            .execute()
        assert len(jobs_before.data) == 1

        # Mock index_memory to succeed without real LLM calls
        with patch("core.retrieval.pipeline.index_memory") as mock_index:
            mock_index.return_value = True
            processed = await process_pending_index_jobs(max_jobs=10)

        assert processed >= 1, "process_pending_index_jobs returned 0"

        # Job should be completed
        jobs_after = supabase.table("pending_retrieval_index_jobs") \
            .select("id, status") \
            .eq("memory_id", mem_id) \
            .execute()
        assert len(jobs_after.data) == 1
        assert jobs_after.data[0]["status"] == "completed", \
            f"Expected completed, got {jobs_after.data[0]['status']}"
    finally:
        _cleanup_jobs(supabase, mem_id)


# ── C3: Dedupe ─────────────────────────────────────────────────────────


@skip_unless_live_db
@pytest.mark.asyncio
async def test_enqueue_dedupes_identical_memory(seed_test_data):
    """C3 — Calling schedule_index_memory twice is idempotent.

    Only one pending job row should exist for the same memory_id.
    """
    supabase = get_supabase()
    mem_id = seed_test_data["memories"][0]

    try:
        schedule_index_memory(mem_id, "dedupe test", "note", "sim_test")
        schedule_index_memory(mem_id, "dedupe test again", "note", "sim_test")

        jobs = supabase.table("pending_retrieval_index_jobs") \
            .select("id, status") \
            .eq("memory_id", mem_id) \
            .execute()

        active_jobs = [j for j in jobs.data if j["status"] in ("pending", "processing")]
        assert len(active_jobs) == 1, (
            f"Expected 1 active job, got {len(active_jobs)}: {[j['status'] for j in active_jobs]}"
        )
    finally:
        _cleanup_jobs(supabase, mem_id)


# ── C4: Retry / dead-letter ────────────────────────────────────────────


@skip_unless_live_db
@pytest.mark.asyncio
async def test_failed_job_retries_then_dead_letter(seed_test_data):
    """C4 — A job that fails repeatedly escalates to dead_letter after 3 retries."""
    supabase = get_supabase()
    mem_id = seed_test_data["memories"][0]

    try:
        schedule_index_memory(mem_id, "retry test content", "note", "sim_test")

        # Make index_memory fail every time
        with patch("core.retrieval.pipeline.index_memory") as mock_index:
            mock_index.return_value = False

            # Attempt 1: should stay as pending (retry_count=1)
            n1 = await process_pending_index_jobs(max_jobs=10)
            assert n1 >= 1

            # Attempt 2: should stay as pending (retry_count=2)
            n2 = await process_pending_index_jobs(max_jobs=10)
            assert n2 >= 1

            # Attempt 3: retry_count reaches 3 → dead_letter
            n3 = await process_pending_index_jobs(max_jobs=10)
            assert n3 >= 1

        job = supabase.table("pending_retrieval_index_jobs") \
            .select("id, status, retry_count, error") \
            .eq("memory_id", mem_id) \
            .maybe_single() \
            .execute()
        assert job and job.data, "Job not found after retries"
        assert job.data["status"] == "dead_letter", (
            f"Expected dead_letter after 3 retries, got {job.data['status']} "
            f"(retry_count={job.data['retry_count']}, error={job.data.get('error', 'none')})"
        )
        assert job.data["retry_count"] >= 3
    finally:
        _cleanup_jobs(supabase, mem_id)
