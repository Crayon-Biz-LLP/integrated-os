"""Phase 2B Step 0 tests: surface summary (R4) + message composer (R2/R3).

Proves, per the hardened test matrix:

- (a) fail-closed byte-identical — no persona summary => the composer returns
  today's exact strings (R5).
- (c) guard coverage — every template's persona form passes the never-guard;
  a planted never-topic anywhere in the persona form collapses to neutral (R3).
- R4 — the surface summary carries only the closed-enum token + display name +
  signoffs; never the raw card, curated people, or never-topics.
"""

from __future__ import annotations

import pytest

from core.services import message_voice
from core.services.persona import persona_surface_summary


def make_card(**overrides) -> dict:
    """A minimal valid-shaped persona card (fields the summary/composer read)."""
    card: dict = {
        "who": "Danny is the founder of Crayon based in Chennai, India.",
        "style": {"voice": "Direct, warm, no fluff."},
        "signoffs": [
            "Wishing you peace and joy.",
            "May your day be filled with grace.",
        ],
        "never": ["debt", "loan", "stress", "overwhelmed"],
    }
    card.update(overrides)
    return card


SUMMARY = {
    "display_name": "Danny",
    "voice_style": "direct",
    "signoffs": ["Wishing you peace and joy."],
}


# ── matrix (a): fail-closed byte-identical (R5) ──────────────────────────


def test_no_summary_returns_neutral_done():
    assert message_voice.compose_done(None) == "Action completed"


def test_no_summary_returns_neutral_snoozed():
    assert (
        message_voice.compose_snoozed(None, 3)
        == "Dismissed — I'll keep it off your board for now"
    )


def test_no_summary_returns_neutral_corrected():
    assert (
        message_voice.compose_corrected(None)
        == "Correction recorded — Rhodey will learn from this."
    )


def test_push_neutral_without_name():
    assert message_voice.compose_push_title(3) == "3 things need your call"
    assert message_voice.compose_push_body([]) == "Something needs your call"


# ── persona forms ─────────────────────────────────────────────────────────


def test_done_persona_form():
    assert message_voice.compose_done(SUMMARY) == "Done, Danny — off your board."


def test_snoozed_persona_form_plural():
    assert "Snoozed for 3 days" in message_voice.compose_snoozed(SUMMARY, 3)


def test_snoozed_persona_form_singular():
    assert "Snoozed for 1 day" in message_voice.compose_snoozed(SUMMARY, 1)


def test_corrected_persona_form():
    out = message_voice.compose_corrected(SUMMARY)
    assert "Got it, Danny" in out
    assert "Rhodey will learn from this." in out


def test_push_name_opener_is_identity_not_card():
    """The push opener keys off the tenant NAME (as decision_pulse does today),
    never the persona summary — card-less tenants stay byte-identical (R5)."""
    assert message_voice.compose_push_title(3, name="Danny") == (
        "Danny, 3 things need your call"
    )
    assert message_voice.compose_push_body(["graph edge"]) == (
        "From graph edge — want a look?"
    )
    assert message_voice.compose_push_body([]) == "Something needs your call"


def test_persona_form_without_name_uses_neutral_shape():
    summary = {"display_name": "", "voice_style": "direct", "signoffs": []}
    assert message_voice.compose_done(summary) == "Done — off your board."


# ── matrix (c): guard coverage on every template (R3) ─────────────────────


@pytest.mark.parametrize(
    "compose, never_topic, neutral",
    [
        (
            lambda card: message_voice.compose_done(SUMMARY, card=card),
            ["board"],
            "Action completed",
        ),
        (
            lambda card: message_voice.compose_snoozed(SUMMARY, 3, card=card),
            ["ready"],
            "Dismissed — I'll keep it off your board for now",
        ),
        (
            lambda card: message_voice.compose_corrected(SUMMARY, card=card),
            ["corrected"],
            "Correction recorded — Rhodey will learn from this.",
        ),
        (
            lambda card: message_voice.compose_push_title(3, name="Danny", card=card),
            ["call"],
            "3 things need your call",
        ),
        (
            lambda card: message_voice.compose_push_body(["graph edge"], card=card),
            ["look"],
            "Something needs your call",
        ),
    ],
)
def test_guard_collapses_never_topic(compose, never_topic, neutral):
    """A never-topic in the composed form must collapse to the neutral form."""
    card = make_card(never=never_topic)
    assert compose(card) == neutral


def test_guard_passes_clean_persona_form():
    """A persona form with no never-topic passes through unguarded."""
    card = make_card(never=["debt", "loan", "stress"])
    out = message_voice.compose_done(SUMMARY, card=card)
    assert out == "Done, Danny — off your board."





# ── R4: surface summary shape + fail-closed ───────────────────────────────


def test_summary_fail_closed_no_card(monkeypatch):
    monkeypatch.setattr("core.services.persona.resolve_persona", lambda uid=None: None)
    assert persona_surface_summary() is None


def test_summary_shape_and_no_leaks(monkeypatch):
    card = make_card()
    monkeypatch.setattr("core.services.persona.resolve_persona", lambda uid=None: card)
    monkeypatch.setattr(
        "core.services.user_settings.resolve_user_name", lambda uid=None: "Danny"
    )
    summary = persona_surface_summary()
    assert set(summary.keys()) == {"display_name", "voice_style", "signoffs"}
    assert summary["display_name"] == "Danny"
    assert summary["voice_style"] in {"direct", "calm", "warm"}
    assert summary["signoffs"] == card["signoffs"][:2]
    # The raw card, curated people, and never-topics never leave the server.
    for forbidden in ("never", "people", "who", "style", "life_snapshot", "claims"):
        assert forbidden not in summary


def test_summary_caps_signoffs_at_two(monkeypatch):
    card = make_card(signoffs=["one", "two", "three", "four"])
    monkeypatch.setattr("core.services.persona.resolve_persona", lambda uid=None: card)
    summary = persona_surface_summary()
    assert summary["signoffs"] == ["one", "two"]


@pytest.mark.parametrize(
    "voice, expected",
    [
        ("Direct, warm, no fluff.", "direct"),  # precedence: direct > warm > calm
        ("Warm and friendly.", "warm"),
        ("Composed and calm.", "calm"),
        ("Enthusiastic, high energy.", "calm"),  # no keyword => default
        ("", "calm"),
    ],
)
def test_voice_style_derivation(monkeypatch, voice, expected):
    card = make_card(style={"voice": voice})
    monkeypatch.setattr("core.services.persona.resolve_persona", lambda uid=None: card)
    assert persona_surface_summary()["voice_style"] == expected
