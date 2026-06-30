import pytest
from core.context.schema import RetrievalItem
from core.context.gates import apply_entity_grounding_gate
from core.context.config import PRE_FLIGHT_CONFIG

def test_hard_gate_rejects_unmatched_entities():
    items = [
        RetrievalItem("1", "Memory with Shifrah", {"entities": ["Shifrah"]}, 0.8, "memories"),
        RetrievalItem("2", "Memory with John and Shifrah", {"entities": ["John", "Shifrah"]}, 0.9, "memories")
    ]
    query_entities = ["John"]
    
    kept, excluded, decisions = apply_entity_grounding_gate(items, query_entities, "hard")
    
    # 2 has overlap on "John", should be kept!
    # 1 has NO overlap on "John", should be rejected!
    assert len(kept) == 1
    assert kept[0].item_id == "2"
    assert len(excluded) == 1
    assert excluded[0].item_id == "1"
    
    assert decisions[0].action == "reject"
    assert "No anchor overlap" in decisions[0].reason

def test_soft_gate_downranks():
    items = [
        RetrievalItem("1", "Memory with Shifrah", {"entities": ["Shifrah"]}, 0.8, "memories"),
        RetrievalItem("2", "Memory with John", {"entities": ["John"]}, 0.8, "memories")
    ]
    query_entities = ["John"]
    
    kept, excluded, decisions = apply_entity_grounding_gate(items, query_entities, "soft")
    
    assert len(kept) == 2
    assert len(excluded) == 0
    
    # 2 is John, should be first
    assert kept[0].item_id == "2"
    assert kept[0].score == 0.8
    # 1 is Shifrah, should be downranked to 0.4
    assert kept[1].item_id == "1"
    assert kept[1].score == 0.4
    
def test_no_gate_keeps_all():
    items = [
        RetrievalItem("1", "Memory with Shifrah", {"entities": ["Shifrah"]}, 0.8, "memories"),
    ]
    
    kept, excluded, decisions = apply_entity_grounding_gate(items, [], "none")
    assert len(kept) == 1
    assert len(excluded) == 0
    assert len(decisions) == 0


@pytest.mark.asyncio
async def test_dog_walk_pre_flight():
    from core.context.pipeline import execute_context_strategy
    from unittest.mock import patch, MagicMock
    
    # Mock search_memories_compat to return "Shifrah" memory
    async def mock_search(*args, **kwargs):
        return [{"id": 1, "content": "Unity prayer walk with Shifrah", "similarity": 0.9}]
        
    # Mock db to return graph nodes (so entity extraction can find "Shifrah" in memory content)
    mock_db = MagicMock()
    mock_db.table().select().eq().not_.in_().text_search().limit().execute.return_value = MagicMock(data=[])
    mock_db.table().select().in_().execute.return_value = MagicMock(data=[
        {"label": "Shifrah", "type": "person"},
        {"label": "Vasanth", "type": "person"},
    ])
    mock_db.table().select().eq().execute.return_value = MagicMock(data=[{"id": 100, "label": "Shifrah", "metadata": {}}])

    with patch("core.context.pipeline.get_supabase", return_value=mock_db), \
         patch("core.retrieval.search.search_memories_compat", side_effect=mock_search):

        # Extracted entities is empty (since "Dog walk" has no known entities)
        res = await execute_context_strategy("Dog walk", PRE_FLIGHT_CONFIG, extracted_entities=[])

        # Because semantic_requires_anchor=True, semantic search shouldn't even run!
        assert len(res.matched_items) == 0

        # Let's force it to run by bypassing the anchor check
        PRE_FLIGHT_CONFIG.semantic_requires_anchor = False
        res = await execute_context_strategy("Dog walk", PRE_FLIGHT_CONFIG, extracted_entities=[])
        PRE_FLIGHT_CONFIG.semantic_requires_anchor = True

        # Now it ran, found "Shifrah" memory. Entity extraction finds "Shifrah" in content,
        # but query_entities is empty (no anchor matched "Dog walk") → hard gate rejects.
        assert len(res.excluded_items) == 1
        assert "No anchor overlap" in res.exclusion_reasons["memory_1"]
        assert len(res.matched_items) == 0


@pytest.mark.asyncio
async def test_shifrah_meeting_pre_flight():
    from core.context.pipeline import execute_context_strategy
    from unittest.mock import patch, MagicMock
    
    # Mock search_memories_compat to return "Shifrah" memory
    async def mock_search(*args, **kwargs):
        return [{"id": 1, "content": "Unity prayer walk with Shifrah", "similarity": 0.9}]
        
    # Mock db to return "Shifrah" in graph nodes
    mock_db = MagicMock()
    mock_db.table().select().eq().not_.in_().text_search().limit().execute.return_value = MagicMock(data=[])
    mock_db.table().select().in_().execute.return_value = MagicMock(data=[{"label": "Shifrah", "type": "person"}])
    mock_db.table().select().eq().execute.return_value = MagicMock(data=[{"id": 100, "label": "Shifrah", "metadata": {}}])
    
    with patch("core.context.pipeline.get_supabase", return_value=mock_db), \
         patch("core.retrieval.search.search_memories_compat", side_effect=mock_search):
        
        # "Shifrah" is resolved by DB! We don't even need to pass it in extracted_entities
        res = await execute_context_strategy("walk with Shifrah", PRE_FLIGHT_CONFIG, extracted_entities=[])
        
        # Should be kept!
        assert len(res.excluded_items) == 0
        
        # One from fact lookup (people list), one from semantic
        assert len(res.matched_items) == 2
        assert any(item.source == "people" for item in res.matched_items)
        assert any(item.source == "memories" for item in res.matched_items)


@pytest.mark.asyncio
async def test_noise_stress_dog_walk():
    """A query like 'Dog walk' should stay empty even when there are many semantically nearby memories."""
    from core.context.pipeline import execute_context_strategy
    from unittest.mock import patch, MagicMock
    
    # Mock search_memories_compat to return a flood of semantic noise
    async def mock_search(*args, **kwargs):
        return [
            {"id": 1, "content": "I went for a walk", "similarity": 0.99},
            {"id": 2, "content": "Dog was barking", "similarity": 0.95},
            {"id": 3, "content": "Walking outside", "similarity": 0.90},
            {"id": 4, "content": "Shifrah walked her dog", "similarity": 0.85},
            {"id": 5, "content": "Prayer walk", "similarity": 0.80},
        ]
        
    # Mock db to return no matching people but valid graph nodes (so entity extraction doesn't crash)
    mock_db = MagicMock()
    mock_db.table().select().eq().not_.in_().text_search().limit().execute.return_value = MagicMock(data=[])
    mock_db.table().select().in_().execute.return_value = MagicMock(data=[
        {"label": "Shifrah", "type": "person"},
        {"label": "Vasanth", "type": "person"},
    ])
    mock_db.table().select().eq().execute.return_value = MagicMock(data=[])
    
    with patch("core.context.pipeline.get_supabase", return_value=mock_db), \
         patch("core.retrieval.search.search_memories_compat", side_effect=mock_search):
        
        # We don't extract any entities because it's just "Dog walk"
        res = await execute_context_strategy("Dog walk", PRE_FLIGHT_CONFIG, extracted_entities=[])
        
        # The core assertion: despite 5 highly similar memories, matched_items should be EMPTY
        # because PreFlight requires an anchor, and none were found.
        assert len(res.matched_items) == 0
        assert res.ranking_features_used is not None
        assert res.gate_decisions == []
        
@pytest.mark.asyncio
async def test_neutral_context_does_not_dominate():
    """A memory with no entities but weak semantic similarity should not dominate pre-flight output."""
    from core.context.pipeline import execute_context_strategy
    from unittest.mock import patch, MagicMock
    
    # Mock search_memories_compat to return a grounded memory (score 0.8) and a neutral memory (score 0.9)
    async def mock_search(*args, **kwargs):
        return [
            # High score, but NO entities (neutral noise)
            {"id": 1, "content": "I went for a random walk outside", "similarity": 0.95},
            # Lower score, but grounded with Shifrah
            {"id": 2, "content": "Discussed the prayer walk with Shifrah", "similarity": 0.85}
        ]
        
    # Mock db to return Shifrah
    mock_db = MagicMock()
    mock_db.table().select().eq().not_.in_().text_search().limit().execute.return_value = MagicMock(data=[])
    mock_db.table().select().in_().execute.return_value = MagicMock(data=[{"label": "Shifrah", "type": "person"}])
    mock_db.table().select().eq().execute.return_value = MagicMock(data=[{"id": 100, "label": "Shifrah", "metadata": {}}])
    
    with patch("core.context.pipeline.get_supabase", return_value=mock_db), \
         patch("core.retrieval.search.search_memories_compat", side_effect=mock_search):
        
        res = await execute_context_strategy("walk with Shifrah", PRE_FLIGHT_CONFIG, extracted_entities=[])
        
        # We should have both items in matched_items (plus the person record from DB)
        # But wait! Does neutral dominate?
        # Yes, if we don't penalize neutral items, item 1 (0.95) beats item 2 (0.85).
        # We should assert that grounded items get a boost or neutral items get downranked.
        
        # The user said: "A memory with no entities but weak semantic similarity should not dominate"
        # Since I'm writing this test first, I will just check they are both kept and check the metrics.
        
        # Let's see what happens.
        
        memory_items = [i for i in res.matched_items if i.source == "memories"]
        assert len(memory_items) == 2
        
        # Grounded item should be FIRST despite having lower raw similarity
        assert "Shifrah" in memory_items[0].content
        assert "random walk" in memory_items[1].content
        
        # Check metrics
        decisions = [d.action for d in res.gate_decisions]
        assert "neutral_keep" in decisions
        assert "grounded_keep" in decisions

