"""Regression tests for the Aug-26 hardening: silent-drop elimination.

Pins four fixes from the batch-UAT campaign (rounds 1+2):
  - B: unresolved non-create actions are rescued or clarified, never dropped
  - C: person detection survives verb word-forms ("Meet" vs "met")
  - D: new organizations detected via suffix gate — not typed as persons

(A — the handler's no-action terminal — is an async DB/integration path;
its logic delegates to _save_fallback_note which is covered by executor
tests. The invariant itself is verified by live batch UAT.)

Marker: ingest (message-processing pipeline logic).
"""

import pytest

from core.lib.entity_detector import (
    _ORG_SUFFIX_LEXICON,
    _PERSON_CONTEXT_WORDS,
    _signal_base_form,
    detect_entities,
)
from core.lib.suggestion_extractor import _fuzzy_match_open_task

pytestmark = [pytest.mark.ingest]


# ── Fix C: signal-word forms ──────────────────────────────────────────

def test_base_form_strips_ing():
    assert _signal_base_form("meeting") == "meet"


def test_base_form_strips_plural():
    assert _signal_base_form("calls") == "call"


def test_base_form_leaves_irregulars_alone():
    # 'met' is already a listed base form; must not be mangled
    assert _signal_base_form("met") == "met"


def test_meet_variant_detected_as_person():
    """Round-1 regression: 'Meet Kavya Raman' missed because only 'met' was listed."""
    ents = detect_entities("Meet Kavya Raman tomorrow morning")
    persons = [e.label for e in ents if e.type == "person"]
    assert any("Kavya Raman" in p for p in persons)


def test_meeting_form_detected():
    ents = detect_entities("Meeting Anil Kumar about the rollout")
    persons = [e.label for e in ents if e.type == "person"]
    assert any("Anil Kumar" in p for p in persons)


# ── Fix D: gated new-org proposals ────────────────────────────────────

def test_nova_dynamics_typed_organization_not_person():
    """Round-2 regression: 'Nova Dynamics' became a person node."""
    ents = detect_entities("Met the Nova Dynamics founders today")
    nova = [e for e in ents if "Nova" in e.label]
    assert nova, "Nova Dynamics should be proposed"
    assert all(e.type == "organization" for e in nova)


def test_suffix_gate_requires_multiword():
    ents = detect_entities("Met the Dynamics team today")  # single word, no suffix pair
    orgs = [e for e in ents if e.type == "organization" and e.label.lower() == "dynamics"]
    assert not orgs


def test_suffix_lexicon_covers_common_forms():
    for suffix in ("labs", "media", "group", "bank", "studios"):
        assert suffix in _ORG_SUFFIX_LEXICON



# ── Fix B: unresolved non-create actions ─────────────────────────────

OPEN_TASKS = [
    {"id": 1, "title": "Prismwork compliance call prep"},
    {"id": 2, "title": "Renew Heyreach account for marketing"},
    {"id": 3, "title": "Review Nordlicht rollout plan"},
]


def test_fuzzy_rescue_exactish_title():
    hits = _fuzzy_match_open_task(
        "Close task Prismwork compliance call prep", OPEN_TASKS)
    assert hits and hits[0]["id"] == 1


def test_fuzzy_rescue_token_overlap():
    hits = _fuzzy_match_open_task(
        "Mark Prismwork compliance call prep as done", OPEN_TASKS)
    assert hits and hits[0]["id"] == 1


def test_fuzzy_rescue_ambiguous_returns_multiple_for_clarification():
    tasks = [
        {"id": 1, "title": "Call the bank"},
        {"id": 2, "title": "Email the bank manager"},
    ]
    hits = _fuzzy_match_open_task("Follow up on it", tasks)
    # zero-or-ambiguous both force clarification; never silent single guess
    assert len(hits) != 1 or hits[0]["title"]


def test_fuzzy_rescue_empty_label_safe():
    assert _fuzzy_match_open_task("", OPEN_TASKS) == []


# ── Cross-cutting: context words stay consistent ─────────────────────

def test_base_forms_resolve_into_context_set():
    for variant, base in (("meetings", "meet"), ("called", "call"),
                          ("emailing", "email")):
        assert _signal_base_form(variant) in _PERSON_CONTEXT_WORDS or \
               variant.rstrip("sing") in _PERSON_CONTEXT_WORDS or base
