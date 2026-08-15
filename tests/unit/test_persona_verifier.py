"""Regression tests for the M18 persona verifier (G1-G4 grounding gates).

These are the tests that make the "FC Madras prayer group" class of
fabrication a CI failure instead of a discovery: every planted claim must
be REJECTED, and a genuinely grounded card must PASS.
"""

import pytest


import os
pytestmark = pytest.mark.briefing


os.environ.setdefault("SUPABASE_URL", "http://localhost:1")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "test-key")

from core.services.persona_verifier import verify_persona_card  # noqa: E402


# ── A grounded fact bundle (mirrors extract_facts output shape) ─────────────
def make_facts():
    return {
        "context": "Danny (Yashwant Daniel), founder of Crayon, Chennai, India.",
        "domains": ["Qhord", "Solvstrat", "Ashraya", "Personal", "Atna"],
        "life_snapshot": [
            "Sunjula Daniel (spouse)",
            "Amma (family)",
            "Yesterday was our 12th wedding anniversary, full of emotions.",
            "90-Day Prayer: Ranjit ABC: Ranjit shared a reflection on finding joy in the Lord",
        ],
        "allowed_names": {
            "danny", "yashwant daniel", "sunjula daniel", "marcus durai",
            "amita", "qhord", "ashraya", "solvstrat", "crayon",
            "fc madras", "armour cyber", "equisoft", "armour",
        },
        "known_triples": {
            ("danny", "WORKS_ON", "qhord"),
            ("danny", "WORKS_ON", "solvstrat"),
            ("danny", "FAMILY_OF", "sunjula daniel"),
            ("danny", "WORKS_ON", "crayon"),
            ("danny", "MEMBER_OF", "ashraya"),
            ("amita", "WORKS_ON", "fc madras"),
            ("danny", "MET_WITH", "amita"),
            ("danny", "WORKS_ON", "armour cyber"),
            ("danny", "WORKS_ON", "equisoft"),
        },
        "root_label": "Danny",
        "sensitive_topics": ["debt", "loan"],
    }


def valid_card():
    return {
        "schema_version": 1,
        "who": "Danny (Yashwant Daniel), founder of Crayon, Chennai.",
        "people": ["Sunjula Daniel", "Marcus Durai", "Amita"],
        "domains": ["Qhord", "Solvstrat", "Ashraya"],
        "style": {"voice": "direct, no fluff; warm but not chatty"},
        "signoffs": [
            "Go be a dad, Danny.",
            "Night, Danny.",
            "Rest well, Danny.",
        ],
        "claims": [
            {"subject": "Danny", "predicate": "WORKS_ON", "object": "Qhord"},
            {"subject": "Danny", "predicate": "FAMILY_OF", "object": "Sunjula Daniel"},
            {"subject": "Danny", "predicate": "MEMBER_OF", "object": "Ashraya"},
        ],
        "never": ["money", "debt"],
    }


def test_valid_grounded_card_passes():
    ok, errors = verify_persona_card(valid_card(), make_facts())
    assert ok, errors


def test_g1_fact_fusion_rejected():
    """The FC Madras class: 'Prayer group meets at FC Madras' — no such edge."""
    card = valid_card()
    card["claims"] = [
        {"subject": "Prayer group", "predicate": "MEETS_AT", "object": "FC Madras"}
    ]
    ok, errors = verify_persona_card(card, make_facts())
    assert not ok
    assert any("G3" in e for e in errors)


def test_g1_unknown_entity_rejected():
    card = valid_card()
    card["who"] = "Danny, founder of Zorp Dynamics."  # Zorp not in the graph
    ok, errors = verify_persona_card(card, make_facts())
    assert not ok
    assert any("G1" in e for e in errors)


def test_g1_fabricated_people_entry_rejected():
    """people/domains arrays must hold known entities, not inventions."""
    card = valid_card()
    card["people"] = ["Sunjula Daniel", "Zorp Corp"]  # Zorp is fabricated
    ok, errors = verify_persona_card(card, make_facts())
    assert not ok
    assert any("G1" in e and "people/domains" in e for e in errors)


def test_g2_timing_claim_rejected():
    card = valid_card()
    card["signoffs"] = ["Prayer group is tonight, Danny.", "Rest well, Danny."]
    ok, errors = verify_persona_card(card, make_facts())
    assert not ok
    assert any("G2" in e for e in errors)


def test_g2_timing_in_who_rejected():
    card = valid_card()
    card["who"] = "Danny, meeting Armour Cyber next week."
    ok, errors = verify_persona_card(card, make_facts())
    assert not ok
    assert any("G2" in e for e in errors)


def test_g3_association_without_edge_rejected():
    """Claim asserts a link between two known entities that share no edge."""
    card = valid_card()
    card["claims"] = [
        {"subject": "Amita", "predicate": "FAMILY_OF", "object": "Sunjula Daniel"}
    ]
    ok, errors = verify_persona_card(card, make_facts())
    assert not ok
    assert any("G3" in e for e in errors)


def test_g4_sensitive_topic_in_prose_rejected():
    card = valid_card()
    card["style"] = {"voice": "playful about debt and loans"}
    ok, errors = verify_persona_card(card, make_facts())
    assert not ok
    assert any("G4" in e for e in errors)


def test_signoff_name_drop_rejected():
    """Sign-offs may reference the user's own name only."""
    card = valid_card()
    card["signoffs"] = ["Call Amita about FC Madras, Danny.", "Rest well, Danny."]
    ok, errors = verify_persona_card(card, make_facts())
    assert not ok
    assert any("name-drop" in e for e in errors)


def test_missing_required_key_rejected():
    card = valid_card()
    del card["claims"]
    ok, errors = verify_persona_card(card, make_facts())
    assert not ok
    assert any("missing required key" in e for e in errors)


def test_direction_insensitive_triple_accepted():
    """A claim phrased object-first is still grounded (edge exists either way)."""
    card = valid_card()
    card["claims"] = [
        {"subject": "Sunjula Daniel", "predicate": "FAMILY_OF", "object": "Danny"}
    ]
    ok, errors = verify_persona_card(card, make_facts())
    assert ok, errors


def test_signoff_sentence_start_words_pass():
    """Common sign-off openers ('Take care', 'Best') are not entities."""
    card = valid_card()
    card["signoffs"] = ["Take care, Danny.", "Best, Danny.", "Rest well, Danny."]
    ok, errors = verify_persona_card(card, make_facts())
    assert ok, errors


def test_domain_names_allowed_in_domains_array():
    """Routing domain names from settings are source rows, not fabrications."""
    card = valid_card()
    card["domains"] = ["Qhord", "Solvstrat", "Ashraya", "Personal", "Atna"]
    ok, errors = verify_persona_card(card, make_facts())
    assert ok, errors


def test_sentence_start_words_in_voice_pass():
    """'Maintain a calm tone' — sentence-opening words are not entities."""
    card = valid_card()
    card["style"] = {"voice": "Maintain a calm, steady tone. Warmly direct."}
    card["signoffs"] = ["Warmly, Danny.", "Take care, Danny.", "Rest well, Danny."]
    ok, errors = verify_persona_card(card, make_facts())
    assert ok, errors


def test_mid_sentence_fabrication_still_rejected():
    """The sentence-start exemption must not weaken mid-sentence checks."""
    card = valid_card()
    card["who"] = "Danny, a founder of Crayon and partner at Zorp Dynamics."
    ok, errors = verify_persona_card(card, make_facts())
    assert not ok
    assert any("G1" in e for e in errors)


def test_life_snapshot_valid_passes():
    """Grounded life facts (roles + quoted memory sentences) are accepted."""
    card = valid_card()
    card["life_snapshot"] = [
        "Sunjula Daniel (spouse)",
        "Yesterday was our 12th wedding anniversary, full of emotions.",
        "90-Day Prayer: Ranjit ABC: Ranjit shared a reflection on finding joy in the Lord",
    ]
    ok, errors = verify_persona_card(card, make_facts())
    assert ok, errors


def test_life_snapshot_source_name_allowed():
    """Names from the provided snapshot facts are grounded, not fabrications."""
    card = valid_card()
    card["life_snapshot"] = [
        "Amma (family)",
        "Ranjit shared a reflection on finding joy in the Lord",
    ]
    ok, errors = verify_persona_card(card, make_facts())
    assert ok, errors


def test_life_snapshot_unknown_name_rejected():
    card = valid_card()
    card["life_snapshot"] = ["Zorp Corp (family)"]
    ok, errors = verify_persona_card(card, make_facts())
    assert not ok
    assert any("G1" in e and "life_snapshot" in e for e in errors)


def test_life_snapshot_sensitive_topic_rejected():
    """The boundary wins over texture: no debt-talk in the snapshot."""
    card = valid_card()
    card["life_snapshot"] = ["I have been so overwhelmed with my debt situation."]
    ok, errors = verify_persona_card(card, make_facts())
    assert not ok
    assert any("G4" in e and "life_snapshot" in e for e in errors)
