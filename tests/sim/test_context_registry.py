import pytest
import os
from unittest.mock import patch, MagicMock
from core.context import execute_context_strategy, PRE_FLIGHT_CONFIG, HYDRATE_MEMORIES_CONFIG
from core.context.schema import RetrievalItem
from core.context.gates import apply_entity_grounding_gate

skip_unless_live_db = pytest.mark.skipif(
    os.getenv("LIVE_DB") != "true",
    reason="Requires LIVE_DB=true (real Supabase)"
)


# ── Group 1a: Anchor Resolution & Entity Grounding ────────────────────

@skip_unless_live_db
@pytest.mark.asyncio
async def test_dog_walk_returns_empty(seed_test_data):
    """T1 — 'Dog walk' → empty pre-flight context (original bug regression)."""
    res = await execute_context_strategy("Dog walk", PRE_FLIGHT_CONFIG, extracted_entities=[])
    assert len(res.matched_items) == 0
    # semantic_skipped_no_anchor is an audit detail, not on ContextResult yet.
    # Instead, verify no semantic items leaked in.
    memory_items = [i for i in res.matched_items if i.source == "memories"]
    assert len(memory_items) == 0


@skip_unless_live_db
@pytest.mark.asyncio
async def test_walk_with_shifrah_returns_grounded(seed_test_data):
    """T2 — 'walk with Shifrah' → grounded memory keeps."""
    res = await execute_context_strategy("walk with Shifrah", PRE_FLIGHT_CONFIG, extracted_entities=[])
    # Should have at least a people match and/or memory
    assert len(res.matched_items) >= 1
    # At least one memory item should mention Shifrah
    memory_items = [i for i in res.matched_items if i.source == "memories"]
    if memory_items:
        assert any("Shifrah" in i.content for i in memory_items)
    # No items should be excluded
    assert len(res.excluded_items) == 0


@skip_unless_live_db
@pytest.mark.asyncio
async def test_anchor_lookup_failure_degrades_safely(seed_test_data):
    """T3 — DB anchor lookup failure → safe empty, not crash or noisy context."""
    async def broken_strategy(*a, **kw):
        raise Exception("Simulated DB timeout")

    with patch("core.context.pipeline.execute_context_strategy", side_effect=broken_strategy):
        try:
            res = await execute_context_strategy("walk with Shifrah", PRE_FLIGHT_CONFIG, extracted_entities=[])
        except Exception:
            # The pipeline itself may throw; the caller (fetch_event_context) wraps in try/except
            pass

    # The real test: simulate a graph_nodes query failure by injecting a mock that throws
    mock_db = MagicMock()
    mock_db.table().select().in_().execute.side_effect = Exception("DB timeout")

    with patch("core.context.pipeline.get_supabase", return_value=mock_db), \
         patch("core.retrieval.search.search_memories_compat") as mock_sem:
        mock_sem.return_value = []

        res = await execute_context_strategy("walk with Shifrah", PRE_FLIGHT_CONFIG, extracted_entities=[])
        # Should fail-closed: no crash, no noisy data
        assert len(res.matched_items) == 0
        # Pipeline did not crash
        assert res is not None


@skip_unless_live_db
@pytest.mark.asyncio
async def test_stale_anchor_returns_empty(seed_test_data):
    """T4 — DB returns data without matching anchor → safe empty."""
    mock_db = MagicMock()
    # graph_nodes query returns data but NOT "Shifrah"
    mock_db.table().select().in_().execute.return_value = MagicMock(data=[
        {"label": "SomeoneElse", "type": "person"}
    ])
    mock_db.table().select().eq().not_.in_().text_search().limit().execute.return_value = MagicMock(data=[])
    mock_db.table().select().eq().execute.return_value = MagicMock(data=[])

    with patch("core.context.pipeline.get_supabase", return_value=mock_db), \
         patch("core.retrieval.search.search_memories_compat") as mock_sem:
        mock_sem.return_value = []

        res = await execute_context_strategy("walk with Shifrah", PRE_FLIGHT_CONFIG, extracted_entities=[])
        assert len(res.matched_items) == 0
        assert len(res.excluded_items) == 0
        # No phantom Shifrah context
        for item in res.matched_items:
            assert "Shifrah" not in item.content


# ── Group 1b: Grounded vs Neutral Ranking ─────────────────────────────

def test_grounded_outranks_neutral():
    """T5 — Grounded item outranks neutral. Neutral still survives with no false anchors."""
    items = [
        RetrievalItem("mem_2", "I went for a walk", {"entities": []}, 0.95, "memories"),
        RetrievalItem("mem_1", "Discussed walk with Shifrah", {"entities": ["Shifrah"]}, 0.80, "memories"),
    ]
    query_entities = ["Shifrah"]

    kept, excluded, decisions = apply_entity_grounding_gate(items, query_entities, "hard")

    # Grounded (Shifrah) should be first, even though its raw score is lower
    assert len(kept) == 2
    assert kept[0].item_id == "mem_1", "Grounded item should outrank neutral"
    assert "Shifrah" in kept[0].content
    assert kept[1].item_id == "mem_2", "Neutral item should still survive"

    # Gate decisions reflect the right branches
    grounded_decisions = [d for d in decisions if d.action == "grounded_keep"]
    neutral_decisions = [d for d in decisions if d.action == "neutral_keep"]
    assert len(grounded_decisions) == 1
    assert len(neutral_decisions) == 1

    # Neutral item's score is halved (downranked), grounded item's score is unchanged
    assert kept[1].score == pytest.approx(0.95 * 0.5)
    assert kept[0].score == 0.80

    # Excluded should be empty — both survive
    assert len(excluded) == 0


def test_neutral_survives_when_no_grounded_alternative():
    """T6 — No grounded alternative → neutral alone survives."""
    items = [
        RetrievalItem("mem_1", "I went for a walk in the park", {"entities": []}, 0.90, "memories"),
    ]
    query_entities = ["Alpha"]

    kept, excluded, decisions = apply_entity_grounding_gate(items, query_entities, "hard")

    assert len(kept) == 1
    assert kept[0].item_id == "mem_1"
    # Score should be halved but item still present
    assert kept[0].score == pytest.approx(0.90 * 0.5)
    decisions_actions = [d.action for d in decisions]
    assert "neutral_keep" in decisions_actions

    # Excluded should be empty
    assert len(excluded) == 0
    # No false anchors introduced
    assert "Shifrah" not in kept[0].content
    assert "Alpha" not in kept[0].content


# ── Group 1c: Hard Gate vs Soft Gate ───────────────────────────────────

def test_hard_gate_rejects_cross_entity_leak():
    """T7 — Hard gate: cross-entity memory is excluded, not just downranked."""
    items = [
        RetrievalItem("mem_1", "Vasanth approved the budget", {"entities": ["Vasanth"]}, 0.90, "memories"),
    ]
    query_entities = ["Shifrah"]

    kept, excluded, decisions = apply_entity_grounding_gate(items, query_entities, "hard")

    # Should be excluded — no entity overlap with query
    assert len(kept) == 0
    assert len(excluded) == 1
    assert excluded[0].item_id == "mem_1"

    # exclusion reason should reflect no anchor overlap
    rejection = [d for d in decisions if d.action == "reject"]
    assert len(rejection) == 1
    assert "No anchor overlap" in rejection[0].reason

    # Query entities are among the entities
    assert "Vasanth" in items[0].metadata.get("entities", [])


def test_soft_gate_downranks_cross_entity_leak():
    """T8 — Soft gate: cross-entity memory is downranked, not excluded."""
    items = [
        RetrievalItem("mem_1", "Vasanth approved the budget", {"entities": ["Vasanth"]}, 0.90, "memories"),
    ]
    query_entities = ["Shifrah"]

    kept, excluded, decisions = apply_entity_grounding_gate(items, query_entities, "soft")

    # Should be kept but downranked
    assert len(kept) == 1
    assert len(excluded) == 0
    assert kept[0].item_id == "mem_1"
    assert kept[0].score == pytest.approx(0.90 * 0.5)

    downranked = [d for d in decisions if d.action == "downrank"]
    assert len(downranked) == 1
    assert "No anchor overlap" in downranked[0].reason
