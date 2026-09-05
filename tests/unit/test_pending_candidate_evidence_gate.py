"""Evidence-gate + provenance guard tests for the pending-candidate queue.

Asserts the hardening in core/lib/entity_context.py (P1 + P2):

  1. Evidence gate — common-word and generic-phrase labels are REJECTED by
     _create_pending_org / _create_pending_person, so "Please", "Chief",
     "Staff", generic single-word meta-labels, and other clearly-non-entity
     labels never become pending nodes.

  2. LLM-only minimum evidence — a person candidate that is NOT already
     detected deterministically and NOT already a known person name is an
     LLM-only guess and must clear _llm_candidate_has_minimum_evidence.
     "Please" must FAIL that bar (common word); "Ravi", all-caps
     abbreviations, and multi-word proper labels must PASS.

  3. Provenance — every pending node carries provenance when provided
     (written to the pending_nodes.provenance JSON column); rows created
     without provenance still exist (live call sites aren't provenance-aware
     yet) but a WARNING is emitted via audit_log_sync so untraceable-ghost
     rows are visible for cleanup.

  4. Happy path still works — deterministic candidates with real evidence
     still become pending nodes, and the context fields (pending_org_id,
     pending_person_ids) are filled correctly.

Fakes match the real call sequences in entity_context.py:
  _create_pending_* do, in order:
    table("pending_nodes").select("id").ilike(...).eq("owner_id", ...)
        .in_("status", [...]).limit(1).execute()      # dedup pending
    table("graph_nodes").select("id").ilike(...)...
        .limit(1).execute()                            # dedup approved
    table("pending_nodes").insert(row).execute()       # materialize
  queue_pending_candidates falls back to _find_existing_org (graph_nodes
  ilike scan) when org creation yields no pending row.

The functions import `tenant_aware_client` from core.services.db inside
their bodies, so the fake patches core.services.db.tenant_aware_client.
audit_log_sync is imported at module scope in entity_context.py and does
NOT emit through the `logging` module (print-on-failure only), so the
provenance WARNING is asserted by stubbing it with a recorder.

Marker: graph (graph/pending-node integrity, evidence-gate hardening).
"""
from __future__ import annotations

import json

import pytest

pytestmark = pytest.mark.graph

_DANNY_TEST = "c302706e-fe61-422a-b384-68e3bc8f6f8e"


# ── fakes ─────────────────────────────────────────────────────────────────────

class _FakeResp:
    def __init__(self, data):
        self.data = data


class _FakeChain:
    """Read chain: table('x').select(...).ilike(...).eq(...).limit(1).execute()."""

    def __init__(self, records):
        self._records = records
        self.filters = []

    def select(self, *a, **k):
        return self

    def ilike(self, *a, **k):
        self.filters.append(("ilike", a))
        return self

    def eq(self, *a, **k):
        self.filters.append(("eq", a))
        return self

    def in_(self, *a, **k):
        self.filters.append(("in_", a))
        return self

    def limit(self, *a, **k):
        return self

    def execute(self):
        return _FakeResp(self._records)


class _FakeInsertResult:
    def __init__(self, insert_id):
        self._insert_id = insert_id

    def execute(self):
        return _FakeResp([{"id": self._insert_id}])


class _FakeSupabase:
    """table(name) → reads return configured records, inserts return insert_id.

    pending_rows/graph_rows are the records a SELECT on that table returns
    (empty → no existing pending/approved node → materialization proceeds).
    insert_id is the id the pending_nodes INSERT returns.
    """

    def __init__(self, *, pending_rows=None, graph_rows=None, insert_id=99):
        self._rows = {
            "pending_nodes": pending_rows if pending_rows is not None else [],
            "graph_nodes": graph_rows if graph_rows is not None else [],
        }
        self._insert_id = insert_id
        self.inserted_rows = []  # every row passed to .insert(), for assertions

    def table(self, name):
        return _FakeTable(name, self._rows.get(name, []), self._insert_id, self.inserted_rows)


class _FakeTable:
    def __init__(self, name, records, insert_id, inserted_rows):
        self._name = name
        self._records = records
        self._insert_id = insert_id
        self._inserted_rows = inserted_rows

    def select(self, *a, **k):
        return _FakeChain(self._records)

    def insert(self, row):
        self._inserted_rows.append(row)
        return _FakeInsertResult(self._insert_id)

    def update(self, *a, **k):
        return _FakeChain(self._records)


def _patch_client(monkeypatch, *, pending_rows=None, graph_rows=None, insert_id=99) -> _FakeSupabase:
    """Patch the tenant_aware_client symbol entity_context imports at call time."""
    fake = _FakeSupabase(
        pending_rows=pending_rows, graph_rows=graph_rows, insert_id=insert_id
    )
    monkeypatch.setattr("core.services.db.tenant_aware_client", lambda: fake)
    return fake


def _record_audit(monkeypatch) -> list[dict]:
    """Stub entity_context's audit_log_sync with a recorder (real one does not
    emit through the logging module, so caplog cannot observe it)."""
    calls: list[dict] = []

    def _rec(service, level, message, metadata=None):
        calls.append({"service": service, "level": level, "message": message})

    monkeypatch.setattr("core.lib.entity_context.audit_log_sync", _rec)
    return calls


def _make_provenance(origin_table="messages", origin_id="msg-1"):
    return {"origin_table": origin_table, "origin_id": origin_id}


# ── 1. Evidence gate: common words / generic labels rejected ──────────────────

class TestCommonWordLabelRejected:
    """Common-word and generic-phrase labels must NOT become pending nodes."""

    _REJECT_COMMON_WORD = ["Please", "Chief", "Staff"]

    _REJECT_GENERIC_SINGLE_WORD = [
        "News", "Media", "Update", "Status", "Report", "Feedback",
        "Note", "Meeting", "Call", "Email", "Task",
    ]

    @pytest.mark.parametrize("label", _REJECT_COMMON_WORD)
    def test_common_word_org_label_rejected(self, monkeypatch, label):
        from core.lib.entity_context import _create_pending_org

        fake = _patch_client(monkeypatch)
        r = _create_pending_org(label, "some text", owner_id=_DANNY_TEST)
        assert r is None, f"'{label}' must be REJECTED (common-word evidence gate)"
        assert fake.inserted_rows == [], "evidence-gate rejection must not INSERT"

    @pytest.mark.parametrize("label", _REJECT_COMMON_WORD)
    def test_common_word_person_label_rejected(self, monkeypatch, label):
        from core.lib.entity_context import _create_pending_person

        fake = _patch_client(monkeypatch)
        r = _create_pending_person(
            label,
            "some text",
            owner_id=_DANNY_TEST,
            detected_entities=[{"type": "person", "label": "Someone Else", "confidence": 0.9}],
            person_names=[],
        )
        assert r is None, f"'{label}' must be REJECTED (common-word evidence gate)"
        assert fake.inserted_rows == [], "evidence-gate rejection must not INSERT"

    @pytest.mark.parametrize("label", _REJECT_GENERIC_SINGLE_WORD)
    def test_generic_single_word_label_rejected(self, monkeypatch, label):
        from core.lib.entity_context import _create_pending_org

        fake = _patch_client(monkeypatch)
        r = _create_pending_org(label, "some text", owner_id=_DANNY_TEST)
        assert r is None, f"'{label}' must be REJECTED (generic-pattern evidence gate)"
        assert fake.inserted_rows == [], "evidence-gate rejection must not INSERT"

    def test_rejection_is_audited(self, monkeypatch):
        """A rejected candidate must emit a WARNING via audit_log_sync so the
        junk-label class is visible in logs, not silently dropped."""
        from core.lib.entity_context import _create_pending_org

        _patch_client(monkeypatch)
        calls = _record_audit(monkeypatch)
        _create_pending_org("Please", "some text", owner_id=_DANNY_TEST)
        assert any(
            c["level"] == "WARNING" and "Rejected pending org" in c["message"]
            for c in calls
        )


# ── 2. LLM-only minimum evidence ─────────────────────────────────────────────

class TestLLMOnlyMinimumEvidence:
    """A person candidate that is LLM-only (not deterministic, not a known
    person name) must clear _llm_candidate_has_minimum_evidence. Common words
    must FAIL. Real-looking names / all-caps abbreviations / multi-word proper
    labels must PASS."""

    def test_common_word_fails_minimum_evidence(self):
        from core.lib.entity_context import _llm_candidate_has_minimum_evidence

        assert _llm_candidate_has_minimum_evidence("Please", [], []) is False
        assert _llm_candidate_has_minimum_evidence("Chief", [], []) is False

    def test_capitalized_single_name_passes_minimum_evidence(self):
        from core.lib.entity_context import _llm_candidate_has_minimum_evidence

        assert _llm_candidate_has_minimum_evidence("Ravi", [], []) is True

    def test_all_caps_abbreviation_passes_minimum_evidence(self):
        from core.lib.entity_context import _llm_candidate_has_minimum_evidence

        assert _llm_candidate_has_minimum_evidence("DBS", [], []) is True

    def test_multi_word_proper_label_passes_minimum_evidence(self):
        from core.lib.entity_context import _llm_candidate_has_minimum_evidence

        assert _llm_candidate_has_minimum_evidence("Ravi Hariharan", [], []) is True

    def test_llm_only_guess_never_materializes(self, monkeypatch):
        """Integration: an LLM-only guess that fails the evidence bar must not
        become a pending row — even when it is the only detected candidate."""
        from core.lib.entity_context import _create_pending_person

        fake = _patch_client(monkeypatch)
        r = _create_pending_person(
            "Please",
            "source text",
            owner_id=_DANNY_TEST,
            detected_entities=[{"type": "person", "label": "Marcus", "confidence": 0.9}],
            person_names=[],
        )
        assert r is None
        assert fake.inserted_rows == []

    def test_llm_only_entity_like_candidate_materializes(self, monkeypatch):
        """Integration: an LLM-only candidate that clears the evidence bar
        (entity-like label quality) is created after passing both dedup scans."""
        from core.lib.entity_context import _create_pending_person

        fake = _patch_client(monkeypatch, insert_id=77)
        r = _create_pending_person(
            "Ravi",
            "source text",
            owner_id=_DANNY_TEST,
            detected_entities=[{"type": "person", "label": "Marcus", "confidence": 0.9}],
            person_names=[],
        )
        assert r == 77
        assert fake.inserted_rows and fake.inserted_rows[0]["label"] == "Ravi"

    def test_known_person_name_bypasses_minimum_evidence(self, monkeypatch):
        from core.lib.entity_context import _create_pending_person

        _patch_client(monkeypatch, insert_id=88)
        r = _create_pending_person(
            "Sharukh",
            "source text",
            owner_id=_DANNY_TEST,
            detected_entities=[{"type": "person", "label": "Someone New", "confidence": 0.9}],
            person_names=["Sharukh"],
        )
        assert r == 88, "'Sharukh' as a known person name must be created"

    def test_deterministically_detected_candidate_materializes(self, monkeypatch):
        from core.lib.entity_context import _create_pending_person

        _patch_client(monkeypatch, insert_id=66)
        r = _create_pending_person(
            "Ravi",
            "source text",
            owner_id=_DANNY_TEST,
            detected_entities=[{"type": "person", "label": "Ravi", "confidence": 1.0}],
            person_names=[],
        )
        assert r == 66, "deterministically detected candidate must be created"


# ── 3. Provenance ────────────────────────────────────────────────────────────

class TestProvenanceMandatory:
    """Pending nodes carry provenance when provided (pending_nodes.provenance
    JSON column); rows created without provenance still exist (live call sites
    aren't provenance-aware yet) but a WARNING is emitted so untraceable-ghost
    rows are visible for cleanup."""

    def test_pending_node_captures_provenance(self, monkeypatch):
        from core.lib.entity_context import _create_pending_org

        fake = _patch_client(monkeypatch, insert_id=42)
        calls = _record_audit(monkeypatch)
        prov = {"origin_table": "raw_dumps", "origin_id": "rd-123"}

        r = _create_pending_org(
            "Solvstrat Academy", "source text", owner_id=_DANNY_TEST, provenance=prov
        )
        assert r == 42
        assert fake.inserted_rows and fake.inserted_rows[0]["provenance"] == json.dumps(prov)
        assert not any("without provenance" in c["message"] for c in calls)

    def test_pending_node_without_provenance_emits_warning(self, monkeypatch):
        from core.lib.entity_context import _create_pending_org

        fake = _patch_client(monkeypatch, insert_id=43)
        calls = _record_audit(monkeypatch)

        r = _create_pending_org(
            "Solvstrat Academy", "source text", owner_id=_DANNY_TEST, provenance=None
        )
        assert r == 43
        assert fake.inserted_rows and "provenance" not in fake.inserted_rows[0]
        assert any(
            c["level"] == "WARNING" and "without provenance" in c["message"]
            for c in calls
        )


# ── 4. Happy path still works ────────────────────────────────────────────────

class TestHappyPathStillWorks:
    """Deterministic candidates with real evidence still become pending nodes,
    and the context fields are filled correctly."""

    def test_pending_org_created_with_evidence(self, monkeypatch):
        from core.lib.entity_context import EntityContext, queue_pending_candidates

        _created = {}

        def _patch_create_pending_org(label, source_text, owner_id, *, provenance=None):
            _created[label] = {"label": label, "source_text": source_text}
            return 101

        monkeypatch.setattr("core.lib.entity_context._create_pending_org", _patch_create_pending_org)
        monkeypatch.setattr("core.lib.entity_context._create_pending_person", lambda *a, **k: None)
        _patch_client(monkeypatch)

        ctx = EntityContext(
            source_text="Invoice from Acme Corp for $500",
            pending_org_label="Acme Corp",
        )
        queue_pending_candidates(ctx, owner_id=_DANNY_TEST)
        assert ctx.pending_org_id == 101
        assert _created["Acme Corp"]["label"] == "Acme Corp"

    def test_pending_person_created_with_evidence(self, monkeypatch):
        from core.lib.entity_context import EntityContext, queue_pending_candidates

        _created = {}

        def _patch_create_pending_person(
            label, source_text, owner_id, *, provenance=None, detected_entities=None, person_names=None
        ):
            _created[label] = {"label": label}
            return 201

        monkeypatch.setattr("core.lib.entity_context._create_pending_person", _patch_create_pending_person)
        monkeypatch.setattr("core.lib.entity_context._create_pending_org", lambda *a, **k: None)
        _patch_client(monkeypatch)

        ctx = EntityContext(
            source_text="Met with Sharukh about the new project",
            detected_entities=[{"type": "person", "label": "Sharukh", "confidence": 1.0}],
        )
        queue_pending_candidates(ctx, owner_id=_DANNY_TEST)
        assert ctx.pending_person_ids == [201]
        assert _created["Sharukh"]["label"] == "Sharukh"

    def test_existing_approved_node_skips_creation(self, monkeypatch):
        """When a live approved graph node already exists for the label, the org
        helper returns None (no INSERT) and queue_pending_candidates resolves
        the context to the live node instead."""
        from core.lib.entity_context import EntityContext, queue_pending_candidates

        fake = _patch_client(
            monkeypatch,
            graph_rows=[{"id": "uuid-1", "label": "Rhodey OS", "type": "organization"}],
        )
        monkeypatch.setattr("core.lib.entity_context._create_pending_person", lambda *a, **k: None)

        ctx = EntityContext(
            source_text="msg",
            pending_org_label="Rhodey OS",
        )
        queue_pending_candidates(ctx, owner_id=_DANNY_TEST)
        assert ctx.pending_org_id is None
        assert ctx.organization_id == "uuid-1"
        assert fake.inserted_rows == [], "approved graph node must not trigger an INSERT"

    def test_dedup_returns_existing_pending_id(self, monkeypatch):
        """A label that already sits in pending_nodes resolves to the existing
        pending id instead of inserting a second row (idempotent requeue)."""
        from core.lib.entity_context import _create_pending_org

        fake = _patch_client(
            monkeypatch,
            pending_rows=[{"id": 555, "label": "Solvstrat", "status": "pending"}],
        )
        r = _create_pending_org("Solvstrat", "some text", owner_id=_DANNY_TEST)
        assert r == 555
        assert fake.inserted_rows == [], "dedup hit must not INSERT"
