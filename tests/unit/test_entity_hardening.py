"""Regression tests for the Aug-6 backfill mislabel hardening.

Covers the four root-cause fixes:
1. Edge-label echo artifacts ('Pup (animal)') are stripped and endpoints must
   be detected entities (never auto-vivified as 'concept').
2. The 'concept' guess is gone from graph.py's unified pipeline.
3. Pattern D no longer proposes ordinary vocabulary as organizations.
4. When Phase 1 DB lookup fails, detection degrades (orgs disabled, person
   proposals capped) instead of running ungrounded at full confidence.
"""

import pytest

import json

from core.lib.graph_rules import sanitize_edge_label, resolve_edge_label
from core.lib.entity_detector import detect_entities, DetectedEntity
from core.skills.backfill_graph import extract_graph_elements
pytestmark = pytest.mark.graph



# ── Fix 2: edge-label echo sanitization ─────────────────────────────────────

def test_sanitize_edge_label_strips_type_echo():
    # The exact labels that polluted the Aug 6 batch
    assert sanitize_edge_label("Pup (animal)") == "Pup"
    assert sanitize_edge_label("Puppy (animal)") == "Puppy"
    assert sanitize_edge_label("Rahul Male Pup Rescuer (person)") == "Rahul Male Pup Rescuer"
    # Quotes and whitespace
    assert sanitize_edge_label('"Pup"') == "Pup"
    assert sanitize_edge_label("'Pup'") == "Pup"
    assert sanitize_edge_label("  Spaced   Out  ") == "Spaced Out"
    assert sanitize_edge_label("") == ""
    assert sanitize_edge_label(None) == ""


def test_resolve_edge_label_matches_detected_case_insensitive():
    detected = {"Pup": "animal", "Danny": "person"}
    assert resolve_edge_label("Pup (animal)", detected) == "Pup"
    assert resolve_edge_label("pup", detected) == "Pup"
    assert resolve_edge_label("Danny", detected) == "Danny"
    # Unknown endpoint -> None (caller must drop the edge, never guess)
    assert resolve_edge_label("Ghost Corp", detected) is None
    assert resolve_edge_label("", detected) is None
    assert resolve_edge_label(None, detected) is None


# ── Fix 3: Pattern D bounds ─────────────────────────────────────────────────

class _MockData:
    def __init__(self, data=None):
        self.data = data or []


class _MockBuilder:
    def __init__(self, fail=False):
        self._fail = fail

    def select(self, *a, **k): return self
    def in_(self, *a, **k): return self
    def neq(self, *a, **k): return self
    def eq(self, *a, **k): return self
    def ilike(self, *a, **k): return self
    def limit(self, *a, **k): return self

    def execute(self):
        if self._fail:
            raise Exception("Server disconnected")
        return _MockData([])


class _MockSupabase:
    def __init__(self, fail=False):
        self._fail = fail

    def table(self, name):
        return _MockBuilder(fail=self._fail)


def _patch_supabase(monkeypatch, fail=False):
    monkeypatch.setattr(
        "core.lib.entity_detector.tenant_aware_client",
        lambda: _MockSupabase(fail=fail),
    )


def test_pattern_d_common_word_never_org(monkeypatch):
    _patch_supabase(monkeypatch)
    # 'Company Evolution' — both are common words that Pattern D used to
    # propose as organizations (Aug 6: Great, Now, Praying, Structure,
    # Evolution, Business).
    ents = detect_entities("They discussed Company Evolution strategy")
    orgs = [e.label for e in ents if e.type == "organization"]
    assert orgs == [], f"common words proposed as orgs: {orgs}"


def test_pattern_d_real_org_still_detected(monkeypatch):
    _patch_supabase(monkeypatch)
    # Legit unregistered org pattern must still fire
    ents = detect_entities("We signed a new client Marutham yesterday")
    orgs = [e.label for e in ents if e.type == "organization"]
    assert "Marutham" in orgs, f"expected Marutham org, got {orgs}"


def test_pattern_d_requires_context_word(monkeypatch):
    _patch_supabase(monkeypatch)
    # No org-context word before the capitalized phrase -> no org
    ents = detect_entities("Qhord released a new feature")
    orgs = [e.label for e in ents if e.type == "organization"]
    assert orgs == [], f"no-context phrase proposed as org: {orgs}"


# ── Fix 4: DB-grounding fail-safe ───────────────────────────────────────────

def test_detect_entities_degrades_when_db_down(monkeypatch):
    calls = []
    # detect_entities imports audit_log_sync locally from core.lib.audit_logger
    monkeypatch.setattr(
        "core.lib.audit_logger.audit_log_sync",
        lambda subsystem, level, msg, **kw: calls.append(msg),
    )
    _patch_supabase(monkeypatch, fail=True)

    ents = detect_entities("I met Joel yesterday and signed client Marutham")
    orgs = [e for e in ents if e.type == "organization"]
    assert orgs == [], f"Pattern D ran ungrounded: {orgs}"
    persons = [e for e in ents if e.type == "person"]
    assert all(e.confidence == 0.4 for e in persons), \
        f"ungrounded person confidence not capped: {persons}"
    assert any("DEGRADED MODE" in m for m in calls), \
        f"no degraded-mode audit event: {calls}"


def test_detect_entities_grounded_person_full_confidence(monkeypatch):
    _patch_supabase(monkeypatch, fail=False)
    ents = detect_entities("I met Joel yesterday")
    persons = [e for e in ents if e.type == "person"]
    assert persons and persons[0].confidence == 0.8, f"expected conf 0.8, got {persons}"


# ── Fix 1 + 2: backfill edge sanitization (extract_graph_elements) ──────────

def test_backfill_edges_sanitized_and_membership_checked(monkeypatch):
    calls = []
    monkeypatch.setattr(
        "core.skills.backfill_graph.audit_log_sync",
        lambda subsystem, level, msg, **kw: calls.append(msg),
    )

    def fake_detect(text):
        return [
            DetectedEntity(label="Pup", type="animal", source="pattern_match", is_new=True),
            DetectedEntity(label="Danny", type="person", source="pattern_match", is_new=True),
        ]
    monkeypatch.setattr("core.lib.entity_detector.detect_entities", fake_detect)

    class FakeResp:
        text = json.dumps([
            {"source": "Pup (animal)", "target": "Danny", "relationship": "OWNS"},
            {"source": "Ghost Corp", "target": "Danny", "relationship": "PARTNER_OF"},
        ])
    monkeypatch.setattr(
        "core.skills.backfill_graph.call_llm_with_fallback_sync",
        lambda **kw: FakeResp(),
    )

    result = extract_graph_elements("text with entities", "mem_1")
    edges = result["edges"]
    # Echo artifact sanitized to canonical 'Pup'; invented endpoint dropped
    assert len(edges) == 1, f"expected 1 kept edge, got {edges}"
    assert edges[0]["source"] == "Pup"
    assert edges[0]["target"] == "Danny"
    assert any("edge_dropped_unresolved" in m and "Ghost Corp" in m for m in calls), \
        f"no dropped-edge audit for Ghost Corp: {calls}"


# ── Fix 1: insert_extracted_entities drops unknown endpoints (real-time) ────

def test_insert_extracted_entities_drops_unknown_edge_endpoints(monkeypatch):
    calls = []

    def mock_audit(subsystem, level, msg, metadata=None, **kw):
        calls.append({"msg": msg, "metadata": metadata})

    monkeypatch.setattr("core.pulse.graph.audit_log_sync", mock_audit)
    monkeypatch.setattr("core.lib.graph_rules.audit_log_sync", mock_audit)

    class MockData:
        def __init__(self, data):
            self.data = data

    class MockBuilder:
        def select(self, *a, **k): return self
        def ilike(self, *a, **k): return self
        def eq(self, *a, **k): return self
        def filter(self, *a, **k): return self
        def limit(self, *a, **k): return self
        def maybe_single(self, *a, **k): return self
        def insert(self, data, **k): return self
        def update(self, data, **k): return self
        def execute(self):
            return MockData([])

    class MockSupabase:
        def table(self, name):
            return MockBuilder()

    monkeypatch.setattr("core.pulse.graph.supabase", MockSupabase())
    monkeypatch.setattr("core.lib.graph_rules.supabase", MockSupabase())

    from core.pulse.graph import insert_extracted_entities
    insert_extracted_entities(
        nodes=[{"label": "father, my wife", "type": "person"}],  # discarded
        edges=[{"source": "Pup (animal)", "target": "Ghost Corp", "relationship": "OWNS"}],
        source_id="123",
        source_type="task",
    )

    # Unknown endpoints are sanitized then SKIPPED at routing (never persisted
    # as concept nodes) — each label gets a label_skipped_no_type audit.
    skipped = [c for c in calls if "label_skipped_no_type" in c["msg"]]
    skipped_labels = {c["msg"] for c in skipped}
    assert len(skipped) == 2, f"expected 2 skipped labels, got {calls}"
    assert any("Pup" in m for m in skipped_labels), f"Pup not skipped: {skipped_labels}"
    assert any("Ghost Corp" in m for m in skipped_labels), f"Ghost Corp not skipped: {skipped_labels}"
    # The echo artifact 'Pup (animal)' must be sanitized to 'Pup' before skip
    assert not any("Pup (animal)" in m for m in skipped_labels), \
        f"echo artifact not sanitized: {skipped_labels}"
    # No routing event should carry these labels (they never got a type)
    routing = [c for c in calls if c["metadata"] and c["metadata"].get("event") == "entity_routing"]
    for c in routing:
        lbl = (c["metadata"].get("label") or "").lower()
        assert "ghost corp" not in lbl and "pup" not in lbl, f"unknown endpoint persisted: {c}"
