"""Unit tests for tsvector search helpers — _build_tsquery.

These are pure functions with no DB dependency.
"""

from core.retrieval.search import _build_tsquery


class TestBuildTsquery:
    def test_single_word(self):
        """Single word produces a bare tsquery term."""
        assert _build_tsquery(["ashraya"]) == "ashraya"

    def test_multiple_words(self):
        """Multiple words joined with OR."""
        result = _build_tsquery(["ashraya", "qhord"])
        assert result == "ashraya | qhord"

    def test_multi_word_phrase_splits(self):
        """Multi-word phrase is split into individual tokens."""
        result = _build_tsquery(["church admin"])
        assert result == "church | admin"

    def test_mixed_single_and_multi_word(self):
        """Mix of single and multi-word phrases."""
        result = _build_tsquery(["ashraya", "church admin", "pricing"])
        assert result == "ashraya | church | admin | pricing"

    def test_deduplication(self):
        """Duplicate words are deduplicated preserving first occurrence."""
        result = _build_tsquery(["ashraya", "ashraya meeting"])
        assert result == "ashraya | meeting"

    def test_empty_input(self):
        """Empty list returns empty string."""
        assert _build_tsquery([]) == ""

    def test_empty_strings_filtered(self):
        """Empty and whitespace-only strings are filtered out."""
        assert _build_tsquery(["", "  ", "ashraya"]) == "ashraya"

    def test_short_words_filtered(self):
        """Words shorter than 2 chars are filtered out."""
        result = _build_tsquery(["a", "bb", "ccc"])
        assert result == "bb | ccc"

    def test_punctuation_stripped(self):
        """Punctuation is stripped from words."""
        result = _build_tsquery(["'ashraya'", '"qhord"', "church!"])
        assert result == "ashraya | qhord | church"

    def test_lowercased(self):
        """All output is lowercased."""
        result = _build_tsquery(["Ashraya", "QHORD", "Church"])
        assert result == "ashraya | qhord | church"

    def test_ordering_preserved(self):
        """First occurrence order is preserved."""
        result = _build_tsquery(["zeta", "alpha", "beta"])
        assert result == "zeta | alpha | beta"

    def test_realistic_query(self):
        """Realistic query phrases from _parse_query."""
        result = _build_tsquery(["what", "should", "remember", "meeting", "ashraya"])
        assert "ashraya" in result
        assert "meeting" in result
        assert " | " in result

    def test_llm_entity_phrases(self):
        """LLM-extracted entities (multi-word) are split correctly."""
        result = _build_tsquery(["QHORD pricing", "church admin"])
        assert result == "qhord | pricing | church | admin"

    def test_special_chars_filtered(self):
        """Words containing tsquery operators are filtered out."""
        result = _build_tsquery(["foo&bar", "baz|qux", "ok", "a(b)"])
        # Only 'ok' should survive — the others contain & or | or ( or )
        assert result == "ok"
