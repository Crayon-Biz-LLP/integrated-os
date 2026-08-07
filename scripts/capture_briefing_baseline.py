"""capture_briefing_baseline.py — M9.3 Step 1: baseline BEFORE any edit.

Renders the pulse briefing prompt exactly as tenant #1 (Danny) sees it TODAY
(with the hardcoded Work/Home/Church section block) and commits the output to
tests/golden/briefing_tenant1.txt. That committed artifact is the byte-diff
target for scripts/verify_m9_3_sections.py — the gate proves M9.3's
sections-from-config swap renders the SAME prompt for Danny.

Why no live DB: build_pulse_briefing_prompt() resolves user_name via
user_settings fallbacks (env/default → "Danny") and the section block will
come from core_config (mocked in the verify script). The baseline here is a
deterministic render of the CURRENT hardcoded prompt.

Run BEFORE editing core/prompts/briefing.py:
    python3 scripts/capture_briefing_baseline.py
Exit 0 = golden written (or already current).
"""

import os
import sys
from pathlib import Path

os.environ.setdefault("SUPABASE_URL", "http://localhost:1")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "test")
os.environ.setdefault("USER_NAME", "Danny")
os.environ.setdefault("USER_TIMEZONE", "Asia/Kolkata")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

GOLDEN = ROOT / "tests" / "golden" / "briefing_tenant1.txt"

# Fixed, deterministic BriefingContext — must stay identical between capture
# and verify so the byte-diff is apples-to-apples.
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


def render_current_prompt() -> str:
    """Render the pulse briefing prompt exactly as shipped today."""
    from core.pulse.models import BriefingContext
    from core.prompts.briefing import build_pulse_briefing_prompt
    ctx = BriefingContext(**FIXED_CTX)
    return build_pulse_briefing_prompt(ctx)


def main() -> int:
    rendered = render_current_prompt()
    GOLDEN.parent.mkdir(parents=True, exist_ok=True)
    prior = GOLDEN.read_text() if GOLDEN.exists() else None
    if prior == rendered:
        print(f"✅ Golden already current: {GOLDEN.relative_to(ROOT)}")
        return 0
    GOLDEN.write_text(rendered)
    print(f"✅ Golden written ({len(rendered)} chars): {GOLDEN.relative_to(ROOT)}")
    print("   (run this BEFORE editing core/prompts/briefing.py — it must")
    print("    capture the pre-change rendering)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
