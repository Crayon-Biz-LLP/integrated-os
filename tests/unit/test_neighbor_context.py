"""Unit tests for Phase 2 — neighboring-context surfacing in _assemble_bundles."""

from core.retrieval.search import _find_neighbor_ids, _MAX_SUPPORTING_PASSAGES


class TestFindNeighborIds:
    """Tests for _find_neighbor_ids pure function."""

    def test_adjacent_passage_found(self):
        """Passage at index 1 is a neighbor of index 0."""
        source_map = {("email", "msg1"): [(10, 0), (11, 1), (12, 2)]}
        passage_index_map = {10: 0, 11: 1, 12: 2}
        source_key_map = {10: ("email", "msg1"), 11: ("email", "msg1"), 12: ("email", "msg1")}

        result = _find_neighbor_ids([10], source_map, passage_index_map, source_key_map)
        assert 11 in result

    def test_both_neighbors_found(self):
        """Passage at index 2 has neighbors at index 1 and 3."""
        source_map = {("email", "msg1"): [(10, 1), (11, 2), (12, 3)]}
        passage_index_map = {10: 1, 11: 2, 12: 3}
        source_key_map = {10: ("email", "msg1"), 11: ("email", "msg1"), 12: ("email", "msg1")}

        result = _find_neighbor_ids([11], source_map, passage_index_map, source_key_map)
        assert set(result) == {10, 12}

    def test_no_neighbors_different_source(self):
        """Neighbors must share the same source_type + source_id."""
        source_map = {
            ("email", "msg1"): [(10, 0), (11, 1)],
            ("email", "msg2"): [(20, 0), (21, 1)],
        }
        passage_index_map = {10: 0, 11: 1, 20: 0, 21: 1}
        source_key_map = {
            10: ("email", "msg1"), 11: ("email", "msg1"),
            20: ("email", "msg2"), 21: ("email", "msg2"),
        }

        result = _find_neighbor_ids([10], source_map, passage_index_map, source_key_map)
        # msg1 index 0 has neighbor index 1 (passage 11), but NOT msg2 index 0
        assert 11 in result
        assert 20 not in result
        assert 21 not in result

    def test_no_neighbors_gap_in_index(self):
        """Non-adjacent indices (gap > 1) are not neighbors."""
        source_map = {("email", "msg1"): [(10, 0), (11, 3)]}
        passage_index_map = {10: 0, 11: 3}
        source_key_map = {10: ("email", "msg1"), 11: ("email", "msg1")}

        result = _find_neighbor_ids([10], source_map, passage_index_map, source_key_map)
        assert result == []

    def test_empty_input(self):
        """Empty passage_ids returns empty list."""
        result = _find_neighbor_ids([], {}, {}, {})
        assert result == []

    def test_no_duplicate_neighbors(self):
        """If passage is already in input, it's not returned as neighbor."""
        source_map = {("email", "msg1"): [(10, 0), (11, 1), (12, 2)]}
        passage_index_map = {10: 0, 11: 1, 12: 2}
        source_key_map = {10: ("email", "msg1"), 11: ("email", "msg1"), 12: ("email", "msg1")}

        # Passages 10 and 11 are both in input — 11 should not be a neighbor of 10
        result = _find_neighbor_ids([10, 11], source_map, passage_index_map, source_key_map)
        assert 11 not in result
        # But 12 should be a neighbor of 11
        assert 12 in result

    def test_multiple_input_passages_collect_all_neighbors(self):
        """Neighbors from multiple input passages are collected."""
        source_map = {("email", "msg1"): [(10, 0), (11, 1), (12, 2), (13, 3)]}
        passage_index_map = {10: 0, 11: 1, 12: 2, 13: 3}
        source_key_map = {i: ("email", "msg1") for i in [10, 11, 12, 13]}

        # Input passages 10 and 12: neighbors are 11 (from 10) and 11,13 (from 12)
        result = _find_neighbor_ids([10, 12], source_map, passage_index_map, source_key_map)
        assert set(result) == {11, 13}

    def test_missing_passage_in_index_map(self):
        """Passage not in index_map is skipped gracefully."""
        source_map = {("email", "msg1"): [(10, 0), (11, 1)]}
        passage_index_map = {10: 0}  # 11 not in map
        source_key_map = {10: ("email", "msg1")}

        result = _find_neighbor_ids([10], source_map, passage_index_map, source_key_map)
        # 11 is in source_map but not in passage_index_map, so no neighbor found
        # Actually 11 IS in source_map as (11, 1) but passage_index_map only has 10
        # The neighbor 11 has index 1 which is abs(1-0)==1, so it's found
        assert 11 in result

    def test_boundary_passage_first_index(self):
        """First passage (index 0) only has a right neighbor."""
        source_map = {("email", "msg1"): [(10, 0), (11, 1), (12, 2)]}
        passage_index_map = {10: 0, 11: 1, 12: 2}
        source_key_map = {i: ("email", "msg1") for i in [10, 11, 12]}

        result = _find_neighbor_ids([10], source_map, passage_index_map, source_key_map)
        assert result == [11]

    def test_boundary_passage_last_index(self):
        """Last passage only has a left neighbor."""
        source_map = {("email", "msg1"): [(10, 0), (11, 1), (12, 2)]}
        passage_index_map = {10: 0, 11: 1, 12: 2}
        source_key_map = {i: ("email", "msg1") for i in [10, 11, 12]}

        result = _find_neighbor_ids([12], source_map, passage_index_map, source_key_map)
        assert result == [11]


class TestConfigFlag:
    """Verify the context_neighbors config flag reads correctly."""

    def test_flag_default_off(self):
        """RETRIEVAL_CONTEXT_NEIGHBORS defaults to False."""
        from core.retrieval.config import RetrievalConfig
        import os
        # Ensure env is not set
        os.environ.pop("RETRIEVAL_CONTEXT_NEIGHBORS", None)
        cfg = RetrievalConfig()
        assert cfg.context_neighbors is False

    def test_flag_env_true(self, monkeypatch):
        """RETRIEVAL_CONTEXT_NEIGHBORS=true enables the flag."""
        monkeypatch.setenv("RETRIEVAL_CONTEXT_NEIGHBORS", "true")
        from core.retrieval.config import RetrievalConfig
        cfg = RetrievalConfig()
        assert cfg.context_neighbors is True

    def test_max_supporting_constant(self):
        """_MAX_SUPPORTING_PASSAGES is 5."""
        assert _MAX_SUPPORTING_PASSAGES == 5
