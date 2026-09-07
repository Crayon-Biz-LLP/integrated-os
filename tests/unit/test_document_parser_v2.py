"""Document Intelligence v2: Gemini-native playbook + critic parser.

Aspect marker: app (user-facing document suggestion card).

Covers:
  1. _build_breakdown — critic filtering (unsupported dropped), recovered-miss
     inclusion (flagged), blank-row guard, mapping to executor-action shape.
  2. _step_to_action — implied_type mapping (event/task/note → create_*),
     params carrying description/deadline/evidence_quote.
  3. _kind_to_node_type — person/organization pass through, product/other →
     concept.
  4. parse_document fallback chain — native failure falls back to the legacy
     text path; legacy path still populates suggested_entities/entity_context.

No network: call_gemini is stubbed; parse flows run on canned playbook/critic
JSON mirroring the live prototype outputs (Kron / Cricket PDFs).
"""
import pytest

from core.webhook import document_parser as dp

pytestmark = pytest.mark.app


# ── fixtures: canned stage outputs (mirroring the validated prototype runs) ──

STAGE1_KRON = {
    "document_type": "proposal",
    "one_line_purpose": "Solvstrat proposes a modular microservices platform for Kron Technologies.",
    "explicit_ask": "We propose a focused 30-minute alignment session with your core team",
    "next_steps": [
        {
            "action": "Schedule a 30-minute alignment session with Solvstrat.",
            "detail": "Validate strategic baselines and architectural fit with the core team.",
            "implied_type": "event",
            "evidence_quote": "We propose a focused 30-minute alignment session with your core team",
        },
        {
            "action": "Compare strategic baselines against internal roadmap.",
            "detail": "Validate whether the modular approach aligns with internal mappings.",
            "implied_type": "task",
            "evidence_quote": "Compare Strategic Baselines: Validate whether this modular approach aligns...",
        },
        {
            "action": "Fabricated: wire the office ping-pong table.",
            "detail": "Not in the document.",
            "implied_type": "task",
            "evidence_quote": "",
        },
    ],
    "entities": [
        {"name": "Solvstrat", "role": "vendor", "kind": "organization"},
        {"name": "Kron Technologies", "role": "client", "kind": "organization"},
        {"name": "Katana", "role": "manufacturing tool to replace", "kind": "product"},
    ],
}

CRITIC_KRON = {
    "verdicts": [
        {"action": "Schedule a 30-minute alignment session with Solvstrat.", "supported": True, "reason": "Section 5"},
        {"action": "Compare strategic baselines against internal roadmap.", "supported": True, "reason": "Section 5"},
        {"action": "Fabricated: wire the office ping-pong table.", "supported": False, "reason": "not in document"},
    ],
    "missed_actions": [
        {
            "action": "Explore Architectural Fit — discuss rollout scope.",
            "implied_type": "task",
            "evidence_quote": "Explore Architectural Fit: Discuss key technical preferences",
        }
    ],
    "fabrications": ["wire the office ping-pong table"],
}


# ── 1. _build_breakdown ───────────────────────────────────────────────────────

class TestBuildBreakdown:
    def test_critic_unsupported_step_dropped(self):
        bd = dp._build_breakdown(STAGE1_KRON, CRITIC_KRON, entities=[])
        labels = [a["human_label"] for a in bd["suggested_actions"]]
        assert "Fabricated: wire the office ping-pong table." not in labels

    def test_supported_steps_kept(self):
        bd = dp._build_breakdown(STAGE1_KRON, CRITIC_KRON, entities=[])
        labels = [a["human_label"] for a in bd["suggested_actions"]]
        assert "Schedule a 30-minute alignment session with Solvstrat." in labels
        assert "Compare strategic baselines against internal roadmap." in labels

    def test_recovered_miss_included_and_flagged(self):
        bd = dp._build_breakdown(STAGE1_KRON, CRITIC_KRON, entities=[])
        recovered = [a for a in bd["suggested_actions"]
                     if a["human_label"] == "Explore Architectural Fit — discuss rollout scope."]
        assert len(recovered) == 1
        assert recovered[0]["confidence"] == 0.85  # critic-sourced flag

    def test_blank_row_guard(self):
        bd = dp._build_breakdown(
            {"document_type": "x", "one_line_purpose": "p", "explicit_ask": "none",
             "next_steps": [{"action": "", "implied_type": "task"}, {"action": "  ", "implied_type": "task"}]},
            None, entities=[],
        )
        assert bd["suggested_actions"] == []

    def test_no_critic_keeps_all_steps(self):
        bd = dp._build_breakdown(STAGE1_KRON, None, entities=[])
        assert len(bd["suggested_actions"]) == 3  # fabricated kept when unverified

    def test_intelligence_metadata(self):
        bd = dp._build_breakdown(STAGE1_KRON, CRITIC_KRON, entities=[])
        intel = bd["intelligence"]
        assert intel["model"] == dp.DOCUMENT_MODEL
        assert intel["critic_recovered_actions"] == 1
        assert intel["critic_rejected_actions"] == ["Fabricated: wire the office ping-pong table."]
        assert intel["fabrications"] == ["wire the office ping-pong table"]

    def test_card_payload_shape(self):
        """The Flutter SuggestionCard contract: document_type/summary/suggested_actions
        with human_label/params — the keys its _parseItems reads."""
        bd = dp._build_breakdown(STAGE1_KRON, CRITIC_KRON, entities=[])
        assert set(bd) >= {"document_type", "summary", "suggested_actions", "suggested_entities"}
        for a in bd["suggested_actions"]:
            assert a.get("human_label")
            assert a.get("operation", "").startswith("create_")


# ── 2. _step_to_action mapping ────────────────────────────────────────────────

class TestStepToAction:
    def test_event_mapping(self):
        a = dp._step_to_action({"action": "Book the session", "implied_type": "event",
                                "detail": "30 min", "when": "Phase 1", "evidence_quote": "q"})
        assert a["operation"] == "create_event"
        assert a["params"]["description"] == "30 min"
        assert a["params"]["deadline"] == "Phase 1"
        assert a["params"]["evidence_quote"] == "q"

    def test_task_mapping(self):
        a = dp._step_to_action({"action": "Do the thing", "implied_type": "task"})
        assert a["operation"] == "create_task"

    def test_note_mapping_content(self):
        a = dp._step_to_action({"action": "Remember this", "implied_type": "note", "detail": "the fact"})
        assert a["operation"] == "create_note"
        assert a["params"]["content"] == "the fact"
        assert "description" not in a["params"]

    def test_invalid_type_defaults_task(self):
        a = dp._step_to_action({"action": "X", "implied_type": "banana"})
        assert a["operation"] == "create_task"


# ── 3. entity kind mapping ────────────────────────────────────────────────────

class TestKindToNodeType:
    def test_person_passthrough(self):
        assert dp._kind_to_node_type("person") == "person"

    def test_organization_passthrough(self):
        assert dp._kind_to_node_type("organization") == "organization"

    def test_product_maps_concept(self):
        assert dp._kind_to_node_type("product") == "concept"

    def test_other_maps_concept(self):
        assert dp._kind_to_node_type("other") == "concept"


# ── 4. parse_document fallback chain (network stubbed) ────────────────────────

class TestParseDocumentFallback:
    def test_native_failure_falls_back_to_text_path(self, monkeypatch):
        async def _boom(*a, **k):
            raise RuntimeError("model down")
        monkeypatch.setattr(dp, "_playbook_pass", _boom)

        captured = {}

        async def _fake_text_path(text):
            captured["text"] = text
            return {"document_type": "message", "summary": "s",
                    "suggested_actions": [], "suggested_entities": [],
                    "entity_context": {"source_text": text}}

        monkeypatch.setattr(dp, "_parse_document_text", _fake_text_path)
        import asyncio
        out = asyncio.run(dp.parse_document("doc text", pdf_bytes=b"%PDF-broken"))
        assert out["entity_context"]["source_text"] == "doc text"
        assert captured["text"] == "doc text"

    def test_no_bytes_goes_straight_to_text_path(self, monkeypatch):
        called = {"native": False}

        async def _no_native(*a, **k):
            called["native"] = True
            return {}

        async def _fake_text_path(text):
            return {"document_type": "message", "summary": "s",
                    "suggested_actions": [], "suggested_entities": []}

        monkeypatch.setattr(dp, "_playbook_pass", _no_native)
        monkeypatch.setattr(dp, "_parse_document_text", _fake_text_path)
        import asyncio
        out = asyncio.run(dp.parse_document("doc text", pdf_bytes=None))
        assert called["native"] is False
        assert out["document_type"] == "message"

    def test_non_pdf_mime_goes_to_text_path(self, monkeypatch):
        async def _no_native(*a, **k):
            raise AssertionError("native must not run for non-PDF")
        monkeypatch.setattr(dp, "_playbook_pass", _no_native)

        async def _fake_text_path(text):
            return {"document_type": "message", "summary": "s",
                    "suggested_actions": [], "suggested_entities": []}

        monkeypatch.setattr(dp, "_parse_document_text", _fake_text_path)
        import asyncio
        asyncio.run(dp.parse_document("doc text", pdf_bytes=b"x", mime_type="application/msword"))

    def test_native_success_path_returns_breakdown(self, monkeypatch):
        async def _fake_playbook(pdf_bytes):
            return STAGE1_KRON

        async def _fake_critic(doc_text, stage1):
            return CRITIC_KRON

        monkeypatch.setattr(dp, "_playbook_pass", _fake_playbook)
        monkeypatch.setattr(dp, "_critic_pass", _fake_critic)
        import asyncio
        out = asyncio.run(dp.parse_document("doc text", pdf_bytes=b"%PDF-real"))
        assert out["document_type"] == "proposal"
        labels = [a["human_label"] for a in out["suggested_actions"]]
        # fabricated dropped, recovered miss present
        assert "Fabricated: wire the office ping-pong table." not in labels
        assert "Explore Architectural Fit — discuss rollout scope." in labels
        # entities mapped: product → concept, with role carried
        kinds = {e["label"]: e["type"] for e in out["suggested_entities"]}
        assert kinds["Katana"] == "concept"
        assert kinds["Solvstrat"] == "organization"
        roles = {e["label"]: e.get("role") for e in out["suggested_entities"]}
        assert roles["Kron Technologies"] == "client"
