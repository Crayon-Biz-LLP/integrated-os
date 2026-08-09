"""Persona-toned message composition — the single home for proactive copy.

Phase 2B (R2): every surface that tells the user something (focal-action
confirmations, decision-pulse push) composes its text here and nowhere else.
Templates are ``(neutral_form, persona_form)`` pairs; the persona form applies
only when a persona surface summary exists, and the final string always passes
``persona_guard_text`` so a never-topic can never reach the user (it collapses
to the neutral form). No card => byte-identical to today's copy (R5 fail-closed).

The persona card itself is NEVER read here — the composer receives the surface
summary (or None) and delegates guarding to ``core.services.persona``.
"""

from __future__ import annotations

from core.services.persona import persona_guard_text

# Neutral forms — the exact strings the surfaces return today. A missing
# summary returns these byte-identical (R5).

_NEUTRAL_DONE = "Action completed"
_NEUTRAL_SNOOZED = "Dismissed — I'll keep it off your board for now"
_NEUTRAL_CORRECTED = "Correction recorded — Rhodey will learn from this."
_NEUTRAL_PUSH_TITLE = "{total} things need your call"
_NEUTRAL_PUSH_BODY = "Something needs your call"


def _name(summary: dict | None) -> str:
    return ((summary or {}).get("display_name") or "").strip()


def _guard(text: str, card: dict | None, neutral: str) -> str:
    """Apply the never-guard to a persona form; collapse to neutral on a hit."""
    return persona_guard_text(text, card=card, fallback=neutral)


def compose_done(summary: dict | None, *, card: dict | None = None) -> str:
    """Focal-action 'done' confirmation."""
    if not summary:
        return _NEUTRAL_DONE
    name = _name(summary)
    persona = f"Done{', ' + name if name else ''} — off your board."
    return _guard(persona, card, _NEUTRAL_DONE)


def compose_snoozed(summary: dict | None, days: int, *, card: dict | None = None) -> str:
    """Focal-action 'snooze' confirmation with the ladder's day count."""
    if not summary:
        return _NEUTRAL_SNOOZED
    name = _name(summary)
    unit = "day" if days == 1 else "days"
    persona = (
        f"Snoozed for {days} {unit} — back when you're ready"
        f"{', ' + name if name else ''}."
    )
    return _guard(persona, card, _NEUTRAL_SNOOZED)


def compose_corrected(summary: dict | None, *, card: dict | None = None) -> str:
    """Focal-action 'correct' confirmation (correction signal recorded)."""
    if not summary:
        return _NEUTRAL_CORRECTED
    name = _name(summary)
    persona = (
        f"Got it{', ' + name if name else ''} — corrected. "
        "Rhodey will learn from this."
    )
    return _guard(persona, card, _NEUTRAL_CORRECTED)


def compose_push_title(total: int, name: str = "", *, card: dict | None = None) -> str:
    """Decision-pulse push title (ported from decision_pulse.py inline copy).

    The name opener is TENANT IDENTITY, not persona-card content — today it
    applies for every named tenant, so it is keyed off ``name`` (a data param),
    never off the persona summary. Card-less tenants stay byte-identical (R5).
    """
    neutral = _NEUTRAL_PUSH_TITLE.format(total=total)
    text = f"{name.strip()}, {neutral}" if name.strip() else neutral
    return _guard(text, card, neutral)


def compose_push_body(channels: list[str], *, card: dict | None = None) -> str:
    """Decision-pulse push body (ported from decision_pulse.py inline copy).

    Applied for every tenant with channels (as today); only the never-guard
    can collapse it to neutral. No persona-card gating — R5 byte-identical.
    """
    if channels:
        text = f"From {', '.join(channels)} — want a look?"
    else:
        text = _NEUTRAL_PUSH_BODY
    return _guard(text, card, _NEUTRAL_PUSH_BODY)
