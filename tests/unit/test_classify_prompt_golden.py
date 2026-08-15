"""Classify prompt golden surface (ingest aspect).

Ports the M9.2 verify script's strongest assertions into the pytest gate:
the ROLE_UPDATE example line rendered from a mocked tenant-#1 graph must
reproduce the committed pin (tests/golden/classify_tenant1.txt) byte-for-byte;
a fresh tenant gets the neutral example; a DB failure is fail-closed to the
neutral line; per-owner caches never bleed.

Hermetic throughout — graph rows are mocked, no live DB.
"""

import pytest
from pathlib import Path
from unittest.mock import patch

from core.prompts.classify import build_classify_intent_prompt
from core.services import example_entities
from core.services.example_entities import NEUTRAL_EXAMPLE

pytestmark = pytest.mark.ingest

GOLDEN = Path(__file__).parent.parent / "golden" / "classify_tenant1.txt"

# Danny's stored shape (dispatch.handle_role_update).
MARCUS = {
    "label": "Marcus Durai",
    "role": "Pastor of Ashraya Chennai Central",
    "org": "Ashraya Chennai Central",
    "_meta": {"enrichment": {"role": "Pastor of Ashraya Chennai Central", "organization_name": "Ashraya Chennai Central"}},
}
PRIYA_ORG = {
    "label": "Rajesh Kumar",
    "role": "COO of Acme",
    "org": "Acme",
    "_meta": {"enrichment": {"role": "COO of Acme", "organization_name": "Acme"}},
}

FIXED_INPUTS = dict(
    text="Marcus Durai is the new Pastor of Ashraya Chennai Central",
    time_phase="morning",
    core_json="[]",
    entities_section="",
    learned_section="",
    context_str="",
    conversation_history="",
    user_name=None,
    routing_rules=None,
)


def _role_update_line(prompt: str) -> str:
    for line in prompt.splitlines():
        if line.strip().startswith("- ROLE_UPDATE:"):
            return line
    return ""


def _render_danny():
    example_entities.clear_cache()
    with patch("core.services.example_entities.get_tenant", return_value="uid-danny"), \
         patch("core.services.example_entities._fetch_important_titles", return_value={"marcus durai", "ashraya"}), \
         patch("core.services.example_entities._fetch_role_people", return_value=[MARCUS]):
        return build_classify_intent_prompt(**FIXED_INPUTS)


# ── 1. Channel-tenant pin line reproduces (mocked graph) ───────────────────

def test_tenant1_role_update_line_reproduces():
    """The ROLE_UPDATE example from a mocked tenant-#1 graph must match the
    committed pin's line byte-for-byte. Hermetic: no live DB."""
    rendered = _render_danny()
    new_line = _role_update_line(rendered)
    golden_line = _role_update_line(GOLDEN.read_text())
    assert new_line == golden_line, (
        "classify_tenant1.txt ROLE_UPDATE line drifted from the rendered "
        "prompt — an example-entities change landed without a golden update"
    )
    assert new_line  # guard: the pin actually has the line


# ── 2. Neutral fresh tenant ────────────────────────────────────────────────

def test_fresh_tenant_gets_neutral_example():
    example_entities.clear_cache()
    with patch("core.services.example_entities.get_tenant", return_value="uid-fresh"), \
         patch("core.services.example_entities._fetch_important_titles", return_value=set()), \
         patch("core.services.example_entities._fetch_role_people", return_value=[]):
        rendered = build_classify_intent_prompt(**FIXED_INPUTS)
    line = _role_update_line(rendered)
    assert NEUTRAL_EXAMPLE in line
    assert "Marcus" not in line


# ── 3. Fail-closed ─────────────────────────────────────────────────────────

def test_db_error_returns_neutral_example_never_raises():
    example_entities.clear_cache()
    with patch("core.services.example_entities.get_tenant", return_value="uid-danny"), \
         patch("core.services.example_entities._fetch_important_titles", side_effect=Exception("db down")), \
         patch("core.services.example_entities._fetch_role_people", return_value=[]):
        example = example_entities.resolve_role_update_example("uid-danny")
    assert example == NEUTRAL_EXAMPLE


# ── 4. No cross-tenant leak ────────────────────────────────────────────────

def test_per_owner_examples_never_bleed():
    example_entities.clear_cache()
    with patch("core.services.example_entities.get_tenant", return_value="uid-danny"), \
         patch("core.services.example_entities._fetch_important_titles", return_value={"marcus durai"}), \
         patch("core.services.example_entities._fetch_role_people", return_value=[MARCUS]):
        danny_example = example_entities.resolve_role_update_example("uid-danny")
    with patch("core.services.example_entities.get_tenant", return_value="uid-priya"), \
         patch("core.services.example_entities._fetch_important_titles", return_value={"rajesh kumar"}), \
         patch("core.services.example_entities._fetch_role_people", return_value=[PRIYA_ORG]):
        priya_example = example_entities.resolve_role_update_example("uid-priya")
    assert "Marcus" in danny_example and "Acme" not in danny_example
    assert "Rajesh Kumar" in priya_example and "Marcus" not in priya_example
    assert set(example_entities._cache.keys()) == {"uid-danny", "uid-priya"}
