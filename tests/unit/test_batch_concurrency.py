"""Unit tests for _run_batch_concurrently — the shared helper behind every
/api/*-action/batch route.

Proves the three properties that fix the batch approve/reject slowness and
failure: (1) items run in parallel (bounded), not serially, (2) a failing
item is counted as failed while the rest still process (fail-closed per
item), and (3) items that were already decided are counted as skipped, not
failed — so the app stops reporting "N failed" for things that merely
changed already.
"""


import asyncio

import pytest

from api.index import _run_batch_concurrently, _run_batch_job
pytestmark = pytest.mark.decision



async def _worker_ok(item_id: int) -> None:
    """A worker that just records its item ran (no-op success)."""
    await asyncio.sleep(0)


async def _worker_failing(item_id: int) -> None:
    """A worker that raises — the route-level failure path."""
    raise RuntimeError(f"boom {item_id}")


async def _worker_sleepy(item_id: int) -> None:
    """A worker that sleeps 100ms per item so serial vs parallel is visible."""
    await asyncio.sleep(0.1)


@pytest.mark.asyncio
async def test_all_succeed_counts_processed():
    processed, failed, skipped = await _run_batch_concurrently(
        [1, 2, 3, 4], _worker_ok,
    )
    assert processed == 4
    assert failed == 0
    assert skipped == 0


@pytest.mark.asyncio
async def test_failing_items_count_failed_rest_proceed():
    async def worker(item_id):
        if item_id in (2, 3):
            raise RuntimeError(f"boom {item_id}")
        await asyncio.sleep(0)

    processed, failed, skipped = await _run_batch_concurrently(
        [1, 2, 3, 4], worker,
    )
    assert processed == 2
    assert failed == 2
    assert skipped == 0


@pytest.mark.asyncio
async def test_all_fail():
    processed, failed, skipped = await _run_batch_concurrently(
        [1, 2], _worker_failing,
    )
    assert processed == 0
    assert failed == 2
    assert skipped == 0


@pytest.mark.asyncio
async def test_parallelism_bounded_runs_faster_than_serial():
    """5 × 100ms items at concurrency 3 should take ~200ms, not 500ms —
    proving the helper actually parallelizes (the batch slowness fix)."""
    start = asyncio.get_event_loop().time()
    processed, failed, skipped = await _run_batch_concurrently(
        [1, 2, 3, 4, 5], _worker_sleepy, concurrency=3,
    )
    elapsed = asyncio.get_event_loop().time() - start
    assert processed == 5
    assert failed == 0
    assert skipped == 0
    # 5 items / 3-wide = 2 waves minimum; 100ms each → < 300ms. Serial
    # would be ≥ 500ms. Give a little clock slack for CI.
    assert elapsed < 0.45, f"batch took {elapsed:.3f}s — looks serial"


@pytest.mark.asyncio
async def test_empty_ids_returns_zero():
    processed, failed, skipped = await _run_batch_concurrently([], _worker_ok)
    assert processed == 0
    assert failed == 0
    assert skipped == 0


@pytest.mark.asyncio
async def test_action_key_already_decided_counts_skipped():
    """Email/graph workers signal already-done via the action key."""

    async def worker(item_id):
        return {"success": False, "action": "already_decided",
                "message": f"Item {item_id} was already acked."}

    processed, failed, skipped = await _run_batch_concurrently([7, 8], worker)
    assert processed == 0
    assert failed == 0
    assert skipped == 2


@pytest.mark.asyncio
async def test_channel_message_already_counts_skipped():
    """Channel (call/whatsapp/teams) workers signal already-done via message
    text with action=None — the utils.py shape."""

    async def worker(item_id):
        return {"success": False, "action": None,
                "message": "This call item was already acknowledged."}

    processed, failed, skipped = await _run_batch_concurrently([1], worker)
    assert processed == 0
    assert failed == 0
    assert skipped == 1


@pytest.mark.asyncio
async def test_success_false_without_skip_signal_counts_failed():
    async def worker(item_id):
        return {"success": False, "action": "error", "message": "boom"}

    processed, failed, skipped = await _run_batch_concurrently([1], worker)
    assert processed == 0
    assert failed == 1
    assert skipped == 0


@pytest.mark.asyncio
async def test_mixed_batch_classifies_each_item():
    """Processed / failed / skipped are counted independently in one batch —
    the FYI "re-acknowledge a stale list" scenario (mix of new + already-acked
    + one genuinely failing)."""

    async def worker(item_id):
        if item_id == 1:
            raise RuntimeError("boom")
        if item_id == 2:
            return {"success": False, "action": "already_decided",
                    "message": "already acked"}
        return {"success": True, "action": "approved"}

    processed, failed, skipped = await _run_batch_concurrently([1, 2, 3, 4], worker)
    assert processed == 2
    assert failed == 1
    assert skipped == 1


# --- background batch job (_run_batch_job / _execute_batch_job) ---


@pytest.mark.asyncio
async def test_run_batch_job_returns_job_id_and_pushes_counts(monkeypatch):
    """The route-facing helper returns instantly with a job_id and the
    background task reports honest counts via the completion push."""

    pushes = []

    def noop_audit(*a, **k):
        return None

    async def capture_push(*a, **k):
        pushes.append((a, k))

    monkeypatch.setattr("api.index.audit_log_sync", noop_audit)
    monkeypatch.setattr("api.index.send_push_notification", capture_push)

    async def worker(item_id):
        return {"success": True, "action": "approved"}

    job_id = await _run_batch_job("email", [1, 2], "approve", worker)
    assert isinstance(job_id, str) and len(job_id) == 12

    await asyncio.sleep(0.1)

    assert len(pushes) == 1
    args, kwargs = pushes[0]
    data = kwargs["data"]
    assert data["type"] == "batch_done"
    assert data["processed"] == "2" and data["skipped"] == "0" and data["failed"] == "0"
    assert all(isinstance(v, str) for v in data.values())


@pytest.mark.asyncio
async def test_run_batch_job_worker_exception_pushes_failed(monkeypatch):
    """Fail-closed: a whole-job error still notifies so the batch can't vanish
    (per-item failures are counted as `failed` by the concurrency helper and
    report normally via batch_done — only infra-level breakage hits this)."""

    pushes = []

    def noop_audit(*a, **k):
        return None

    async def capture_push(*a, **k):
        pushes.append((a, k))

    async def exploding_batch(*a, **k):
        raise RuntimeError("batch infra boom")

    monkeypatch.setattr("api.index.audit_log_sync", noop_audit)
    monkeypatch.setattr("api.index.send_push_notification", capture_push)
    monkeypatch.setattr("api.index._run_batch_concurrently", exploding_batch)

    async def worker(item_id):
        return {"success": True, "action": "approved"}

    await _run_batch_job("call", [1], "reject", worker)
    await asyncio.sleep(0.1)

    assert len(pushes) == 1
    assert pushes[0][1]["data"]["type"] == "batch_failed"


@pytest.mark.asyncio
async def test_run_batch_job_bulk_worker_reports_counts(monkeypatch):
    """bulk=True calls the worker once with the id list and pushes its
    (processed, failed, skipped) tuple — the FYI atomic-UPDATE path."""

    pushes = []
    received = []

    def noop_audit(*a, **k):
        return None

    async def capture_push(*a, **k):
        pushes.append((a, k))

    async def bulk_worker(ids_list):
        received.append(ids_list)
        return 3, 1, 2

    monkeypatch.setattr("api.index.audit_log_sync", noop_audit)
    monkeypatch.setattr("api.index.send_push_notification", capture_push)

    await _run_batch_job("fyi", [1, 2, 3, 4, 5, 6], "acknowledge", bulk_worker, bulk=True)
    await asyncio.sleep(0.1)

    assert received == [[1, 2, 3, 4, 5, 6]]
    assert len(pushes) == 1
    data = pushes[0][1]["data"]
    assert data["type"] == "batch_done"
    assert data["processed"] == "3" and data["skipped"] == "2" and data["failed"] == "1"
