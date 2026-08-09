"""Unit tests for the M18c persona-as-L3-knowledge fix.

Root cause (session-notes/72-persona-l3-knowledge.md): the persona card —
Layer 3 (Intelligence) knowledge — was injected into prompts as a Layer 4
(Presentation) string-append (`persona_voice_block()` called directly at
each prompt site). The curated life circle never entered the L3 context
assembly pipeline alongside memories/tasks/people, so generators could not
reason about the user's world the way they reason about other knowledge.

The fix: `ContextProvider.hydrate_persona_context()` is the single L3
accessor; every generator consumes the persona through the context
assembly and prompt builders receive it as a parameter (never read the
card themselves).

These tests pin the fail-closed contract, the life-circle inclusion, and
the layer rule (no generator reaches into the card directly).
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch

from core.pulse.context import ContextProvider
from core.services.persona import clear_persona_cache


async def _hydrate(provider) -> str:
    return await provider.hydrate_persona_context()


def _card(**overrides):
    card = {
        "who": "Danny is the founder of Crayon based in Chennai, India.",
        "style": {"voice": "Direct, composed, and concise."},
        "signoffs": ["Rest well.", "Locked in for the night."],
        "never": ["debt", "stress"],
        "life_snapshot": [
            "Sunjula Daniel (spouse)",
            "Jeremy (family)",
            "Jaden (family)",
            "Jeffery (family)",
        ],
    }
    card.update(overrides)
    return card


def _patch_card(card):
    """Patch the service-layer resolve_persona (what hydrate/voice_block
    call internally) and clear the per-tenant cache between tests."""
    clear_persona_cache()
    return patch("core.services.persona.resolve_persona", return_value=card)


# ── hydrate_persona_context: fail-closed ─────────────────────────────────


def test_no_card_returns_empty():
    """No persona card => '' => every prompt stays byte-identical pre-persona."""
    provider = ContextProvider()
    with _patch_card(None):
        assert asyncio.run(_hydrate(provider)) == ""


def test_card_without_life_snapshot_returns_voice_block():
    """A card with who/style/never but no life_snapshot => exactly the
    M18 voice block, no Life clause added."""
    provider = ContextProvider()
    with _patch_card(_card(life_snapshot=[])):
        out = asyncio.run(_hydrate(provider))
        assert "Danny's world" in out
        assert "Life:" not in out


# ── hydrate_persona_context: life circle is knowledge ────────────────────


def test_card_with_life_snapshot_includes_life_clause():
    """The curated circle is L3 knowledge: it must appear in the block so
    generators can reason about the user's world."""
    provider = ContextProvider()
    with _patch_card(_card()):
        out = asyncio.run(_hydrate(provider))
        assert "Life: Sunjula Daniel (spouse), Jeremy (family)" in out
        assert "Jeffery (family)" in out


def test_hydrate_never_raises_on_db_failure():
    """Any read failure => '' (fail-closed), never a crash or another
    tenant's card."""
    provider = ContextProvider()
    with patch("core.services.persona.resolve_persona",
               side_effect=RuntimeError("db down")):
        assert asyncio.run(_hydrate(provider)) == ""


def test_card_with_padded_fields_normalized():
    """Write-time normalization (persona_synthesis) strips padded fields;
    the read path must not double-space. A padded who produces the same
    block as a clean one."""
    provider = ContextProvider()
    with _patch_card(_card(who="  Danny is the founder of Crayon based in Chennai, India.  ")):
        out = asyncio.run(_hydrate(provider))
        assert " This is Danny's world: Danny is the founder" in out
        assert "  " not in out


# ── query.py builder: presentation-only, receives persona as a param ─────


def test_build_interrogate_accepts_persona_param():
    from core.prompts.query import build_interrogate_brain_prompt

    prompt = build_interrogate_brain_prompt(
        now_str="2026-08-09 10:00:00",
        sources_str="active tasks",
        context_str="TASK: Call Mark",
        query="What's on top?",
        streaming=True,
        persona_context=" This is Danny's world: founder of Crayon. Voice: Direct. Life: Sunjula (spouse).",
    )
    assert "Danny's world" in prompt
    assert "Life: Sunjula" in prompt


def test_build_interrogate_empty_persona_byte_identical():
    """Empty persona_context must produce a prompt with no persona section
    (byte-identical pre-persona path)."""
    from core.prompts.query import build_interrogate_brain_prompt

    prompt = build_interrogate_brain_prompt(
        now_str="2026-08-09 10:00:00",
        sources_str="active tasks",
        context_str="TASK: Call Mark",
        query="What's on top?",
        streaming=True,
        persona_context="",
    )
    assert "world:" not in prompt
    assert "Life:" not in prompt


# ── Write-time contract: verifier count gates mirror the read path ───────


def _facts_min():
    return {
        "allowed_names": {"danny", "sunjula daniel", "jeremy", "jaden",
                          "jeffery", "personal"},
        "known_triples": set(),
        "root_label": "Danny",
        "sensitive_topics": ["debt"],
        "context": "Danny (Yashwant Daniel), founder of Crayon in Chennai, India.",
        "domains": ["Personal"],
        "life_snapshot": [],
    }


def _valid_card():
    # Mirrors the real pipeline: the synthesis stamps schema_version /
    # source_fingerprint / generated_at BEFORE the verifier runs.
    return {
        "schema_version": 1,
        "generation": 1,
        "generated_at": "2026-08-09T00:00:00Z",
        "source_fingerprint": {"nodes": 1},
        "who": "Danny is a founder in Chennai.",
        "people": ["Sunjula Daniel"],
        "domains": ["Personal"],
        "style": {"voice": "Direct."},
        "signoffs": ["Rest well.", "Locked in for the night."],
        "claims": [],
        "never": ["debt"],
        "life_snapshot": [],
    }


def test_verifier_rejects_over_contract_people():
    """Regression: an 11-person card (Danny's dormant-card bug) must be
    rejected at WRITE time, not silently refused at read time."""
    from core.services.persona_verifier import verify_persona_card

    facts = _facts_min()
    facts["allowed_names"] |= {f"person {i}" for i in range(1, 12)}
    card = _valid_card()
    card["people"] = [f"Person {i}" for i in range(1, 12)]
    ok, errors = verify_persona_card(card, facts)
    assert not ok
    assert any("too many people" in e for e in errors)


def test_verifier_rejects_over_contract_domains_and_signoffs():
    from core.services.persona_verifier import verify_persona_card

    facts = _facts_min()
    facts["allowed_names"] |= {f"domain {i}" for i in range(1, 10)}
    card = _valid_card()
    card["domains"] = [f"Domain {i}" for i in range(1, 10)]
    card["signoffs"] = ["Rest well.", "Locked in.", "All quiet.", "Onward.", "Steady."]
    ok, errors = verify_persona_card(card, facts)
    assert not ok
    assert any("too many domains" in e for e in errors)
    assert any("too many sign-offs" in e for e in errors)


def test_contract_verifier_read_path_agree():
    """A card that passes the verifier must pass validate_card_shape — the
    two contracts can never drift again."""
    from core.services.persona_verifier import verify_persona_card
    from core.services.persona import validate_card_shape

    facts = _facts_min()
    card = _valid_card()
    ok, errors = verify_persona_card(card, facts)
    assert ok, errors
    assert validate_card_shape(card)


# ── The layer rule: generators never read the card directly ──────────────


def test_l3_accessor_is_only_card_read_path_for_generators():
    """Architectural gate (session-notes/72): NO generator in the runtime
    trees may import persona_voice_block / resolve_persona — persona
    knowledge flows only through the ContextProvider accessors
    (hydrate_persona_context / persona_signoffs_context). The gate scans
    whole directories with an ALLOWLIST, so a new generator file added
    later cannot silently bypass the rule. persona_guard_text (L4
    post-generation output guarding) remains the one allowed exception."""
    import ast
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[2]
    # Allowed readers of the card (legitimate Layer-3 service/context code):
    #   services/persona.py           — the card reader itself
    #   services/persona_verifier.py  — write-time grounding gates
    #   skills/persona_synthesis.py   — the card author (monthly job)
    #   pulse/context.py              — the L3 ContextProvider accessors
    allowlist = {
        "core/services/persona.py",
        "core/services/persona_verifier.py",
        "core/skills/persona_synthesis.py",
        "core/pulse/context.py",
    }
    scan_dirs = ["core/pulse", "core/prompts", "core/webhook", "core/skills",
                 "core/actions", "core/agents"]
    scan_files = ["core/decisions.py", "core/clarifier.py"]
    banned_symbols = ("persona_voice_block", "resolve_persona")
    offenders = []

    def _scan(src: str, rel: str) -> None:
        try:
            tree = ast.parse(src)
        except SyntaxError:
            return
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                names = [a.name for a in node.names]
                from_persona_mod = bool(node.module and "persona" in node.module)
                # `from core.services.persona import resolve_persona` AND
                # `from core.services import persona` (alias) both caught.
                if from_persona_mod and any(n in banned_symbols for n in names):
                    offenders.append(f"{rel}: imports {names}")
                if names == ["persona"]:
                    offenders.append(f"{rel}: imports persona module alias")
            elif isinstance(node, ast.Import):
                for a in node.names:
                    if a.name.endswith("persona"):
                        offenders.append(f"{rel}: imports {a.name}")

    for d in scan_dirs:
        for py in sorted((root / d).glob("*.py")):
            rel = py.relative_to(root).as_posix()
            if rel in allowlist:
                continue
            _scan(py.read_text(), rel)
    for f in scan_files:
        py = root / f
        if py.exists() and f not in allowlist:
            _scan(py.read_text(), f)
    assert not offenders, (
        "Layer violation — persona knowledge must flow through "
        "ContextProvider accessors only: " + ", ".join(offenders)
    )
