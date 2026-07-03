"""Unit tests for Phase 3 — chunk enrichment with metadata prefixes."""

from core.retrieval.pipeline import _build_enrichment_prefix
from core.retrieval.config import RetrievalConfig


class TestBuildEnrichmentPrefix:
    """Tests for _build_enrichment_prefix pure function."""

    def test_source_type_only(self):
        """Prefix with just source_type and no entities."""
        result = _build_enrichment_prefix("note", [])
        assert result == "[note]"

    def test_source_type_with_entities(self):
        """Prefix includes source_type and up to 3 entities."""
        result = _build_enrichment_prefix("memory", ["ashraya", "pricing"])
        assert result == "[memory, ashraya, pricing]"

    def test_deduplication(self):
        """Duplicate entity labels are removed."""
        result = _build_enrichment_prefix("note", ["ashraya", "ashraya", "pricing"])
        assert result == "[note, ashraya, pricing]"

    def test_max_three_entities(self):
        """Only 3 entities are included (source_type + 3 = 4 parts max)."""
        result = _build_enrichment_prefix("memory", ["a", "b", "c", "d", "e"])
        assert result == "[memory, a, b, c]"

    def test_whitespace_stripped(self):
        """Entity labels are stripped and lowercased."""
        result = _build_enrichment_prefix("note", ["  Ashraya  ", "  Pricing  "])
        assert result == "[note, ashraya, pricing]"

    def test_empty_labels_skipped(self):
        """Empty string labels are skipped."""
        result = _build_enrichment_prefix("note", ["", "ashraya", ""])
        assert result == "[note, ashraya]"

    def test_format_matches_query_prefix(self):
        """Prefix format is stable: [source_type, entity1, entity2]."""
        prefix = _build_enrichment_prefix("memory", ["qhord", "launch"])
        assert prefix.startswith("[")
        assert prefix.endswith("]")
        assert "memory" in prefix
        assert "qhord" in prefix
        assert "launch" in prefix

    def test_source_types_vary(self):
        """Different source_types produce different prefixes."""
        p1 = _build_enrichment_prefix("note", [])
        p2 = _build_enrichment_prefix("task", [])
        p3 = _build_enrichment_prefix("email", [])
        assert p1 != p2 != p3
        assert p1 == "[note]"
        assert p2 == "[task]"
        assert p3 == "[email]"

    def test_realistic_entity_labels(self):
        """Realistic entity labels from triple extraction."""
        labels = ["ashraya", "church admin", "finances"]
        result = _build_enrichment_prefix("memory", labels)
        assert result == "[memory, ashraya, church admin, finances]"


class TestConfigFlag:
    """Verify the chunk_enrichment config flag reads correctly."""

    def test_flag_default_off(self):
        """RETRIEVAL_CHUNK_ENRICHMENT defaults to False."""
        import os
        os.environ.pop("RETRIEVAL_CHUNK_ENRICHMENT", None)
        cfg = RetrievalConfig()
        assert cfg.chunk_enrichment is False

    def test_flag_env_true(self, monkeypatch):
        """RETRIEVAL_CHUNK_ENRICHMENT=true enables the flag."""
        monkeypatch.setenv("RETRIEVAL_CHUNK_ENRICHMENT", "true")
        cfg = RetrievalConfig()
        assert cfg.chunk_enrichment is True
