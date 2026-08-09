"""Persona card — the per-tenant "who you are" artifact (M18).

One grounded card per tenant, stored in core_config under key 'persona'
((owner_id, key) composite PK). Synthesized monthly (+ drift) by
core/skills/persona_synthesis.py; read here with fail-closed semantics:
no row / parse error / wrong schema -> None, and every caller falls back
to neutral. A persona can never be another tenant's.

Card schema (v1)::

    {
      "schema_version": 1,
      "generation": int,               # bumped on every write
      "generated_at": ISO-8601,
      "who": str,                      # who the user is — facts only, <= 240
      "people": [str],                 # top people by connectivity, <= 10
      "domains": [str],                # routing domain names, <= 8
      "style": {"voice": str},         # tone instruction, <= 200
      "signoffs": [str],               # 2-4 short lines, 3-70 chars each
      "claims": [{"subject","predicate","object"}],  # traceable triples
      "never": [str],                  # sensitive topics: never in copy
      "source_fingerprint": {...}      # drift-detection inputs
    }

All prose is verified by core/services/persona_verifier.py (G1-G4 grounding
gates) before a card may be written. This module only reads and shape-checks.
"""

from __future__ import annotations

import json
import re

from core.services.db import tenant_aware_client

CARD_SCHEMA_VERSION = 1
_PERSONA_KEY = "persona"
_PERSONA_PREV_KEY = "persona_prev"  # rollback source (M18 versioning)

# Per-tenant process cache, keyed by user id (mirrors user_settings).
_persona_cache: dict[str, dict | None] = {}


def clear_persona_cache(user_id: str | None = None) -> None:
    """Drop cached persona (tests / after writes). None clears all."""
    if user_id is None:
        _persona_cache.clear()
    else:
        _persona_cache.pop(user_id, None)


def _effective_user_id(user_id: str | None) -> str | None:
    if user_id:
        return user_id
    try:
        from core.services.user_settings import current_user_id

        return current_user_id() or None
    except Exception:
        return None


def validate_card_shape(card: object) -> bool:
    """Minimal structural check for a stored persona card.

    Deep content verification (G1-G4) happens at write time in the
    verifier; this is the read-path guard so a corrupt row never surfaces.
    """
    if not isinstance(card, dict):
        return False
    if card.get("schema_version") != CARD_SCHEMA_VERSION:
        return False
    for key in ("who", "people", "domains", "style", "signoffs", "claims",
                "source_fingerprint", "generated_at", "generation"):
        if key not in card:
            return False
    if not isinstance(card["who"], str) or len(card["who"]) > 240:
        return False
    if not isinstance(card["people"], list) or len(card["people"]) > 10:
        return False
    if not isinstance(card["domains"], list) or len(card["domains"]) > 8:
        return False
    if not isinstance(card["style"], dict) or not isinstance(
        card["style"].get("voice"), str
    ):
        return False
    if not isinstance(card["signoffs"], list) or not 2 <= len(card["signoffs"]) <= 4:
        return False
    if not isinstance(card["claims"], list):
        return False
    snapshot = card.get("life_snapshot", [])
    if not isinstance(snapshot, list) or len(snapshot) > 12:
        return False
    if any(not isinstance(s, str) or not 1 <= len(s) <= 140 for s in snapshot):
        return False
    return True


def resolve_persona(user_id: str | None = None) -> dict | None:
    """The tenant's persona card, or None (fail-closed).

    Never inherits another tenant's card: unscoped calls (no tenant
    context) return None.
    """
    uid = _effective_user_id(user_id)
    if not uid:
        return None
    if uid in _persona_cache:
        return _persona_cache[uid]
    card: dict | None = None
    try:
        rows = (
            tenant_aware_client()
            .table("core_config")
            .select("content")
            .eq("key", _PERSONA_KEY)
            .limit(1)
            .execute()
            .data
        )
        if rows and rows[0].get("content"):
            content = rows[0]["content"]
            parsed = json.loads(content) if isinstance(content, str) else content
            if validate_card_shape(parsed):
                card = parsed
    except Exception:
        card = None
    _persona_cache[uid] = card
    return card


def persona_voice_block(user_name: str = "", card: dict | None = None) -> str:
    """The persona 'voice block' appended to LLM prompts.

    Byte-identical to the M18 briefing.py inline block when a card exists
    (single source of truth for all surfaces); returns ``""`` when there is
    no card (fail-closed: every prompt stays byte-identical pre-persona).
    """
    card = card if card is not None else resolve_persona()
    if not card:
        return ""
    who = (card.get("who") or "").strip()
    style = ((card.get("style") or {}).get("voice") or "").strip()
    if not (who or style):
        return ""
    never = " ".join(card.get("never") or [])
    voice = f" This is {user_name}'s world: {who}."
    if style:
        voice += f" Voice: {style}."
    if never:
        voice += f" Never: {never}."
    return voice


def persona_guard_text(
    text: str,
    card: dict | None = None,
    fallback: str = "",
) -> str:
    """Post-generation never-guard: reject copy that touches a never-topic.

    Same matcher as the verifier's G4 gate (word-boundary + inflection-aware
    on casefolded text): if the text hits any topic in ``card["never"]``,
    return ``fallback`` (neutral copy); otherwise return ``text`` unchanged.
    Fail-closed: no card => text passes through untouched.
    """
    card = card if card is not None else resolve_persona()
    if not card or not text:
        return text
    never = [t for t in (card.get("never") or []) if isinstance(t, str) and t.strip()]
    if not never:
        return text
    lowered = text.casefold()
    for topic in never:
        if re.search(rf"\b{re.escape(topic.casefold())}\w*\b", lowered):
            return fallback
    return text


# ── Phase 2B: surface summary (R4 closed-enum transport) ──────────────────

# voice_style keyword precedence — most specific first. Derived deterministically
# from the card's prose voice descriptor so the app only ever receives a closed
# enum token, never prose (R4). Default is 'calm'. STOPGAP: replaced by a
# style_token stamped at synthesis time once the card schema gains it.
_VOICE_STYLE_KEYWORDS = (("direct", "direct"), ("warm", "warm"), ("calm", "calm"))
_DEFAULT_VOICE_STYLE = "calm"


def _derive_voice_style(card: dict) -> str:
    voice = (((card.get("style") or {}).get("voice") or "").lower())
    for keyword, token in _VOICE_STYLE_KEYWORDS:
        if keyword in voice:
            return token
    return _DEFAULT_VOICE_STYLE


def persona_surface_summary(user_id: str | None = None) -> dict | None:
    """Safe, display-ready persona summary for app surfaces (Phase 2B, R4).

    The app receives ONLY a closed-enum ``voice_style`` token, the tenant's
    display name, and up to two signoffs. Never the raw card, curated people
    names, or the never-topic list — those stay server-side. Fail-closed: no
    card (or no tenant scope) => None, and every consumer renders today's copy.
    """
    card = resolve_persona(user_id)
    if not card:
        return None
    display_name = ""
    try:
        from core.services.user_settings import resolve_user_name

        display_name = resolve_user_name(user_id) or ""
    except Exception:
        display_name = ""
    signoffs = [
        s
        for s in (card.get("signoffs") or [])
        if isinstance(s, str) and s.strip()
    ][:2]
    return {
        "display_name": display_name,
        "voice_style": _derive_voice_style(card),
        "signoffs": signoffs,
    }
