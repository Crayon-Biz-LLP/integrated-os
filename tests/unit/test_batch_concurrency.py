"""Unit tests for _run_batch_concurrently — the shared helper behind every
/api/*-action/batch route.

Proves the two properties that fix the batch approve/reject slowness and
failure: (1) items run in parallel (bounded), not serially, and (2) a failing
item is counted as failed while the rest still process (fail-closed per item).
"""
import asyncio

import pytest

from api.index import _run_batch_concurrently


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
    processed, failed = await _run_batch_concurrently(
        [1, 2, 3, 4], _worker_ok,
    )
    assert processed == 4
    assert failed == 0


@pytest.mark.asyncio
async def test_failing_items_count_failed_rest_proceed():
    async def worker(item_id):
        if item_id in (2, 3):
            raise RuntimeError(f"boom {item_id}")
        await asyncio.sleep(0)

    processed, failed = await _run_batch_concurrently(
        [1, 2, 3, 4], worker,
    )
    assert processed == 2
    assert failed == 2


@pytest.mark.asyncio
async def test_all_fail():
    processed, failed = await _run_batch_concurrently(
        [1, 2], _worker_failing,
    )
    assert processed == 0
    assert failed == 2


@pytest.mark.asyncio
async def test_parallelism_bounded_runs_faster_than_serial():
    """5 × 100ms items at concurrency 3 should take ~200ms, not 500ms —
    proving the helper actually parallelizes (the batch slowness fix)."""
    start = asyncio.get_event_loop().time()
    processed, failed = await _run_batch_concurrently(
        [1, 2, 3, 4, 5], _worker_sleepy, concurrency=3,
    )
    elapsed = asyncio.get_event_loop().time() - start
    assert processed == 5
    assert failed == 0
    # 5 items / 3-wide = 2 waves minimum; 100ms each → < 300ms. Serial
    # would be ≥ 500ms. Give a little clock slack for CI.
    assert elapsed < 0.45, f"batch took {elapsed:.3f}s — looks serial"


@pytest.mark.asyncio
async def test_empty_ids_returns_zero():
    processed, failed = await _run_batch_concurrently([], _worker_ok)
    assert processed == 0
    assert failed == 0
