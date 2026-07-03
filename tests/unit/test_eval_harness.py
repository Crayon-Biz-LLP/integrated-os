"""Unit tests for retrieval evaluation harness — recall, precision, metrics.

These tests exercise pure functions only (no DB, no LLM).
"""

import pytest
from core.retrieval.eval import compute_recall, compute_precision, compute_metrics


# ---------------------------------------------------------------------------
# compute_recall
# ---------------------------------------------------------------------------
class TestComputeRecall:
    def test_perfect_recall(self):
        """All expected items returned within k."""
        expected = {1, 2, 3}
        returned = [1, 2, 3, 4, 5]
        assert compute_recall(expected, returned, k=5) == 1.0

    def test_partial_recall(self):
        """Some expected items returned."""
        expected = {1, 2, 3, 4}
        returned = [1, 2, 99]
        assert compute_recall(expected, returned, k=5) == 0.5

    def test_zero_recall(self):
        """No expected items in top-k."""
        expected = {10, 20, 30}
        returned = [1, 2, 3]
        assert compute_recall(expected, returned, k=5) == 0.0

    def test_empty_expected(self):
        """Empty expected set returns 0.0."""
        assert compute_recall(set(), [1, 2, 3], k=5) == 0.0

    def test_k_limits_search(self):
        """Only top-k items count — items beyond k are ignored."""
        expected = {100}
        returned = [1, 2, 3, 100]  # 100 is at index 3 (within k=5)
        assert compute_recall(expected, returned, k=5) == 1.0

        returned2 = [1, 2, 3, 4, 5, 100]  # 100 is at index 5 (beyond k=5)
        assert compute_recall(expected, returned2, k=5) == 0.0

    def test_k_equals_one(self):
        """k=1 — only the very top item counts."""
        expected = {42}
        returned = [42, 99, 100]
        assert compute_recall(expected, returned, k=1) == 1.0

        returned2 = [99, 42, 100]
        assert compute_recall(expected, returned2, k=1) == 0.0

    def test_duplicates_in_returned(self):
        """Duplicates in returned list don't inflate recall."""
        expected = {1}
        returned = [1, 1, 1, 1]
        assert compute_recall(expected, returned, k=5) == 1.0

    def test_empty_returned(self):
        """Empty returned list — recall is 0 unless expected is empty."""
        expected = {1, 2}
        assert compute_recall(expected, [], k=5) == 0.0

    def test_expected_larger_than_k(self):
        """More expected items than k — recall capped by k."""
        expected = {1, 2, 3, 4, 5, 6}
        returned = [1, 2, 3, 4, 5]
        # 5 of 6 expected found in top-5 → 5/6
        assert compute_recall(expected, returned, k=5) == pytest.approx(5 / 6)


# ---------------------------------------------------------------------------
# compute_precision
# ---------------------------------------------------------------------------
class TestComputePrecision:
    def test_perfect_precision(self):
        """All returned items are relevant."""
        expected = {1, 2, 3}
        returned = [1, 2, 3]
        assert compute_precision(expected, returned, k=3) == 1.0

    def test_partial_precision(self):
        """Some returned items are relevant."""
        expected = {1, 2}
        returned = [1, 99, 2, 100]
        # top-3: [1, 99, 2] → 2 relevant out of 3
        assert compute_precision(expected, returned, k=3) == pytest.approx(2 / 3)

    def test_zero_precision(self):
        """No returned items are relevant."""
        expected = {10, 20}
        returned = [1, 2, 3]
        assert compute_precision(expected, returned, k=3) == 0.0

    def test_k_zero(self):
        """k=0 returns 0.0."""
        assert compute_precision({1}, [1], k=0) == 0.0

    def test_precision_beyond_relevant(self):
        """More returned items than relevant — precision drops."""
        expected = {1}
        returned = [1, 2, 3, 4, 5]
        assert compute_precision(expected, returned, k=5) == pytest.approx(0.2)


# ---------------------------------------------------------------------------
# compute_metrics
# ---------------------------------------------------------------------------
class TestComputeMetrics:
    def test_returns_all_k_values(self):
        """Metrics dict contains recall and precision for each k."""
        expected = {1, 2}
        returned = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12]
        metrics = compute_metrics(expected, returned, k_values=[5, 8, 12])

        assert "recall_at_5" in metrics
        assert "recall_at_8" in metrics
        assert "recall_at_12" in metrics
        assert "precision_at_5" in metrics
        assert "precision_at_8" in metrics
        assert "precision_at_12" in metrics

    def test_recall_increases_with_k(self):
        """Recall should be non-decreasing as k increases."""
        expected = {50}
        returned = [1, 2, 3, 4, 50, 6, 7, 8]
        metrics = compute_metrics(expected, returned, k_values=[3, 5, 8])

        assert metrics["recall_at_3"] == 0.0
        assert metrics["recall_at_5"] == 1.0
        assert metrics["recall_at_8"] == 1.0

    def test_precision_decreases_with_k(self):
        """Precision should generally decrease as k increases (more noise)."""
        expected = {1}
        returned = [1, 2, 3, 4, 5, 6, 7, 8]
        metrics = compute_metrics(expected, returned, k_values=[1, 5, 8])

        assert metrics["precision_at_1"] == 1.0
        assert metrics["precision_at_5"] == pytest.approx(0.2)
        assert metrics["precision_at_8"] == pytest.approx(0.125)

    def test_empty_expected_gives_zeros(self):
        """Empty expected set → all recalls are 0.0."""
        metrics = compute_metrics(set(), [1, 2, 3], k_values=[5])
        assert metrics["recall_at_5"] == 0.0

    def test_custom_k_values(self):
        """Accepts non-default k values."""
        expected = {1}
        returned = [1, 2, 3]
        metrics = compute_metrics(expected, returned, k_values=[1, 2, 3])

        assert metrics["recall_at_1"] == 1.0
        assert metrics["recall_at_2"] == 1.0
        assert metrics["recall_at_3"] == 1.0
        assert metrics["precision_at_1"] == 1.0
        assert metrics["precision_at_2"] == pytest.approx(0.5)
        assert metrics["precision_at_3"] == pytest.approx(1 / 3)
