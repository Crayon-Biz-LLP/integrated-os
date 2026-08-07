"""verify_m9_3_sections.py — M9.3 equivalence gate (plans/70 §M9.3 Step 4).

Proves the sections-from-config swap is safe:

  1. BYTE-IDENTICAL FOR DANNY — the briefing rendered from his seeded
     briefing_sections row == the committed baseline
     (tests/golden/briefing_tenant1.txt), with ZERO diffs (his row reproduces
     the hardcoded prompt exactly — no whitelist needed).
  2. NEUTRAL FRESH TENANT — base skeleton only: no Church section, no
     "Ashraya/Church" home exclusion, no faith framing.
  3. DETERMINISM — two cold resolutions ⇒ identical output.
  4. FAIL-CLOSED — a DB exception ⇒ Danny-era default, never a raise.
  5. NO CROSS-TENANT LEAK — two tenants with different rows each resolve
     their own sections.

Run:  python3 scripts/verify_m9_3_sections.py
Exit 0 = gate green.
"""

import os
import sys
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("SUPABASE_URL", "http://localhost:1")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "test")
os.environ.setdefault("USER_NAME", "Danny")
os.environ.setdefault("USER_TIMEZONE", "Asia/Kolkata")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

FAILURES: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    mark = "✅" if cond else "❌"
    print(f"  {mark} {name}" + (f"  ({detail})" if detail else ""))
    if not cond:
        FAILURES.append(name)


# ── Shared fixtures ─────────────────────────────────────────────────────────

GOLDEN = ROOT / "tests" / "golden" / "briefing_tenant1.txt"

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
)

import json  # noqa: E402

# Danny's seeded briefing_sections row (matches DEFAULT_BRIEFING_SECTIONS).
DANNY_ROW = json.dumps({
    "domain_sections": [
        {"name": "Church", "description": "Ashraya admin, operations, finance tasks only."}
    ],
    "home_description": "Family and personal tasks only. Not Ashraya/Church.",
    "role_framing": "work, family, and faith",
})

# A second tenant's row — must never bleed into Danny's briefing.
PRIYA_ROW = json.dumps({
    "domain_sections": [
        {"name": "Volunteering", "description": "Charity and volunteering tasks only."}
    ],
    "home_description": "Family tasks only.",
    "role_framing": "work and personal life",
})


# ── 1. Byte-identical for Danny ─────────────────────────────────────────────

print("\n[1] Byte-identical for Danny — seeded row vs committed baseline")

from core.prompts.briefing import build_pulse_briefing_prompt  # noqa: E402
from core.pulse.models import BriefingContext  # noqa: E402

ctx = BriefingContext(**FIXED_CTX)
with patch("core.services.briefing_sections._fetch_briefing_sections_cfg", return_value=DANNY_ROW):
    rendered = build_pulse_briefing_prompt(ctx)

golden_text = GOLDEN.read_text()
check(
    "Briefing rendered from Danny's seeded row == baseline (zero diffs)",
    rendered == golden_text,
    f"len(new)={len(rendered)} len(golden)={len(golden_text)}",
)
if rendered != golden_text:
    for a, b in zip(golden_text.splitlines(), rendered.splitlines()):
        if a != b:
            print(f"      -OLD: {a[:90]}\n      +NEW: {b[:90]}")
            break


# ── 2. Neutral fresh tenant ─────────────────────────────────────────────────

print("\n[2] Neutral fresh tenant — base skeleton only")
from core.services.briefing_sections import NEUTRAL_BRIEFING_SECTIONS  # noqa: E402
neutral_row = json.dumps(NEUTRAL_BRIEFING_SECTIONS)
with patch("core.services.briefing_sections._fetch_briefing_sections_cfg", return_value=neutral_row):
    fresh = build_pulse_briefing_prompt(BriefingContext(**FIXED_CTX))

check("no Church section for fresh tenant", "- Church:" not in fresh)
check("no 'Ashraya/Church' home exclusion", "Not Ashraya/Church" not in fresh)
check("no faith framing", "work, family, and faith" not in fresh)
check("base skeleton present (Work/Home/Done/Schedule/Ideas)",
      all(s in fresh for s in ["- Work:", "- Home:", "- Done:", "- Schedule:", "- Ideas:", "- Stale Loops:"]))
check("fidelity list is Work/Home/Done (no Church)", "Every task in Work/Home/Done MUST" in fresh)
check("URGENT hides only Home, Ideas", "- URGENT mode: Hide Home, Ideas. Work and Done only." in fresh)


# ── 3. Determinism ──────────────────────────────────────────────────────────

print("\n[3] Determinism — two cold resolutions, identical output")
from core.services import briefing_sections  # noqa: E402

a = None
b = None
with patch("core.services.briefing_sections._fetch_briefing_sections_cfg", return_value=DANNY_ROW):
    a = briefing_sections.resolve_briefing_sections("uid-danny")
with patch("core.services.briefing_sections._fetch_briefing_sections_cfg", return_value=DANNY_ROW):
    b = briefing_sections.resolve_briefing_sections("uid-danny")
check("resolve_briefing_sections() → identical output on repeat", a == b)


# ── 4. Fail-closed ──────────────────────────────────────────────────────────

print("\n[4] Fail-closed — DB error ⇒ Danny-era default, never a raise")
try:
    with patch("core.services.briefing_sections._fetch_briefing_sections_cfg", side_effect=Exception("db down")):
        failed = briefing_sections.resolve_briefing_sections("uid-danny")
    check("DB exception → Danny-era default (byte-identical sections)", failed.board_lines == a.board_lines)
except Exception as e:
    check("DB exception → Danny-era default (byte-identical sections)", False, f"raised {type(e).__name__}")


# ── 5. No cross-tenant leak ─────────────────────────────────────────────────

print("\n[5] No cross-tenant leak — per-owner rows resolve per-owner sections")
with patch("core.services.briefing_sections._fetch_briefing_sections_cfg", return_value=DANNY_ROW):
    danny_s = briefing_sections.resolve_briefing_sections("uid-danny")
with patch("core.services.briefing_sections._fetch_briefing_sections_cfg", return_value=PRIYA_ROW):
    priya_s = briefing_sections.resolve_briefing_sections("uid-priya")
check("Danny's sections keep Church", "Church" in danny_s.board_lines and "Volunteering" not in danny_s.board_lines)
check("Priya's sections are hers only", "Volunteering" in priya_s.board_lines and "Church" not in priya_s.board_lines)
check("Priya's fidelity list excludes Church", "Every task in Work/Home/Done MUST" in priya_s.fidelity_names or priya_s.fidelity_names == "Work/Home/Volunteering/Done")


# ── Summary ─────────────────────────────────────────────────────────────────

print()
if FAILURES:
    print(f"❌ M9.3 gate FAILED: {len(FAILURES)} check(s) failed")
    for f in FAILURES:
        print(f"   - {f}")
    sys.exit(1)
print("✅ M9.3 gate GREEN — Danny's briefing byte-identical; fresh tenants get the base skeleton; deterministic; fail-closed; no leaks.")
sys.exit(0)
