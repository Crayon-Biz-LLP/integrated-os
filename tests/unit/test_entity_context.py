"""
Unit tests for the Entity Context Pipeline.

Tests EntityContext dataclass and basic extract_context_from_source behavior.
"""
import pytest
from core.lib.entity_context import EntityContext

# ── Aspect marker ──────────────────────────────────────────────────────────────
pytestmark = [pytest.mark.graph]


# ── EntityContext dataclass ─────────────────────────────────────────────────────


class TestEntityContext:
    """Test EntityContext dataclass construction and serialization."""

    def test_creates_with_all_fields(self):
        ctx = EntityContext(
            organization_id="org-uuid-1",
            pending_org_id=42,
            person_ids=["person-1", "person-2"],
            org_to_org_edges=[{"source": "A", "target": "B", "relationship": "WORKS_AT"}],
        )
        assert ctx.organization_id == "org-uuid-1"
        assert ctx.pending_org_id == 42
        assert len(ctx.person_ids) == 2
        assert len(ctx.org_to_org_edges) == 1

    def test_creates_with_defaults(self):
        ctx = EntityContext()
        assert ctx.organization_id is None
        assert ctx.pending_org_id is None
        assert ctx.person_ids == []
        assert ctx.org_to_org_edges == []

    def test_is_empty_when_all_none(self):
        ctx = EntityContext()
        assert ctx.is_empty() is True

    def test_is_not_empty_when_org_set(self):
        ctx = EntityContext(organization_id="org-uuid-1")
        assert ctx.is_empty() is False

    def test_is_not_empty_when_pending_set(self):
        ctx = EntityContext(pending_org_id=42)
        assert ctx.is_empty() is False

    def test_is_not_empty_when_persons_set(self):
        ctx = EntityContext(person_ids=["person-1"])
        assert ctx.is_empty() is False

    def test_primary_org_id_prefers_existing(self):
        ctx = EntityContext(organization_id="org-uuid-1", pending_org_id=42)
        assert ctx.primary_org_id() == "org-uuid-1"

    def test_primary_org_id_returns_none_when_no_existing(self):
        ctx = EntityContext(pending_org_id=42)
        assert ctx.primary_org_id() is None

    def test_primary_pending_org_id_returns_pending(self):
        ctx = EntityContext(pending_org_id=42)
        assert ctx.primary_pending_org_id() == 42

    def test_primary_pending_org_id_returns_none_when_no_pending(self):
        ctx = EntityContext(organization_id="org-uuid-1")
        assert ctx.primary_pending_org_id() is None

    def test_to_dict_roundtrip(self):
        ctx = EntityContext(
            organization_id="org-uuid-1",
            pending_org_id=42,
            person_ids=["person-1"],
            org_to_org_edges=[{"source": "A", "target": "B", "relationship": "WORKS_AT"}],
        )
        d = ctx.to_dict()
        assert d["organization_id"] == "org-uuid-1"
        assert d["pending_org_id"] == 42
        assert d["person_ids"] == ["person-1"]
        assert len(d["org_to_org_edges"]) == 1

        ctx2 = EntityContext.from_dict(d)
        assert ctx2.organization_id == ctx.organization_id
        assert ctx2.pending_org_id == ctx.pending_org_id
        assert ctx2.person_ids == ctx.person_ids
        assert ctx2.org_to_org_edges == ctx.org_to_org_edges

    def test_from_dict_with_missing_fields(self):
        d = {"organization_id": "org-uuid-1"}
        ctx = EntityContext.from_dict(d)
        assert ctx.organization_id == "org-uuid-1"
        assert ctx.pending_org_id is None
        assert ctx.person_ids == []
        assert ctx.org_to_org_edges == []

    def test_from_dict_restores_bug6_fields(self):
        """Bug 6 regression pin: from_dict used to silently drop three fields,
        so the confirm flow operated on a partial picture of extraction."""
        ctx = EntityContext(
            detected_entities=[{"type": "organization", "label": "Project Balance", "confidence": 0.9}],
            org_to_org_edges=[{"source": "Solvstrat", "target": "Project Balance", "relationship": "CLIENT_OF"}],
            org_to_org_edge_labels=["Project Balance"],
            extraction_timing="card",
        )
        ctx2 = EntityContext.from_dict(ctx.to_dict())
        assert ctx2.detected_entities == ctx.detected_entities
        assert ctx2.org_to_org_edges == ctx.org_to_org_edges
        assert ctx2.org_to_org_edge_labels == ["Project Balance"]
        assert ctx2.extraction_timing == "card"

    def test_from_dict_bug6_fields_default_when_missing(self):
        ctx = EntityContext.from_dict({})
        assert ctx.detected_entities == []
        assert ctx.org_to_org_edge_labels == []
        assert ctx.extraction_timing == ""

    def test_from_dict_with_none(self):
        ctx = EntityContext.from_dict(None)
        assert ctx.organization_id is None
        assert ctx.pending_org_id is None

    def test_from_dict_with_empty_dict(self):
        ctx = EntityContext.from_dict({})
        assert ctx.organization_id is None
        assert ctx.pending_org_id is None

    def test_serialization_preserves_nested_lists(self):
        ctx = EntityContext(
            person_ids=["p1", "p2", "p3"],
            org_to_org_edges=[
                {"source": "A", "target": "B", "relationship": "WORKS_AT"},
                {"source": "C", "target": "D", "relationship": "KNOWS"},
            ],
        )
        d = ctx.to_dict()
        ctx2 = EntityContext.from_dict(d)
        assert ctx2.person_ids == ["p1", "p2", "p3"]
        assert len(ctx2.org_to_org_edges) == 2

    def test_extraction_method_tracking(self):
        ctx = EntityContext(extraction_method="deterministic")
        assert ctx.extraction_method == "deterministic"

        ctx = EntityContext(extraction_method="hybrid")
        assert ctx.extraction_method == "hybrid"

        ctx = EntityContext(extraction_method="fallback_personal")
        assert ctx.extraction_method == "fallback_personal"

    def test_extraction_timing_tracking(self):
        ctx = EntityContext(extraction_timing="sync")
        assert ctx.extraction_timing == "sync"

        ctx = EntityContext(extraction_timing="async")
        assert ctx.extraction_timing == "async"

    def test_source_text_tracking(self):
        ctx = EntityContext(source_text="Invoice from Acme Corp for $500")
        assert ctx.source_text == "Invoice from Acme Corp for $500"


# ── extract_context_from_source edge cases ──────────────────────────────────────


class TestExtractContextEdgeCases:
    """Test edge cases that don't require mocking the DB."""

    @pytest.mark.asyncio
    async def test_returns_empty_for_empty_text(self):
        from core.lib.entity_context import extract_context_from_source
        ctx = await extract_context_from_source("", "owner-123")
        assert ctx.organization_id is None
        assert ctx.pending_org_id is None
        assert ctx.person_ids == []
        assert ctx.org_to_org_edges == []

    @pytest.mark.asyncio
    async def test_returns_empty_for_whitespace_only(self):
        from core.lib.entity_context import extract_context_from_source
        ctx = await extract_context_from_source("   \n\t  ", "owner-123")
        assert ctx.organization_id is None
        assert ctx.pending_org_id is None

    @pytest.mark.asyncio
    async def test_strips_quotation_marks(self):
        from core.lib.entity_context import extract_context_from_source
        # Quoted text should be stripped and still work
        ctx = await extract_context_from_source('"Buy groceries"', "owner-123")
        # Should complete without error
        assert ctx is not None

    @pytest.mark.asyncio
    async def test_handles_very_long_text(self):
        from core.lib.entity_context import extract_context_from_source
        # Long text should not crash
        long_text = "This is a very long message. " * 100
        ctx = await extract_context_from_source(long_text, "owner-123")
        assert ctx is not None

    @pytest.mark.asyncio
    async def test_handles_special_characters(self):
        from core.lib.entity_context import extract_context_from_source
        ctx = await extract_context_from_source(
            "Meeting with @John about $100 & review",
            "owner-123",
        )
        assert ctx is not None


# ── queue_pending_candidates: decision-gated materialization (Step 1) ─────────


class TestQueuePendingCandidates:
    """Extraction is pure — queue_pending_candidates() is the ONLY pending writer.

    These tests pin the orchestration (which candidates get queued, dedup,
    existing-org precedence) with the DB helpers monkeypatched.
    """

    def test_org_queued_when_unmatched_label(self, monkeypatch):
        from core.lib.entity_context import EntityContext, queue_pending_candidates
        calls = []

        def fake_create_pending_org(label, source_text, owner_id=None):
            calls.append(("org", label))
            return 101

        monkeypatch.setattr(
            "core.lib.entity_context._create_pending_org", fake_create_pending_org
        )
        ctx = EntityContext(pending_org_label="Nova Dynamics", source_text="msg")
        queue_pending_candidates(ctx, owner_id="owner-1")
        assert calls == [("org", "Nova Dynamics")]
        assert ctx.pending_org_id == 101

    def test_org_not_queued_when_existing_org_wins(self, monkeypatch):
        from core.lib.entity_context import EntityContext, queue_pending_candidates
        calls = []

        def fake_create_pending_org(label, source_text, owner_id=None):
            calls.append(label)
            return 101

        monkeypatch.setattr(
            "core.lib.entity_context._create_pending_org", fake_create_pending_org
        )
        # Existing org already resolved → never queue the new candidate
        ctx = EntityContext(
            organization_id="live-uuid", organization_name="Solvstrat",
            pending_org_label="Nova Dynamics", source_text="msg",
        )
        queue_pending_candidates(ctx, owner_id="owner-1")
        assert calls == []
        assert ctx.pending_org_id is None

    def test_no_label_no_queue(self, monkeypatch):
        from core.lib.entity_context import EntityContext, queue_pending_candidates
        calls = []

        def fake_create_pending_org(label, source_text, owner_id=None):
            calls.append(label)
            return 101

        monkeypatch.setattr(
            "core.lib.entity_context._create_pending_org", fake_create_pending_org
        )
        ctx = EntityContext(source_text="msg")
        queue_pending_candidates(ctx, owner_id="owner-1")
        assert calls == []

    def test_live_org_resolved_when_pending_creation_skipped(self, monkeypatch):
        """Step 2: when a live node already exists for the candidate, the queue
        resolves to it directly instead of leaving the org unlinked (a stale
        'approved' pending id is never stamped as pending_org_id)."""
        from core.lib.entity_context import EntityContext, queue_pending_candidates
        calls = []

        def fake_create_pending_org(label, source_text, owner_id=None):
            calls.append(label)
            return None  # live node already exists → no pending row created

        monkeypatch.setattr(
            "core.lib.entity_context._create_pending_org", fake_create_pending_org
        )
        monkeypatch.setattr(
            "core.lib.entity_context._find_existing_org",
            lambda label, owner_id=None: {"id": "live-org-uuid", "label": "Nova Dynamics"},
        )
        ctx = EntityContext(pending_org_label="Nova Dynamics", source_text="msg")
        queue_pending_candidates(ctx, owner_id="owner-1")
        assert calls == ["Nova Dynamics"]
        assert ctx.pending_org_id is None
        assert ctx.organization_id == "live-org-uuid"
        assert ctx.organization_name == "Nova Dynamics"

    def test_matched_persons_skipped_unmatched_queued(self, monkeypatch):
        from core.lib.entity_context import EntityContext, queue_pending_candidates
        calls = []

        def fake_create_pending_person(label, source_text, owner_id=None):
            calls.append(label)
            return 201

        monkeypatch.setattr(
            "core.lib.entity_context._create_pending_person", fake_create_pending_person
        )
        ctx = EntityContext(
            person_ids=["live-person-uuid"],
            person_names=["Marcus"],
            detected_entities=[
                {"type": "person", "label": "Marcus", "matched": True},   # live — skip
                {"type": "person", "label": "Sharukh", "matched": False}, # new — queue
                {"type": "organization", "label": "Spartan Schools"},      # not a person
            ],
            source_text="msg",
        )
        queue_pending_candidates(ctx, owner_id="owner-1")
        assert calls == ["Sharukh"]
        assert ctx.pending_person_ids == [201]

    def test_person_dedup_by_existing_pending_id(self, monkeypatch):
        from core.lib.entity_context import EntityContext, queue_pending_candidates
        calls = []

        def fake_create_pending_person(label, source_text, owner_id=None):
            calls.append(label)
            return 201  # same id as already-tracked pending

        monkeypatch.setattr(
            "core.lib.entity_context._create_pending_person", fake_create_pending_person
        )
        ctx = EntityContext(
            pending_person_ids=[201],
            detected_entities=[{"type": "person", "label": "Sharukh", "matched": False}],
            source_text="msg",
        )
        queue_pending_candidates(ctx, owner_id="owner-1")
        # Helper returns the existing id → appended once (no duplicate)
        assert ctx.pending_person_ids == [201]

    def test_none_ctx_noop(self, monkeypatch):
        from core.lib.entity_context import queue_pending_candidates
        queue_pending_candidates(None, owner_id="owner-1")
        queue_pending_candidates(None)

    def test_empty_label_person_skipped(self, monkeypatch):
        from core.lib.entity_context import EntityContext, queue_pending_candidates
        calls = []

        def fake_create_pending_person(label, source_text, owner_id=None):
            calls.append(label)
            return 201

        monkeypatch.setattr(
            "core.lib.entity_context._create_pending_person", fake_create_pending_person
        )
        ctx = EntityContext(
            detected_entities=[{"type": "person", "label": "   ", "matched": False}],
            source_text="msg",
        )
        queue_pending_candidates(ctx, owner_id="owner-1")
        assert calls == []
