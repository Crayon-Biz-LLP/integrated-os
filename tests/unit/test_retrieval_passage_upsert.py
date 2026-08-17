"""Unit tests for the retrieval passage atomic upsert (core/retrieval/pipeline.py).

Pins the Aug-17 fix: `_upsert_passage` must use a DB-arbitrated upsert
(on_conflict + ignore_duplicates) instead of the racy read-then-insert that
threw duplicate-key violations when backfill and live indexing ran
concurrently. On conflict it must return the existing id (idempotent).
"""
from unittest.mock import AsyncMock, MagicMock

import pytest

from core.retrieval.pipeline import _upsert_passage
from core.retrieval.schema import Passage

pytestmark = pytest.mark.retrieval


def _fluent_chain(*execute_data):
    """A fluent table chain: every verb returns itself; execute() pops the
    next canned result. NOTE the shapes must mirror supabase-py: maybe_single
    reads return a single row DICT (or None); insert/upsert return a LIST."""
    m = MagicMock()
    m.execute.side_effect = [MagicMock(data=d) for d in execute_data]
    for meth in ("select", "eq", "order", "limit", "maybe_single", "upsert", "insert"):
        getattr(m, meth).return_value = m
    return m


def _passage(**over):
    fields = dict(
        source_type="relationship_note",
        source_id="5030",
        memory_id=5030,
        passage_index=0,
        text="Q3 roadmap discussion",
        source_fingerprint="fp-5030-0",
        index_version=1,
    )
    fields.update(over)
    return Passage(**fields)


@pytest.fixture(autouse=True)
def _mock_embedding():
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            "core.retrieval.pipeline.get_embedding",
            AsyncMock(return_value=MagicMock(vector=[0.1] * 4)),
        )
        yield


@pytest.mark.asyncio
async def test_new_passage_upserts_atomically(monkeypatch):
    """A fresh passage inserts via upsert with the owner-scoped conflict
    target + ignore_duplicates — never a bare insert."""
    chain = _fluent_chain(None, [{"id": 42}])  # read-first: none; upsert: inserted
    supabase = MagicMock()
    supabase.table.return_value = chain
    monkeypatch.setattr("core.retrieval.pipeline.supabase", supabase)

    pid = await _upsert_passage(_passage())

    assert pid == 42
    chain.upsert.assert_called_once()
    args, kwargs = chain.upsert.call_args
    assert kwargs["on_conflict"] == "owner_id,source_fingerprint,passage_index,index_version"
    assert kwargs["ignore_duplicates"] is True
    assert args[0]["source_fingerprint"] == "fp-5030-0"
    # No bare insert anywhere in the path.
    chain.insert.assert_not_called()


@pytest.mark.asyncio
async def test_existing_passage_returns_existing_id(monkeypatch):
    """Already-indexed passage: read-first fast path returns the id, no write."""
    chain = _fluent_chain({"id": 7})  # read-first finds it (maybe_single → dict)
    supabase = MagicMock()
    supabase.table.return_value = chain
    monkeypatch.setattr("core.retrieval.pipeline.supabase", supabase)

    pid = await _upsert_passage(_passage())

    assert pid == 7
    chain.upsert.assert_not_called()


@pytest.mark.asyncio
async def test_conflict_returns_winner_id(monkeypatch):
    """Race: upsert is a no-op (conflict) → fallback fetch returns the winner's
    id — idempotent outcome instead of the old duplicate-key ERROR."""
    # read-first: none → upsert: conflict (empty list) → fallback read: winner id
    chain = _fluent_chain(None, [], {"id": 99})
    supabase = MagicMock()
    supabase.table.return_value = chain
    monkeypatch.setattr("core.retrieval.pipeline.supabase", supabase)

    pid = await _upsert_passage(_passage())

    assert pid == 99
    chain.upsert.assert_called_once()
