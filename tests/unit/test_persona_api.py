"""Phase 2B Step 1: API transport tests.

Proves, per the hardened test matrix:

- (a) fail-closed — GET /api/persona returns null without a card, so the
  app renders today's neutral copy everywhere (R5).
- R4 — with a card, the summary is exactly {display_name, voice_style,
  signoffs}: closed-enum transport; the raw card, curated people, and
  never-topics never leave the server.
- (b) isolation — the endpoint resolves the card of the tenant in context;
  two tenants can never see each other's summary.

The negative AST gate (matrix d) lives in test_persona_l3_context.py.
"""



from __future__ import annotations
import pytest

import asyncio

from fastapi.testclient import TestClient

from api.index import app, get_persona_route
pytestmark = pytest.mark.briefing


client = TestClient(app)


def _card(**overrides) -> dict:
    card: dict = {
        "schema_version": 1,
        "generation": 2,
        "generated_at": "2026-08-09T00:00:00Z",
        "source_fingerprint": {},
        "who": "Danny is the founder of Crayon based in Chennai, India.",
        "people": ["Sunjula Daniel"],
        "domains": ["Personal"],
        "style": {"voice": "Direct, warm, no fluff."},
        "signoffs": [
            "Wishing you peace and joy.",
            "May your day be filled with grace.",
        ],
        "claims": [],
        "never": ["debt", "loan", "stress"],
    }
    card.update(overrides)
    return card


def _noop_auth(request):
    """Bypass real auth in tests — the tenant context is set by the caller."""
    return None


def _stub_request():
    return object()


# ── (a) fail-closed ──────────────────────────────────────────────────────


def test_persona_endpoint_fail_closed(monkeypatch):
    monkeypatch.setattr("api.index.require_api_auth", _noop_auth)
    monkeypatch.setattr("core.services.persona.resolve_persona", lambda uid=None: None)
    r = client.get("/api/persona")
    assert r.status_code == 200
    assert r.json() is None


def test_persona_endpoint_summary_shape(monkeypatch):
    monkeypatch.setattr("api.index.require_api_auth", _noop_auth)
    monkeypatch.setattr(
        "core.services.persona.resolve_persona", lambda uid=None: _card()
    )
    monkeypatch.setattr(
        "core.services.user_settings.resolve_user_name", lambda uid=None: "Danny"
    )
    r = client.get("/api/persona")
    assert r.status_code == 200
    body = r.json()
    assert body == {
        "display_name": "Danny",
        "voice_style": "direct",
        "signoffs": [
            "Wishing you peace and joy.",
            "May your day be filled with grace.",
        ],
    }
    # R4: nothing else leaks — no raw card, no curated people, no never list.
    for forbidden in ("never", "people", "who", "style", "life_snapshot", "claims"):
        assert forbidden not in body


def test_persona_endpoint_voice_style_variant(monkeypatch):
    monkeypatch.setattr("api.index.require_api_auth", _noop_auth)
    monkeypatch.setattr(
        "core.services.persona.resolve_persona",
        lambda uid=None: _card(style={"voice": "Warm and friendly."}),
    )
    monkeypatch.setattr(
        "core.services.user_settings.resolve_user_name", lambda uid=None: "Johan"
    )
    body = client.get("/api/persona").json()
    assert body["display_name"] == "Johan"
    assert body["voice_style"] == "warm"


# ── (b) isolation ────────────────────────────────────────────────────────


def test_persona_endpoint_isolated_per_tenant(monkeypatch):
    """The endpoint resolves the card of the tenant in context — tenant A's
    summary can never be tenant B's."""
    from core.services.db import tenant_scope
    from core.services.user_settings import current_user_id

    cards = {
        "tenant-a-uid": _card(style={"voice": "Direct, warm, no fluff."}),
        "tenant-b-uid": _card(style={"voice": "Warm and friendly."}),
    }

    def fake_resolve(uid=None):
        return cards.get(current_user_id())

    monkeypatch.setattr("api.index.require_api_auth", _noop_auth)
    monkeypatch.setattr("core.services.persona.resolve_persona", fake_resolve)
    monkeypatch.setattr(
        "core.services.user_settings.resolve_user_name", lambda uid=None: "Danny"
    )

    with tenant_scope("tenant-a-uid"):
        summary_a = asyncio.run(get_persona_route(_stub_request()))
    with tenant_scope("tenant-b-uid"):
        summary_b = asyncio.run(get_persona_route(_stub_request()))

    assert summary_a is not None and summary_b is not None
    assert summary_a["voice_style"] == "direct"
    assert summary_b["voice_style"] == "warm"
    assert summary_a != summary_b
