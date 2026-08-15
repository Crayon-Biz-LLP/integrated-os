"""Planner prompt golden surface (decision aspect).

Ports the M9.4 verify script's planner assertions into the pytest gate: the
committed pin (tests/golden/planner_tenant1.txt) reproduces byte-identical
under the default Asia/Kolkata tenant, and a non-IST tenant's prompt embeds
its own zone (JST/+09:00), never IST's. Hermetic — timezone is patched, no DB.
"""

import pytest
from pathlib import Path
from unittest.mock import patch

from core.prompts.planner import build_planner_prompt

pytestmark = pytest.mark.decision

GOLDEN = Path(__file__).parent.parent / "golden" / "planner_tenant1.txt"

FIXED_PLANNER = dict(
    current_time="2026-08-07 09:00:00 IST",
    text="Remind me to send the Solvstrat proposal by Friday 2pm",
    title="Send Solvstrat proposal",
    intent="TASK",
    entity="SOLVSTRAT",
    candidate_lines="- 12: Send Solvstrat proposal [Solvstrat] (todo, Friday)\n- 13: Qhord pricing review [Qhord] (todo)",
    org_lines="1: Solvstrat\n2: Qhord\n3: Personal",
)


def test_planner_tenant1_pin_reproduces():
    rendered = build_planner_prompt(**FIXED_PLANNER)
    assert rendered == GOLDEN.read_text(), (
        "planner_tenant1.txt pin drifted from the rendered prompt — a planner "
        "prompt change landed without a deliberate golden update"
    )


def test_planner_non_ist_tenant_embeds_own_zone():
    with patch("core.services.user_settings.resolve_timezone", return_value="Asia/Tokyo"):
        rendered = build_planner_prompt(**FIXED_PLANNER)
    assert "JST (UTC+09:00)" in rendered
    assert "+09:00" in rendered
    assert "+05:30" not in rendered
