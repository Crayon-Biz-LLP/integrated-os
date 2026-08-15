"""Unit tests for the M18a curated-people fix.

Root cause of the fix: the persona synthesis mined noisy graph edges for
the life circle (675 edges off the root, pulling in FAMILY_OF/FRIEND_OF
nodes like Amma and Binu), while the tenant's OWN curated "relationships"
config row (\"FAMILY: Sunju (Wife), Jeremy (8), Jaden (5)\") was ignored.

The fix: curated row wins -> graph is only the fallback. These tests pin
the parser, the role cleaning, and the verifier's acceptance of curated
names as source-row vocabulary.
"""



from __future__ import annotations
import pytest

from core.services.persona_verifier import verify_persona_card
from core.services.user_settings import (
    _clean_relationship_role,
    _parse_relationships_row,
)
pytestmark = pytest.mark.graph



# ── _parse_relationships_row ─────────────────────────────────────────────


def test_parse_danny_style_row():
    row = (
        "\n\nKEY PEOPLE CONTEXT:\n"
        "1. FAMILY: Sunju (Wife - URGENT/Connection), Jeremy (8), Jaden (5), Jeffery (8mo).\n"
        "2. PROFESSIONAL: Team leads at Solvstrat.\n"
    )
    parsed = _parse_relationships_row(row)
    assert {"name": "Sunju", "role": "wife", "section": "family"} in parsed
    assert {"name": "Jeremy", "role": "family", "section": "family"} in parsed
    assert {"name": "Jaden", "role": "family", "section": "family"} in parsed
    # Regression: the trailing period after "(8mo)." must not drop the
    # last entry — Jeffery survives the delimiter.
    assert {"name": "Jeffery", "role": "family", "section": "family"} in parsed
    # Bare plural line still parses (name kept), role = section.
    assert any(p["name"] == "Team leads at Solvstrat" for p in parsed)
    # Work sections are parsed but never become life texture (synthesis
    # filters them) — see test_work_sections_never_life_texture.
    # No Amma/Binu unless the tenant wrote them — curated wins.


def test_parse_newline_continued_section():
    """A section body may continue across lines — every line is parsed
    under the same section (hardening: one name per line)."""
    row = "FAMILY:\nSunju (Wife)\nJeremy (8)\nJaden (5)"
    parsed = _parse_relationships_row(row)
    assert {"name": "Sunju", "role": "wife", "section": "family"} in parsed
    assert {"name": "Jeremy", "role": "family", "section": "family"} in parsed
    assert {"name": "Jaden", "role": "family", "section": "family"} in parsed


def test_parse_connector_words_not_polluting_names():
    """'and'/'plus'/'&' between entries must not become part of a name
    (hardening: "…Sunju (Wife), and Jeremy (8)" keeps Jeremy clean)."""
    row = "FAMILY: Sunju (Wife), and Jeremy (8)"
    parsed = _parse_relationships_row(row)
    assert {"name": "Sunju", "role": "wife", "section": "family"} in parsed
    assert {"name": "Jeremy", "role": "family", "section": "family"} in parsed
    assert not any("and" in (p["name"] or "").lower() for p in parsed)


def test_parse_connector_without_comma_keeps_both():
    """A connector separating entries WITHOUT a comma must not drop the
    preceding entry — "Sunju (Wife) and Jeremy (8)" keeps both."""
    row = "FAMILY: Sunju (Wife) and Jeremy (8)"
    parsed = _parse_relationships_row(row)
    assert {"name": "Sunju", "role": "wife", "section": "family"} in parsed
    assert {"name": "Jeremy", "role": "family", "section": "family"} in parsed


def test_parse_ampersand_without_comma_keeps_both():
    row = "FAMILY: Sunju (Wife) & Jeremy (8)"
    parsed = _parse_relationships_row(row)
    assert {"name": "Sunju", "role": "wife", "section": "family"} in parsed
    assert {"name": "Jeremy", "role": "family", "section": "family"} in parsed


def test_parse_bare_names_without_parens():
    row = "FAMILY: Sunjula Daniel, Jeremy"
    parsed = _parse_relationships_row(row)
    assert {"name": "Sunjula Daniel", "role": "family", "section": "family"} in parsed
    assert {"name": "Jeremy", "role": "family", "section": "family"} in parsed


def test_parse_empty_and_garbage():
    assert _parse_relationships_row("") == []
    assert _parse_relationships_row("no colon here") == []
    assert _parse_relationships_row("FAMILY:") == []


def test_parse_section_numbering_stripped():
    row = "1. FAMILY: Sunju (Wife)"
    parsed = _parse_relationships_row(row)
    assert parsed and parsed[0]["section"] == "family"
    assert parsed[0]["role"] == "wife"


def test_work_sections_never_life_texture():
    """The synthesis-side section filter (mirrored constant) drops work."""
    from core.skills.persona_synthesis import _CURATED_WORK_SECTIONS

    assert "professional" in _CURATED_WORK_SECTIONS
    assert "team" in _CURATED_WORK_SECTIONS
    assert "work" in _CURATED_WORK_SECTIONS
    assert "family" not in _CURATED_WORK_SECTIONS
    assert "friends" not in _CURATED_WORK_SECTIONS


# ── _clean_relationship_role ─────────────────────────────────────────────


def test_clean_role_strips_annotations():
    assert _clean_relationship_role("Wife - URGENT/Connection", "family") == "wife"
    assert _clean_relationship_role("Wife", "family") == "wife"
    assert _clean_relationship_role("Husband - primary", "family") == "husband"
    # Em/en-dash separators too (prose the tenant wrote).
    assert _clean_relationship_role("Wife — primary", "family") == "wife"
    assert _clean_relationship_role("Wife – primary", "family") == "wife"


def test_clean_role_ages_collapse_to_section():
    assert _clean_relationship_role("8", "family") == "family"
    assert _clean_relationship_role("8mo", "family") == "family"


def test_clean_role_empty_collapses_to_section():
    assert _clean_relationship_role("", "family") == "family"
    assert _clean_relationship_role("  ", "personal") == "personal"


# ── Verifier: curated names are source-row vocabulary ────────────────────


def _facts(allowed_names=(), life_snapshot=()):
    return {
        "allowed_names": set(allowed_names) | {"danny"},
        "known_triples": set(),
        "root_label": "Danny",
        "sensitive_topics": ["debt"],
        "context": "Danny (Yashwant Daniel), founder of Crayon in Chennai, India.",
        "domains": ["Personal"],
        "life_snapshot": list(life_snapshot),
    }


def test_curated_kids_accepted_in_life_snapshot():
    """Curated names without graph nodes still pass G1 (they are source rows)."""
    facts = _facts(
        allowed_names=["sunjula daniel"],
        life_snapshot=["Sunju (spouse)", "Jeremy (child)"],
    )
    card = {
        "who": "Danny is a founder in Chennai.",
        "people": ["Sunjula Daniel"],
        "domains": ["Personal"],
        "style": {"voice": "Direct."},
        "signoffs": ["Rest well.", "Take good care of your family."],
        "claims": [],
        "never": ["debt"],
        "life_snapshot": ["Sunjula Daniel (spouse)", "Jeremy (child)"],
    }
    ok, errors = verify_persona_card(card, facts)
    assert ok, errors


def test_curated_name_not_in_vocabulary_rejected():
    """A fabricated name is still rejected even with a curated row present."""
    facts = _facts(
        allowed_names=["sunjula daniel"],
        life_snapshot=["Sunju (spouse)"],
    )
    card = {
        "who": "Danny is a founder in Chennai.",
        "people": ["Sunjula Daniel"],
        "domains": ["Personal"],
        "style": {"voice": "Direct."},
        "signoffs": ["Rest well.", "Take good care of your family."],
        "claims": [],
        "never": ["debt"],
        "life_snapshot": ["MadeUp Person (friend)"],
    }
    ok, errors = verify_persona_card(card, facts)
    assert not ok
    assert any("unknown entity" in e for e in errors)


def test_curated_sensitive_boundary_still_holds():
    """G4 applies to curated life_snapshot entries too — the boundary wins."""
    facts = _facts(
        allowed_names=["sunjula daniel"],
        life_snapshot=["Sunju (spouse)"],
        # sensitive_topics includes debt; a curated entry mentioning it is
        # rejected even though the name is legitimately sourced.
    )
    card = {
        "who": "Danny is a founder in Chennai.",
        "people": ["Sunjula Daniel"],
        "domains": ["Personal"],
        "style": {"voice": "Direct."},
        "signoffs": ["Rest well.", "Take good care of your family."],
        "claims": [],
        "never": ["debt"],
        "life_snapshot": ["Sunjula Daniel and the debt plan (spouse)"],
    }
    ok, errors = verify_persona_card(card, facts)
    assert not ok
    assert any("G4" in e for e in errors)
