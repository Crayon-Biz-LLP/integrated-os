"""capture_m9_4_baseline.py — M9.4 baseline capture (plans/70 §M9.4).

Run BEFORE any timezone edit. Renders the action-planner prompt under
tenant #1's deterministic env defaults and writes the committed golden
tests/golden/planner_tenant1.txt — the byte-diff target for the gate.

The briefing golden already exists (tests/golden/briefing_tenant1.txt,
captured during M9.3). The workflow enrichment prompt embeds the current
time, so it cannot be byte-diffed — verify_m9_4_timezone.py gates it
structurally instead.

Run:  python3 scripts/capture_m9_4_baseline.py
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

from core.prompts.planner import build_planner_prompt  # noqa: E402

# Fixed, deterministic inputs (same fixture the gate uses).
FIXED_CURRENT_TIME = "2026-08-07 09:00:00 IST"

prompt = build_planner_prompt(
    current_time=FIXED_CURRENT_TIME,
    text="Remind me to send the Solvstrat proposal by Friday 2pm",
    title="Send Solvstrat proposal",
    intent="TASK",
    entity="SOLVSTRAT",
    candidate_lines="- 12: Send Solvstrat proposal [Solvstrat] (todo, Friday)\n- 13: Qhord pricing review [Qhord] (todo)",
    org_lines="1: Solvstrat\n2: Qhord\n3: Personal",
)

out = ROOT / "tests" / "golden" / "planner_tenant1.txt"
out.parent.mkdir(parents=True, exist_ok=True)
out.write_text(prompt)
print(f"✅ planner baseline captured → {out.relative_to(ROOT)}  ({len(prompt)} chars)")
