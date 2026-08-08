"""verify_m9_4_timezone.py — M9.4 equivalence gate (plans/70 §M9.4).

Proves the timezone swap (literal IST/+05:30 → per-tenant resolver) is safe:

  1. BYTE-IDENTICAL FOR DANNY — the briefing prompt (with the tz slots
     resolved under tenant #1 = Asia/Kolkata) == the M9.3 committed golden,
     AND the planner prompt == the M9.4 committed golden. Zero diffs.
  2. HELPER CORRECTNESS — tz_label() == "IST", tz_offset_str() == "+05:30"
     under Asia/Kolkata.
  3. NON-IST TENANT — Asia/Tokyo resolves JST/+09:00; planner + briefing +
     enrichment prompts embed the tenant's own label/offset, not IST's.
  4. DETERMINISM — two cold resolutions are identical.
  5. FAIL-CLOSED — a timezone-resolution failure falls back to IST/+05:30
     and never raises.
  6. NO CROSS-TENANT LEAK — per-user resolution returns each tenant's own
     zone, never the other's.
  7. GOOGLE FORMATTER — format_rfc3339 attaches the tenant's offset.

Run:  python3 scripts/verify_m9_4_timezone.py
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


# ── Fixtures (identical to the capture scripts) ─────────────────────────────

BRIEFING_GOLDEN = ROOT / "tests" / "golden" / "briefing_tenant1.txt"
PLANNER_GOLDEN = ROOT / "tests" / "golden" / "planner_tenant1.txt"

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

FIXED_PLANNER = dict(
    current_time="2026-08-07 09:00:00 IST",
    text="Remind me to send the Solvstrat proposal by Friday 2pm",
    title="Send Solvstrat proposal",
    intent="TASK",
    entity="SOLVSTRAT",
    candidate_lines="- 12: Send Solvstrat proposal [Solvstrat] (todo, Friday)\n- 13: Qhord pricing review [Qhord] (todo)",
    org_lines="1: Solvstrat\n2: Qhord\n3: Personal",
)


def render_briefing() -> str:
    from core.prompts.briefing import build_pulse_briefing_prompt
    from core.pulse.models import BriefingContext
    return build_pulse_briefing_prompt(BriefingContext(**FIXED_CTX))


def render_system_instruction() -> str:
    """The second pulse prompt (pulse/briefing.py:1001) — holds the
    HIGH-PRECISION TIME FORMATTING rule swapped by M9.4."""
    from core.prompts.briefing import build_pulse_system_instruction
    return build_pulse_system_instruction(
        system_persona="SYSTEM PERSONA: morning.",
        briefing_history_context="",
        routing_logic="",
        drift_context="None",
    )


def render_planner() -> str:
    from core.prompts.planner import build_planner_prompt
    return build_planner_prompt(**FIXED_PLANNER)


# ── 1. Byte-identical for Danny ─────────────────────────────────────────────

print("\n[1] Byte-identical for Danny — briefing + planner vs committed goldens")

briefing = render_briefing()
golden_briefing = BRIEFING_GOLDEN.read_text()
check(
    "Briefing (tz slots under Asia/Kolkata) == M9.3 golden (zero diffs)",
    briefing == golden_briefing,
    f"len(new)={len(briefing)} len(golden)={len(golden_briefing)}",
)
if briefing != golden_briefing:
    for a, b in zip(golden_briefing.splitlines(), briefing.splitlines()):
        if a != b:
            print(f"      -OLD: {a[:90]}\n      +NEW: {b[:90]}")
            break

planner = render_planner()
golden_planner = PLANNER_GOLDEN.read_text()
check(
    "Planner (tz slots under Asia/Kolkata) == M9.4 golden (zero diffs)",
    planner == golden_planner,
    f"len(new)={len(planner)} len(golden)={len(golden_planner)}",
)
if planner != golden_planner:
    for a, b in zip(golden_planner.splitlines(), planner.splitlines()):
        if a != b:
            print(f"      -OLD: {a[:90]}\n      +NEW: {b[:90]}")
            break


# ── 2. Helper correctness (tenant #1) ───────────────────────────────────────

print("\n[2] Helper correctness — Asia/Kolkata → IST / +05:30")
from core.lib.time_utils import tz_label, tz_offset_str  # noqa: E402

check("tz_label() == 'IST'", tz_label() == "IST", f"got {tz_label()!r}")
check("tz_offset_str() == '+05:30'", tz_offset_str() == "+05:30", f"got {tz_offset_str()!r}")


# ── 3. Non-IST tenant (Asia/Tokyo — no DST, deterministic) ──────────────────

print("\n[3] Non-IST tenant — Asia/Tokyo gets JST/+09:00 everywhere")

with patch("core.services.user_settings.resolve_timezone", return_value="Asia/Tokyo"):
    check("tz_offset_str() == '+09:00'", tz_offset_str() == "+09:00", f"got {tz_offset_str()!r}")
    check("tz_label() == 'JST'", tz_label() == "JST", f"got {tz_label()!r}")
    tok_planner = render_planner()
    tok_briefing = render_briefing()
    tok_sys = render_system_instruction()
    from core.prompts.workflow import build_enrichment_prompt  # noqa: E402
    tok_workflow = build_enrichment_prompt("Remind me about the review at 3pm", "")

check("planner: contains 'JST (UTC+09:00)'", "JST (UTC+09:00)" in tok_planner)
check("planner: ISO examples use +09:00, never +05:30",
      "+09:00" in tok_planner and "+05:30" not in tok_planner)
check("briefing: evening phase uses JST", "19:00+ JST" in tok_briefing)
check("system instruction: time-format rule uses JST/UTC+09:00",
      "(JST/UTC+09:00)" in tok_sys and "SS+09:00" in tok_sys and "+05:30" not in tok_sys)
check("Danny system instruction: time-format rule unchanged (IST/UTC+05:30)",
      "(IST/UTC+05:30)" in render_system_instruction() and "SS+05:30" in render_system_instruction())
check("workflow: clock line is JST", "JST" in tok_workflow and "IST" not in tok_workflow)
check("workflow: ISO example uses +09:00",
      "+09:00" in tok_workflow and "+05:30" not in tok_workflow)


# ── 4. Determinism ──────────────────────────────────────────────────────────

print("\n[4] Determinism — two cold resolutions, identical output")
a1, a2 = tz_offset_str(), tz_offset_str()
b1, b2 = tz_label(), tz_label()
check("tz_offset_str() → identical on repeat", a1 == a2)
check("tz_label() → identical on repeat", b1 == b2)


# ── 5. Fail-closed ──────────────────────────────────────────────────────────

print("\n[5] Fail-closed — resolution failure ⇒ IST fallback, never a raise")
try:
    with patch("core.services.user_settings.resolve_timezone", side_effect=Exception("tz db down")):
        f_off = tz_offset_str()
        f_lbl = tz_label()
    check("tz_offset_str() falls back to '+05:30'", f_off == "+05:30", f"got {f_off!r}")
    check("tz_label() falls back to 'IST'", f_lbl == "IST", f"got {f_lbl!r}")
except Exception as e:  # pragma: no cover - failure path
    check("no raise on resolution failure", False, f"raised {type(e).__name__}")


# ── 6. No cross-tenant leak ─────────────────────────────────────────────────

print("\n[6] No cross-tenant leak — per-user resolution")
_USER_TZ = {"uid-danny": "Asia/Kolkata", "uid-priya": "Asia/Tokyo"}


def _per_user_resolve(user_id=None):
    return _USER_TZ.get(user_id, "Asia/Kolkata")


with patch("core.services.user_settings.resolve_timezone", side_effect=_per_user_resolve):
    danny_off = tz_offset_str("uid-danny")
    priya_off = tz_offset_str("uid-priya")
    priya_lbl = tz_label("uid-priya")
check("Danny (uid-danny) keeps +05:30", danny_off == "+05:30", f"got {danny_off!r}")
check("Priya (uid-priya) gets +09:00", priya_off == "+09:00", f"got {priya_off!r}")
check("Priya's label is JST, not IST", priya_lbl == "JST", f"got {priya_lbl!r}")


# ── 7. Google formatter ─────────────────────────────────────────────────────

print("\n[7] Google formatter — tenant offset attached")
from core.services.google_service import format_rfc3339  # noqa: E402

check("Danny: '2026-08-10' → 'T09:00:00+05:30'",
      format_rfc3339("2026-08-10") == "2026-08-10T09:00:00+05:30",
      f"got {format_rfc3339('2026-08-10')!r}")
with patch("core.services.user_settings.resolve_timezone", return_value="Asia/Tokyo"):
    check("Tokyo: '2026-08-10' → 'T09:00:00+09:00'",
          format_rfc3339("2026-08-10") == "2026-08-10T09:00:00+09:00",
          f"got {format_rfc3339('2026-08-10')!r}")


# ── Summary ─────────────────────────────────────────────────────────────────

print()
if FAILURES:
    print(f"❌ M9.4 gate FAILED: {len(FAILURES)} check(s) failed")
    for f in FAILURES:
        print(f"   - {f}")
    sys.exit(1)
print("✅ M9.4 gate GREEN — Danny's prompts byte-identical; non-IST tenants get their own zone; deterministic; fail-closed; no leaks.")
sys.exit(0)
