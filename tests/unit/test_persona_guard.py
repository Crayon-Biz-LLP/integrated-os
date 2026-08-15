"""Unit tests for the M18 Phase 2A shared persona helpers.

Covers the two helpers every proactive-copy surface now calls:

- ``persona_voice_block()`` — the LLM prompt voice block. Fail-closed: no
  card => ``""`` (prompts stay byte-identical pre-persona).
- ``persona_guard_text()`` — the post-generation never-guard. Rejects copy
  touching a never-topic using the exact G4 verifier matcher
  (word-boundary + inflection-aware on casefolded text), falling back to
  neutral. Fail-closed: no card => text passes through untouched.
"""



from __future__ import annotations
import pytest

from core.services.persona import persona_guard_text, persona_voice_block
pytestmark = pytest.mark.briefing



def make_card(**overrides) -> dict:
    """A minimal valid-shaped persona card (only fields the helpers read)."""
    card: dict = {
        "who": "Danny is the founder of Crayon based in Chennai, India.",
        "style": {"voice": "Direct, warm, no fluff."},
        "never": ["debt", "loan", "stress", "overwhelmed", "feel like living"],
    }
    card.update(overrides)
    return card


# ── persona_voice_block ──────────────────────────────────────────────────


def test_voice_block_empty_without_card():
    assert persona_voice_block() == ""
    assert persona_voice_block(user_name="Danny") == ""


def test_voice_block_omits_style_and_never_when_absent():
    card = make_card(style={"voice": ""}, never=[])
    block = persona_voice_block(user_name="Danny", card=card)
    assert block == (
        " This is Danny's world: Danny is the founder of Crayon based in Chennai, India.."
    )


def test_voice_block_full_shape():
    block = persona_voice_block(user_name="Danny", card=make_card())
    assert "This is Danny's world" in block
    assert "Voice: Direct, warm, no fluff." in block
    assert "Never: debt loan stress overwhelmed feel like living." in block


def test_voice_block_byte_identical_to_briefing_shape():
    """The block must match the pre-refactor briefing.py inline construction."""
    card = make_card()
    block = persona_voice_block(user_name="Danny", card=card)
    who = card["who"]
    style = card["style"]["voice"]
    never = " ".join(card["never"])
    expected = f" This is Danny's world: {who}. Voice: {style}. Never: {never}."
    assert block == expected


def test_voice_block_strips_padded_card_fields():
    """Padded fields normalize to the same byte-string as clean ones.

    Synthesis strips who/style/never at write time; the read path strips
    who/style again, so a padded card can never produce a different prompt
    than a clean one. (Never items are normalized at write time, matching
    the pre-refactor join semantics byte-for-byte.)
    """
    padded = make_card(
        who="  Danny is the founder of Crayon based in Chennai, India.  ",
        style={"voice": "  Direct, warm, no fluff.  "},
    )
    clean = make_card()
    assert persona_voice_block(user_name="Danny", card=padded) == persona_voice_block(
        user_name="Danny", card=clean
    )


def test_voice_block_never_join_matches_pre_refactor():
    """Never joins verbatim (no per-item strip in the read path) — this is
    the exact pre-refactor briefing.py join, kept byte-identical. Items are
    normalized at write time instead."""
    card = make_card(never=[" debt ", "loan", ""])
    block = persona_voice_block(user_name="Danny", card=card)
    assert "Never:  debt  loan ." in block


# ── persona_guard_text ───────────────────────────────────────────────────


def test_guard_passthrough_without_card():
    text = "3 things need your call"
    assert persona_guard_text(text) == text
    assert persona_guard_text(text, card=None, fallback="neutral") == text


def test_guard_passthrough_neutral_copy():
    card = make_card()
    text = "3 things need your call"
    assert persona_guard_text(text, card=card, fallback="neutral") == text


def test_guard_rejects_never_topic():
    card = make_card()
    assert (
        persona_guard_text(
            "Plan for your debt repayment", card=card, fallback="neutral"
        )
        == "neutral"
    )


def test_guard_is_inflection_aware():
    """Same matcher as G4: 'debt' blocks 'debts', 'debt-ridden'."""
    card = make_card(never=["debt"])
    assert (
        persona_guard_text("Your debts are piling up", card=card, fallback="n") == "n"
    )
    assert (
        persona_guard_text("Debt-ridden decisions", card=card, fallback="n") == "n"
    )


def test_guard_is_case_insensitive():
    card = make_card(never=["stress"])
    assert persona_guard_text("STRESS review", card=card, fallback="n") == "n"


def test_guard_matches_multiword_topic():
    card = make_card(never=["feel like living"])
    assert (
        persona_guard_text(
            "Sometimes I feel like living abroad", card=card, fallback="n"
        )
        == "n"
    )


def test_guard_does_not_catch_common_words():
    """Never-topics are precise — common words pass through."""
    card = make_card(never=["debt", "stress"])
    text = "A note about the meeting today"
    assert persona_guard_text(text, card=card, fallback="n") == text


def test_guard_empty_text_passthrough():
    card = make_card()
    assert persona_guard_text("", card=card, fallback="n") == ""


def test_guard_with_explicit_card_bypasses_db():
    """Card is passed explicitly — no tenant scope / DB needed in tests."""
    card = make_card(never=["loan"])
    assert persona_guard_text("Loan approved", card=card, fallback="n") == "n"
    assert (
        persona_guard_text("Board is clear", card=card, fallback="n") == "Board is clear"
    )
