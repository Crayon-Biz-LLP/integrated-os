import pytest
import os
import asyncio
from unittest.mock import patch, MagicMock
from core.context import execute_context_strategy, PRE_FLIGHT_CONFIG

skip_unless_live_db = pytest.mark.skipif(
    os.getenv("LIVE_DB") != "true",
    reason="Requires LIVE_DB=true (real Supabase)"
)


# ── Fix A: PRE_FLIGHT routing ──────────────────────────────────────────


@skip_unless_live_db
@pytest.mark.asyncio
async def test_preflight_calls_legacy_path(seed_test_data):
    """P1 — Fix A: PRE_FLIGHT calls search_memories_compat with use_associative=False.

    This asserts the routing decision itself: the sentinel's fetch_event_context
    bypasses the associative retrieval index and uses the legacy pgvector path
    so that newly created (unindexed) memories are still findable.
    """
    with patch("core.retrieval.search.search_memories_compat") as mock_search:
        mock_search.return_value = []

        await execute_context_strategy(
            "Alpha project",
            PRE_FLIGHT_CONFIG,
            extracted_entities=["Alpha"],
        )

        # Called exactly once with the right signature
        mock_search.assert_called_once()
        _, kwargs = mock_search.call_args
        assert kwargs.get("use_associative") is False, (
            f"Expected use_associative=False for PRE_FLIGHT, got {kwargs.get('use_associative')}"
        )
        assert kwargs.get("top_k") == 12
        assert kwargs.get("threshold") == 0.55


# ── Fix D: Entity extraction ──────────────────────────────────────────


def test_entity_extraction_uses_graph_labels_not_regex():
    """P2 — Fix D: entities are derived from known graph node labels, not regex.

    The old regex \b[A-Z][a-z]+\b extracted any capitalized word (e.g. "Quick",
    "Friday", "So", "But") as entities even when they weren't people, orgs, or
    projects. The new approach matches memory content against known graph node
    labels from the anchor-resolution query — only those are kept as entities.
    """

    async def _run():
        mock_db = MagicMock()

        # Anchor resolution returns realistic graph node labels
        mock_db.table().select().in_().execute.return_value = MagicMock(data=[
            {"label": "Shifrah", "type": "person"},
            {"label": "Vasanth", "type": "person"},
            {"label": "Alpha", "type": "project"},
            {"label": "Armour Cyber", "type": "organization"},
        ])

        # Fact sources return nothing
        mock_db.table().select().eq().not_.in_().text_search().limit().execute.return_value = MagicMock(data=[])
        mock_db.table().select().eq().execute.return_value = MagicMock(data=[])

        memory_content = (
            "So I had a meeting with Shifrah about the Alpha project. "
            "But Vasanth also joined from Armour Cyber. "
            "Quick update: The deadline is next Friday."
        )

        with patch("core.context.pipeline.get_supabase", return_value=mock_db), \
             patch("core.retrieval.search.search_memories_compat") as mock_search:
            mock_search.return_value = [{
                "id": 999002,
                "content": memory_content,
                "memory_type": "note",
                "similarity": 0.85,
                "created_at": "2026-06-30T00:00:00+00:00",
            }]

            res = await execute_context_strategy(
                "meeting with Shifrah",
                PRE_FLIGHT_CONFIG,
                extracted_entities=["Shifrah"],
            )

            memory_items = [i for i in res.matched_items if i.source == "memories"]
            assert len(memory_items) == 1

            entities = memory_items[0].metadata.get("entities", [])
            entities_str = ", ".join(entities)

            # Valid graph labels in content ARE extracted
            assert "Shifrah" in entities, f"Expected Shifrah in [{entities_str}]"
            assert "Vasanth" in entities, f"Expected Vasanth in [{entities_str}]"
            assert "Alpha" in entities, f"Expected Alpha in [{entities_str}]"
            assert "Armour Cyber" in entities, f"Expected Armour Cyber in [{entities_str}]"

            # Regex-trap tokens that match \b[A-Z][a-z]+\b with len>3 are NOT extracted
            assert "Quick" not in entities, f"'Quick' should NOT be in entities: [{entities_str}]"
            assert "Friday" not in entities, f"'Friday' should NOT be in entities: [{entities_str}]"
            # Short stop-words ("So", "But") have len<=3 so the old regex already excluded
            # them; verifying they're still not present is a safety check
            assert "So" not in entities, f"'So' should NOT be in entities: [{entities_str}]"
            assert "But" not in entities, f"'But' should NOT be in entities: [{entities_str}]"

            # The hard gate should keep this memory (Shifrah in query_entities and entities overlap)
            assert len(res.excluded_items) == 0

    asyncio.run(_run())
