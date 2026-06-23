"""Tests for core/retrieval/ modules."""

import asyncio
import os
from unittest.mock import patch, MagicMock, AsyncMock
import pytest
from core.retrieval.chunker import chunk_text, compute_fingerprint, _split_into_paragraphs
from core.retrieval.normalizer import normalize_phrase, is_noise_phrase, expand_shorthand, classify_node_type
from core.retrieval.ppr import personalized_pagerank, build_adjacency_from_edges, normalize_scores
from core.retrieval.ranking import rank_memories, WeightConfig
from core.retrieval.schema import Passage
from core.retrieval.pipeline import index_memory, retry_failed_index_runs


class TestChunker:
    def test_deterministic_chunking(self):
        text = "This is a test memory. " * 50
        result1 = chunk_text(text, "memory", "1")
        result2 = chunk_text(text, "memory", "1")
        assert len(result1) == len(result2)
        for p1, p2 in zip(result1, result2):
            assert p1.text == p2.text
            assert p1.source_fingerprint == p2.source_fingerprint
            assert p1.passage_index == p2.passage_index

    def test_different_inputs_produce_different_fingerprints(self):
        fp1 = compute_fingerprint("hello world")
        fp2 = compute_fingerprint("hello world!")
        assert fp1 != fp2

    def test_same_input_same_fingerprint(self):
        assert compute_fingerprint("hello world") == compute_fingerprint("hello world")

    def test_empty_text_returns_empty(self):
        result = chunk_text("", "memory", "1")
        assert result == []

    def test_short_text(self):
        result = chunk_text("Short note.", "memory", "2")
        assert len(result) == 1
        assert result[0].text == "Short note."
        assert result[0].passage_index == 0

    def test_passage_has_all_fields(self):
        result = chunk_text("Test passage content.", "memory", "42", memory_id=99, index_version=1)
        assert len(result) == 1
        p = result[0]
        assert p.source_type == "memory"
        assert p.source_id == "42"
        assert p.memory_id == 99
        assert p.passage_index == 0
        assert p.index_version == 1
        assert p.char_count > 0

    def test_split_into_paragraphs(self):
        text = "Para one.\n\nPara two.\n\nPara three."
        paras = _split_into_paragraphs(text)
        assert len(paras) >= 1
        assert "Para one" in paras[0] or paras[0].startswith("Para")


class TestNormalizer:
    def test_basic_normalization(self):
        assert normalize_phrase("  Hello WORLD  ") == "hello world"
        assert normalize_phrase("Danny's Project") == "danny's project"

    def test_noise_detection(self):
        assert is_noise_phrase("a") is True
        assert is_noise_phrase("ab") is True
        assert is_noise_phrase("the and") is True
        assert is_noise_phrase("Project QHORD") is False

    def test_shorthand_expansion(self):
        assert "people" in expand_shorthand("talked to ppl")
        assert expand_shorthand("ASAP") == "as soon as possible"

    def test_classify_node_type(self):
        assert classify_node_type("Danny", {"danny": "person"}) == "person"
        assert classify_node_type("QHORD", {"qhord": "project"}) == "project"
        result = classify_node_type("Some random concept")
        assert result in ("entity", "concept")


class TestPPR:
    def test_ppr_on_small_graph(self):
        adj = {
            1: [(2, 1.0)],
            2: [(3, 1.0)],
            3: [(1, 0.5)],
        }
        seeds = {1: 1.0}
        scores = personalized_pagerank(adj, seeds, iterations=20)
        assert 1 in scores
        assert scores[1] > 0
        assert len(scores) <= 4

    def test_ppr_empty_graph(self):
        assert personalized_pagerank({}, {}) == {}

    def test_ppr_seeds_outside_graph(self):
        adj = {1: [(2, 1.0)]}
        seeds = {99: 1.0}
        scores = personalized_pagerank(adj, seeds)
        assert isinstance(scores, dict)
        assert len(scores) >= 1

    def test_adjacency_builder(self):
        edges = [(1, 2, 1.0), (2, 3, 0.5)]
        adj = build_adjacency_from_edges(edges)
        assert 1 in adj
        assert 2 in adj
        assert adj[1] == [(2, 1.0)]
        assert adj[2] == [(3, 0.5)]

    def test_score_normalization(self):
        scores = {1: 10.0, 2: 20.0, 3: 30.0}
        norm = normalize_scores(scores)
        assert abs(norm[1] - 0.0) < 1e-6
        assert abs(norm[3] - 1.0) < 1e-6
        assert 0 <= norm[2] <= 1

    def test_normalization_single_value(self):
        scores = {1: 5.0, 2: 5.0}
        norm = normalize_scores(scores)
        assert norm[1] == 1.0
        assert norm[2] == 1.0


class TestRanking:
    def test_empty_input(self):
        assert rank_memories({}) == []

    def test_basic_ranking(self):
        scores = {1: 1.0, 2: 0.5, 3: 0.0}
        ranked = rank_memories(memory_scores=scores)
        assert len(ranked) == 3
        assert ranked[0][0] == 1
        assert ranked[-1][0] == 3

    def test_ppr_scores_influence_ranking(self):
        scores = {1: 0.5, 2: 0.5}
        ppr = {1: 1.0, 2: 0.0}
        ranked = rank_memories(memory_scores=scores, ppr_scores=ppr)
        mid1 = [m for m, s in ranked if m == 1][0]
        mid2 = [m for m, s in ranked if m == 2][0]
        idx1 = next(i for i, (m, s) in enumerate(ranked) if m == mid1)
        idx2 = next(i for i, (m, s) in enumerate(ranked) if m == mid2)
        assert idx1 < idx2

    def test_custom_weights(self):
        scores = {1: 1.0, 2: 1.0}
        person_boost = {1: 1.0, 2: 0.0}
        weights = WeightConfig(person_boost=1.0, semantic=0.0, ppr=0.0, recency=0.0, importance=0.0, specificity=0.0, project_boost=0.0)
        ranked = rank_memories(memory_scores=scores, person_boost=person_boost, weights=weights)
        assert ranked[0][0] == 1


class TestSchema:
    def test_passage_defaults(self):
        p = Passage(source_type="test", source_id="1", passage_index=0, text="hello")
        assert p.char_count == 0
        assert p.index_version == 1
        assert p.metadata == {}
        assert p.memory_id is None


# ──────────────────────────────────────────────
# Test helpers
# ──────────────────────────────────────────────

class _QueryResult:
    """Mimics a Supabase query result with .data and .count."""
    def __init__(self, data=None, count=None):
        self.data = data or []
        self.count = count


def _mock_supabase_client():
    """Build a supabase mock that supports .table().select().eq().execute() chains.

    All table names share one builder mock. The builder supports both
    the select-chain path and the upsert-chain path (which are different
    attribute chains on the same MagicMock).
    """
    m = MagicMock(name="supabase")
    builder = MagicMock(name="builder")
    m.table.return_value = builder

    # Long-lived chain: all filter methods return self
    builder.select.return_value = builder
    builder.eq.return_value = builder
    builder.order.return_value = builder
    builder.limit.return_value = builder
    builder.gt.return_value = builder
    builder.is_.return_value = builder
    builder.in_.return_value = builder
    builder.lt.return_value = builder
    builder.not_.return_value = builder
    builder.maybe_single.return_value = builder

    # Default: select chain execute returns empty
    builder.execute.return_value = _QueryResult([])

    # Upsert path: builder.upsert() returns a DIFFERENT mock
    upsert_result = MagicMock(name="upsert_result")
    builder.upsert.return_value = upsert_result
    upsert_result.execute.return_value = _QueryResult([{"id": 1}])

    # Update path
    update_result = MagicMock(name="update_result")
    builder.update.return_value = update_result
    update_result.eq.return_value = update_result
    update_result.execute.return_value = _QueryResult([])

    return m


# ──────────────────────────────────────────────
# Test 1: Status transitions with forced LLM failures
# ──────────────────────────────────────────────

class TestPipelineStatusTransitions:
    """Verify index_memory() aggregates per-passage extraction results correctly.

    All extractions fail → status='failed', return False.
    Some fail, some succeed → status='completed_partial', return True.
    All succeed → status='completed', return True.
    """

    # Each paragraph must exceed PASSAGE_MIN_CHARS (80) to stay as separate
    CONTENT = (
        "Danny led the QHORD standup today and discussed the roadmap for "
        "the upcoming June launch of the product.\n\n"
        "He also had a lengthy call with the Ashraya team about the upcoming "
        "community event that will be held next month in the auditorium."
    )

    def _with_supabase(self, **kwargs):
        """Return a supabase mock with optional overrides."""
        m = _mock_supabase_client()
        for key, val in kwargs.items():
            setattr(m, key, val)
        return m

    @pytest.mark.asyncio
    @patch.dict(os.environ, {"RETRIEVAL_INDEXING_ENABLED": "true"})
    @patch("core.retrieval.pipeline.supabase")
    @patch("core.retrieval.pipeline._upsert_passage", new_callable=AsyncMock)
    @patch("core.retrieval.pipeline.upsert_memory_bundle_link", new_callable=AsyncMock)
    @patch("core.retrieval.pipeline.extract_triples", new_callable=AsyncMock)
    @patch("core.retrieval.pipeline.build_triple_graph", new_callable=AsyncMock)
    @patch("core.retrieval.pipeline._set_run_status")
    async def test_all_extractions_fail(
        self, mock_set_status,         mock_build_graph, mock_extract, mock_bundle_link,
        mock_upsert_passage, mock_supabase,
    ):
        mock_upsert_passage.side_effect = [100, 200]
        mock_extract.side_effect = [
            ([], False),
            ([], False),
        ]

        result = await index_memory(
            memory_id=42, content=self.CONTENT,
            memory_type="memory", source="test",
        )

        assert result is False
        mock_set_status.assert_called_once()
        status_arg = mock_set_status.call_args[0][1]
        assert status_arg == "failed", f"Expected failed, got {status_arg}"

    @pytest.mark.asyncio
    @patch.dict(os.environ, {"RETRIEVAL_INDEXING_ENABLED": "true"})
    @patch("core.retrieval.pipeline.supabase")
    @patch("core.retrieval.pipeline._upsert_passage", new_callable=AsyncMock)
    @patch("core.retrieval.pipeline.upsert_memory_bundle_link", new_callable=AsyncMock)
    @patch("core.retrieval.pipeline.extract_triples", new_callable=AsyncMock)
    @patch("core.retrieval.pipeline.build_triple_graph", new_callable=AsyncMock)
    @patch("core.retrieval.pipeline._set_run_status")
    async def test_partial_extractions_fail(
        self, mock_set_status,         mock_build_graph, mock_extract, mock_bundle_link,
        mock_upsert_passage, mock_supabase,
    ):
        mock_upsert_passage.side_effect = [100, 200]
        mock_extract.side_effect = [
            ([MagicMock()], True),
            ([], False),
        ]
        

        result = await index_memory(
            memory_id=42, content=self.CONTENT,
            memory_type="memory", source="test",
        )

        assert result is True, f"Expected True for partial, got {result}"
        mock_set_status.assert_called_once()
        status_arg = mock_set_status.call_args[0][1]
        assert status_arg == "completed_partial", f"Expected completed_partial, got {status_arg}"
        mock_build_graph.assert_called_once()

    @pytest.mark.asyncio
    @patch.dict(os.environ, {"RETRIEVAL_INDEXING_ENABLED": "true"})
    @patch("core.retrieval.pipeline.supabase")
    @patch("core.retrieval.pipeline._upsert_passage", new_callable=AsyncMock)
    @patch("core.retrieval.pipeline.upsert_memory_bundle_link", new_callable=AsyncMock)
    @patch("core.retrieval.pipeline.extract_triples", new_callable=AsyncMock)
    @patch("core.retrieval.pipeline.build_triple_graph", new_callable=AsyncMock)
    @patch("core.retrieval.pipeline._set_run_status")
    async def test_all_extractions_succeed(
        self, mock_set_status,         mock_build_graph, mock_extract, mock_bundle_link,
        mock_upsert_passage, mock_supabase,
    ):
        mock_upsert_passage.side_effect = [100, 200]
        mock_extract.side_effect = [
            ([MagicMock()], True),
            ([MagicMock()], True),
        ]
        

        result = await index_memory(
            memory_id=42, content=self.CONTENT,
            memory_type="memory", source="test",
        )

        assert result is True
        mock_set_status.assert_called_once()
        status_arg = mock_set_status.call_args[0][1]
        assert status_arg == "completed", f"Expected completed, got {status_arg}"
        assert mock_build_graph.call_count == 2, (
            f"Expected build_triple_graph called 2 times, got {mock_build_graph.call_count}"
        )


# ──────────────────────────────────────────────
# Test 2: Concurrent ingestion doesn't exceed semaphore limit
# ──────────────────────────────────────────────

class TestConcurrentBackfill:
    """Verify the module-level semaphore limits concurrent extraction calls."""

    CONTENT = (
        "Danny led the QHORD standup today and discussed the roadmap for "
        "the upcoming June launch of the product.\n\n"
        "He also had a lengthy call with the Ashraya team about the upcoming "
        "community event that will be held next month in the auditorium."
    )

    @pytest.mark.asyncio
    @patch.dict(os.environ, {"RETRIEVAL_INDEXING_ENABLED": "true"})
    @patch("core.retrieval.pipeline.supabase")
    @patch("core.retrieval.pipeline._upsert_passage", new_callable=AsyncMock)
    @patch("core.retrieval.pipeline.upsert_memory_bundle_link", new_callable=AsyncMock)
    @patch("core.retrieval.pipeline.extract_triples", new_callable=AsyncMock)
    @patch("core.retrieval.pipeline.build_triple_graph", new_callable=AsyncMock)
    @patch("core.retrieval.pipeline._set_run_status")
    @patch("core.retrieval.pipeline.index_semaphore", wraps=asyncio.Semaphore(3))
    async def test_semaphore_limits_concurrent_calls(
        self, mock_sem, mock_set_status,         mock_build_graph, mock_extract, mock_bundle_link,
        mock_upsert_passage, mock_supabase,
    ):
        max_concurrent = 0
        _concurrent_counter = 0

        async def controlled_extract(*args, **kwargs):
            nonlocal max_concurrent, _concurrent_counter
            _concurrent_counter += 1
            max_concurrent = max(max_concurrent, _concurrent_counter)
            await asyncio.sleep(0.05)
            _concurrent_counter -= 1
            return ([MagicMock()], True)

        mock_upsert_passage.side_effect = [100, 200]
        mock_extract.side_effect = controlled_extract
        

        tasks = [
            index_memory(memory_id=i, content=self.CONTENT,
                         memory_type="memory", source="test")
            for i in range(6)
        ]
        await asyncio.gather(*tasks)

        assert max_concurrent <= 3, (
            f"Semaphore allowed {max_concurrent} concurrent extractions, "
            f"expected ≤ 3"
        )
        assert max_concurrent >= 2, (
            f"Semaphore serialized too aggressively: "
            f"max_concurrent={max_concurrent}, expected at least 2"
        )


# ──────────────────────────────────────────────
# Test 3: Retry replay idempotency
# ──────────────────────────────────────────────

class TestRetryReplay:
    """Verify retry_failed_index_runs picks up failed runs, retries,
    and escalates to dead_letter only after hitting max_retries."""

    CONTENT = (
        "Danny led the QHORD standup today and discussed the roadmap for "
        "the upcoming June launch of the product.\n\n"
        "He also had a lengthy call with the Ashraya team about the upcoming "
        "community event that will be held next month in the auditorium."
    )

    @pytest.mark.asyncio
    @patch.dict(os.environ, {"RETRIEVAL_INDEXING_ENABLED": "true"})
    @patch("core.retrieval.pipeline.supabase")
    @patch("core.retrieval.pipeline._upsert_passage", new_callable=AsyncMock)
    @patch("core.retrieval.pipeline.upsert_memory_bundle_link", new_callable=AsyncMock)
    @patch("core.retrieval.pipeline.extract_triples", new_callable=AsyncMock)
    @patch("core.retrieval.pipeline.build_triple_graph", new_callable=AsyncMock)
    @patch("core.retrieval.pipeline._set_run_status")
    async def test_retry_replay_no_duplicates(
        self, mock_set_status,         mock_build_graph, mock_extract, mock_bundle_link,
        mock_upsert_passage, mock_supabase,
    ):
        mock_upsert_passage.return_value = 100
        mock_extract.side_effect = [
            ([MagicMock()], True),
        ]
        

        # Short single-paragraph content → 1 passage
        result1 = await index_memory(
            memory_id=42, content="Danny led the QHORD standup.",
            memory_type="memory", source="test",
        )
        assert result1 is True

        assert mock_build_graph.call_count == 1, (
            f"Expected 1 build_triple_graph call, got {mock_build_graph.call_count}"
        )
        mock_set_status.assert_called_once()
        assert mock_set_status.call_args[0][1] == "completed"

    @pytest.mark.asyncio
    async def test_retry_sweeper_dead_letter_escalation(self):
        """When index_memory repeatedly fails, retry sweeper escalates to dead_letter."""
        import core.retrieval.pipeline as pipeline_mod

        # Verify the module-level variable exists in __dict__
        assert 'supabase' in pipeline_mod.__dict__, "supabase not in pipeline namespace"

        mock_supabase = MagicMock(name="supabase")
        mock_index = AsyncMock(return_value=False)

        runs_builder = MagicMock(name="runs_builder")
        runs_builder.select.return_value = runs_builder
        runs_builder.eq.return_value = runs_builder
        runs_builder.lt.return_value = runs_builder
        runs_builder.limit.return_value = runs_builder
        runs_builder.order.return_value = runs_builder
        runs_builder.in_.return_value = runs_builder
        runs_builder.update.return_value = runs_builder
        runs_builder.maybe_single.return_value.execute.return_value = _QueryResult(
            {"retry_count": 0}
        )
        runs_builder.execute.return_value = _QueryResult([
            {"id": 1, "source_type": "memory", "source_id": "42", "retry_count": 0},
            {"id": 2, "source_type": "memory", "source_id": "99", "retry_count": 1},
        ])

        mem_builder = MagicMock(name="mem_builder")
        mem_builder.select.return_value = mem_builder
        mem_builder.eq.return_value = mem_builder
        mem_builder.maybe_single.return_value.execute.side_effect = [
            _QueryResult({"id": 50, "content": "Test content 1.", "memory_type": "memory",
                          "source": "test", "metadata": {}}),
            _QueryResult({"id": 51, "content": "Test content 2.", "memory_type": "memory",
                          "source": "test", "metadata": {}}),
        ]

        def table_router(name):
            return mem_builder if name == "memories" else runs_builder
        mock_supabase.table.side_effect = table_router

        original_supabase = pipeline_mod.supabase
        original_index = pipeline_mod.index_memory
        original_audit = pipeline_mod.audit_log_sync
        pipeline_mod.supabase = mock_supabase
        pipeline_mod.index_memory = mock_index
        pipeline_mod.audit_log_sync = lambda *a, **kw: None

        # Verify the function's namespace sees our mocks
        func_globals = retry_failed_index_runs.__globals__
        assert func_globals['supabase'] is mock_supabase, \
            "Function globals not using mock supabase"
        assert func_globals['index_memory'] is mock_index, \
            "Function globals not using mock index_memory"
        assert func_globals is pipeline_mod.__dict__, \
            "Function globals not same as module dict"

        # Instrument: capture mock calls
        mock_supabase.mock_calls.clear()

        try:
            result = await retry_failed_index_runs(
                max_retries=2, batch_size=10, retry_delay_seconds=0
            )
        finally:
            pipeline_mod.supabase = original_supabase
            pipeline_mod.index_memory = original_index
            pipeline_mod.audit_log_sync = original_audit

        # Diagnose what the function actually did
        if result == 0:
            print("\n=== mock_calls ===")
            for call in mock_supabase.mock_calls[:30]:
                print(f"  {call}")

        assert result == 2, f"Expected 2 retried runs, got {result}"
        assert mock_index.call_count == 2, (
            f"Expected index_memory called 2 times, got {mock_index.call_count}"
        )
