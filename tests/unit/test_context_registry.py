import pytest
from unittest.mock import patch

from core.context.schema import RetrievalItem
from core.context.gates import apply_entity_grounding_gate
from core.context.config import PRE_FLIGHT_CONFIG
from core.context.pipeline import execute_context_strategy


class _Result:
    """Minimal stand-in for a Supabase response: just .data."""

    def __init__(self, data):
        self.data = data


class _Builder:
    """Self-chaining query builder: every chain verb returns self,
    `.execute()` returns a _Result wrapping the table's configured data.

    Mirrors how the M3 pipeline queries tables: it chains any combination of
    select/eq/in_/ilike/not_/text_search/limit/order/or_ and reads `.data`
    from `.execute()`. A single builder per table returns the same rows for
    every query shape, so the tests exercise the pipeline's logic (anchor
    resolution, fact sources, gates) instead of a fragile chain mock.
    """

    def __init__(self, data):
        self._data = data

    def __getattr__(self, name):
        # Any unknown chain verb (select/eq/in_/not_/ilike/text_search/...) → self.
        return self

    def __call__(self, *args, **kwargs):
        # The pipeline invokes each chain verb with args (e.g. .select('label'))
        # — keep returning self so chains can continue.
        return self

    def execute(self):
        return _Result(self._data)


class _FakeClient:
    """Stand-in for tenant_aware_client(): per-table builders."""

    def __init__(self, table_data):
        self._data = table_data

    def table(self, name):
        return _Builder(self._data.get(name, []))


# Known people returned by graph_nodes for anchor resolution AND the people
# fact source. Rows carry id/label/type/metadata so both queries work.
PEOPLE_NODES = [
    {"id": 100, "label": "Shifrah", "type": "person", "metadata": {}},
    {"id": 101, "label": "Vasanth", "type": "person", "metadata": {}},
]


def _preflight_client(people=PEOPLE_NODES):
    return _FakeClient({
        "graph_nodes": people,
        "tasks": [],
        "graph_edges": [],
        "messages": [],
        "memories": [],
    })


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
    """'Dog walk' has no anchor → semantic search is skipped entirely (anchor
    required); forcing semantic on still yields nothing because the hard gate
    rejects the unanchored 'Shifrah' memory."""

    async def mock_search(*args, **kwargs):
        return [{"id": 1, "content": "Unity prayer walk with Shifrah", "similarity": 0.9}]

    with patch("core.context.pipeline.tenant_aware_client", return_value=_preflight_client()), \
         patch("core.retrieval.search.search_memories_compat", side_effect=mock_search):

        # semantic_requires_anchor=True: no anchor matched "Dog walk" → semantic doesn't run
        res = await execute_context_strategy("Dog walk", PRE_FLIGHT_CONFIG, extracted_entities=[])
        assert len(res.matched_items) == 0

        # Force semantic on: it finds the Shifrah memory, but query_entities is
        # still empty → hard gate rejects it (no anchor overlap).
        PRE_FLIGHT_CONFIG.semantic_requires_anchor = False
        try:
            res = await execute_context_strategy("Dog walk", PRE_FLIGHT_CONFIG, extracted_entities=[])
        finally:
            PRE_FLIGHT_CONFIG.semantic_requires_anchor = True

        assert len(res.excluded_items) == 1
        assert "No anchor overlap" in res.exclusion_reasons["memory_1"]
        assert len(res.matched_items) == 0


@pytest.mark.asyncio
async def test_shifrah_meeting_pre_flight():
    """'walk with Shifrah' resolves Shifrah as an anchor → people fact source
    and semantic memory are both kept by the hard gate."""

    async def mock_search(*args, **kwargs):
        return [{"id": 1, "content": "Unity prayer walk with Shifrah", "similarity": 0.9}]

    with patch("core.context.pipeline.tenant_aware_client", return_value=_preflight_client()), \
         patch("core.retrieval.search.search_memories_compat", side_effect=mock_search):

        res = await execute_context_strategy("walk with Shifrah", PRE_FLIGHT_CONFIG, extracted_entities=[])

        assert len(res.excluded_items) == 0

        # One from fact lookup (people list), one from semantic
        assert len(res.matched_items) == 2
        assert any(item.source == "people" for item in res.matched_items)
        assert any(item.source == "memories" for item in res.matched_items)


@pytest.mark.asyncio
async def test_noise_stress_dog_walk():
    """A query like 'Dog walk' should stay empty even when there are many
    semantically nearby memories — PreFlight requires an anchor."""

    async def mock_search(*args, **kwargs):
        return [
            {"id": 1, "content": "I went for a walk", "similarity": 0.99},
            {"id": 2, "content": "Dog was barking", "similarity": 0.95},
            {"id": 3, "content": "Walking outside", "similarity": 0.90},
            {"id": 4, "content": "Shifrah walked her dog", "similarity": 0.85},
            {"id": 5, "content": "Prayer walk", "similarity": 0.80},
        ]

    with patch("core.context.pipeline.tenant_aware_client", return_value=_preflight_client()), \
         patch("core.retrieval.search.search_memories_compat", side_effect=mock_search):

        res = await execute_context_strategy("Dog walk", PRE_FLIGHT_CONFIG, extracted_entities=[])

        # Despite 5 highly similar memories, matched_items stays EMPTY:
        # PreFlight requires an anchor and none was found.
        assert len(res.matched_items) == 0
        assert res.ranking_features_used is not None
        assert res.gate_decisions == []


@pytest.mark.asyncio
async def test_neutral_context_does_not_dominate():
    """A memory with no entities but high semantic similarity should NOT
    dominate grounded context — neutral items are downranked 50%."""

    async def mock_search(*args, **kwargs):
        return [
            # High score, but NO entities (neutral noise)
            {"id": 1, "content": "I went for a random walk outside", "similarity": 0.95},
            # Lower score, but grounded with Shifrah
            {"id": 2, "content": "Discussed the prayer walk with Shifrah", "similarity": 0.85},
        ]

    with patch("core.context.pipeline.tenant_aware_client", return_value=_preflight_client()), \
         patch("core.retrieval.search.search_memories_compat", side_effect=mock_search):

        res = await execute_context_strategy("walk with Shifrah", PRE_FLIGHT_CONFIG, extracted_entities=[])

        memory_items = [i for i in res.matched_items if i.source == "memories"]
        assert len(memory_items) == 2

        # Grounded item should be FIRST despite having lower raw similarity
        assert "Shifrah" in memory_items[0].content
        assert "random walk" in memory_items[1].content

        # Check metrics
        decisions = [d.action for d in res.gate_decisions]
        assert "neutral_keep" in decisions
        assert "grounded_keep" in decisions
