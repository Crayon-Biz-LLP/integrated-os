"""Briefing prompt golden surface (briefing aspect) — the re-based §10 suite.

Ports the strongest assertions of the MANUAL M9.2/M9.3/M9.4 verify scripts
into the pytest gate so a prompt/section/tz change that breaks a pinned
output fails CI instead of only a hand-run script.

Golden-artifact model (see tests/golden/README.md):
  - `tests/golden/briefing_tenant1.txt` is a CHANNEL-TENANT (tenant #1)
    regression pin — Danny's pinned output shape. It is compared HERMETICALLY
    here (the sections row is mocked, never Danny's live DB) — the Test-tenant
    principle (plans/75 §7) is untouched.
  - A fresh tenant resolves the NEUTRAL skeleton (no Church/Ashraya/faith) —
    that is the Test-tenant flavor of the same branch.

Covers:
  - byte-identical pin reproduction under the mocked Danny row
  - neutral fresh-tenant skeleton (base sections only)
  - per-owner sections never bleed (Danny vs Priya)
  - fail-closed (DB error → Danny-era default, never a raise)
  - determinism (two cold resolutions identical)
  - per-tenant timezone helpers (IST/+05:30, JST/+09:00, fail-closed fallback)
"""

import json
import pytest
from pathlib import Path
from unittest.mock import patch

from core.prompts.briefing import build_pulse_briefing_prompt
from core.pulse.models import BriefingContext
from core.services.briefing_sections import (
    NEUTRAL_BRIEFING_SECTIONS,
    resolve_briefing_sections,
)

pytestmark = pytest.mark.briefing

GOLDEN = Path(__file__).parent.parent / "golden" / "briefing_tenant1.txt"

FIXED_CTX = dict(
    current_time_str="2026-08-07 09:00 IST",
    briefing_mode="morning",
    is_overloaded=False,
    is_monday_morning=False,
    people_names="Marcus Durai, Sunju",
    season_config="Q3: growth focus",
    session_memory_context="",
    calendar_context="09:30 — Ashraya trustees sync",
    recent_memories_context="",
    hindsight_context="None",
    weekly_patterns_str="",
    graph_task_context="",
    dependency_context="None",
    social_graph_context="None",
    temporal_context="None",
    centrality_context="None",
    adaptive_context="None",
    morning_pulse_narrative="",
    serendipity_context="None",
    canonical_context="No Master Pages yet. Rely on raw context.",
    delta_context="None",
    practices_context="None",
    active_clusters_context="None",
    universal_task_map="None",
    cluster_task_list="No tasks.",
    urgency_lists="",
    pattern_context="None",
    newly_enriched_context="None",
    recent_urls_context="None",
    new_inputs="None",
    new_input_tags="None",
    focal_learning_context="No pattern data yet.",
    home_mode_learning_context="No pattern data yet.",
)

DANNY_ROW = json.dumps({
    "domain_sections": [
        {"name": "Church", "description": "Ashraya admin, operations, finance tasks only."}
    ],
    "home_description": "Family and personal tasks only. Not Ashraya/Church.",
    "role_framing": "work, family, and faith",
})

PRIYA_ROW = json.dumps({
    "domain_sections": [
        {"name": "Volunteering", "description": "Charity and volunteering tasks only."}
    ],
    "home_description": "Family tasks only.",
    "role_framing": "work and personal life",
})


def _render(row_json):
    with patch("core.services.briefing_sections._fetch_briefing_sections_cfg", return_value=row_json):
        return build_pulse_briefing_prompt(BriefingContext(**FIXED_CTX))


# ── 1. Channel-tenant pin reproduces (hermetic) ────────────────────────────

def test_tenant1_briefing_pin_reproduces_byte_identical():
    """Danny's pinned output shape must reproduce under his (mocked) row with
    ZERO diffs. A prompt/section change that alters his output fails here —
    this is the regression protection that used to live only in a manual
    script. Hermetic: the row is mocked, no live DB, no tenant data read."""
    rendered = _render(DANNY_ROW)
    assert rendered == GOLDEN.read_text(), (
        "briefing_tenant1.txt pin drifted from the rendered prompt — a "
        "prompt/sections change landed without a deliberate golden update"
    )


# ── 2. Neutral fresh tenant (Test-tenant flavor) ───────────────────────────

def test_fresh_tenant_gets_neutral_skeleton_only():
    fresh = _render(json.dumps(NEUTRAL_BRIEFING_SECTIONS))
    assert "- Church:" not in fresh
    assert "Not Ashraya/Church" not in fresh
    assert "work, family, and faith" not in fresh
    for section in ["- Work:", "- Home:", "- Done:", "- Schedule:", "- Ideas:", "- Stale Loops:"]:
        assert section in fresh


# ── 3. No cross-tenant leak ────────────────────────────────────────────────

def test_sections_never_bleed_across_tenants():
    with patch("core.services.briefing_sections._fetch_briefing_sections_cfg", return_value=DANNY_ROW):
        danny = resolve_briefing_sections("uid-danny")
    with patch("core.services.briefing_sections._fetch_briefing_sections_cfg", return_value=PRIYA_ROW):
        priya = resolve_briefing_sections("uid-priya")
    assert "Church" in danny.board_lines and "Volunteering" not in danny.board_lines
    assert "Volunteering" in priya.board_lines and "Church" not in priya.board_lines


# ── 4. Fail-closed ─────────────────────────────────────────────────────────

def test_db_error_falls_back_to_danny_default_never_raises():
    with patch("core.services.briefing_sections._fetch_briefing_sections_cfg", side_effect=Exception("db down")):
        failed = resolve_briefing_sections("uid-danny")
    with patch("core.services.briefing_sections._fetch_briefing_sections_cfg", return_value=DANNY_ROW):
        default = resolve_briefing_sections("uid-danny")
    assert failed.board_lines == default.board_lines


# ── 5. Determinism ─────────────────────────────────────────────────────────

def test_resolution_is_deterministic():
    with patch("core.services.briefing_sections._fetch_briefing_sections_cfg", return_value=DANNY_ROW):
        a = resolve_briefing_sections("uid-danny")
        b = resolve_briefing_sections("uid-danny")
    assert a.board_lines == b.board_lines


# ── 6. Per-tenant timezone helpers (M9.4 surface) ──────────────────────────

def test_tz_helpers_istanbul_default():
    from core.lib.time_utils import tz_label, tz_offset_str
    assert tz_label() == "IST"
    assert tz_offset_str() == "+05:30"


def test_tz_helpers_non_ist_tenant():
    from core.lib.time_utils import tz_label, tz_offset_str
    with patch("core.services.user_settings.resolve_timezone", return_value="Asia/Tokyo"):
        assert tz_label() == "JST"
        assert tz_offset_str() == "+09:00"


def test_tz_helpers_per_user_no_leak():
    from core.lib.time_utils import tz_label, tz_offset_str
    _USER_TZ = {"uid-danny": "Asia/Kolkata", "uid-priya": "Asia/Tokyo"}
    with patch("core.services.user_settings.resolve_timezone", side_effect=lambda uid=None: _USER_TZ.get(uid, "Asia/Kolkata")):
        assert tz_offset_str("uid-danny") == "+05:30"
        assert tz_offset_str("uid-priya") == "+09:00"
        assert tz_label("uid-priya") == "JST"


def test_tz_helpers_fail_closed_to_ist():
    from core.lib.time_utils import tz_label, tz_offset_str
    with patch("core.services.user_settings.resolve_timezone", side_effect=Exception("tz db down")):
        assert tz_offset_str() == "+05:30"
        assert tz_label() == "IST"
