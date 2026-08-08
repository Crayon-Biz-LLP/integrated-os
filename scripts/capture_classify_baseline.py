"""capture_classify_baseline.py — M9.2 Step 1: baseline BEFORE any edit.

Renders the intent-classification prompt exactly as tenant #1 (Danny) sees it
TODAY (with the hardcoded ROLE_UPDATE example) and commits the output to
tests/golden/classify_tenant1.txt. That committed artifact is the byte-diff
target for scripts/verify_m9_2_examples.py — the gate proves M9.2's template
swap renders the SAME prompt for Danny.

Why no live DB: build_classify_intent_prompt() falls back to env/defaults
(Danny-era seeded values) when no tenant scope is active — identical to what
Danny's seeded rows produce (M2 equivalence). So the baseline is deterministic
and reproducible in CI.

Run BEFORE editing core/prompts/classify.py:
    python3 scripts/capture_classify_baseline.py
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

GOLDEN = ROOT / "tests" / "golden" / "classify_tenant1.txt"

# Fixed inputs — MUST stay identical between capture and verify scripts so the
# byte-diff is apples-to-apples.
FIXED_INPUTS = dict(
    text="Marcus Durai is the new Pastor of Ashraya Chennai Central",
    time_phase="morning",
    core_json="[]",
    entities_section="",
    learned_section="",
    context_str="",
    conversation_history="",
    user_name=None,       # → defaults (Danny)
    routing_rules=None,   # → defaults (Danny's domains)
)


def render_current_prompt() -> str:
    """Render the classify prompt exactly as shipped today."""
    from core.services import user_settings
    user_settings.clear_cache()
    from core.prompts.classify import build_classify_intent_prompt
    return build_classify_intent_prompt(**FIXED_INPUTS)


def main() -> int:
    rendered = render_current_prompt()
    GOLDEN.parent.mkdir(parents=True, exist_ok=True)
    prior = GOLDEN.read_text() if GOLDEN.exists() else None
    if prior == rendered:
        print(f"✅ Golden already current: {GOLDEN.relative_to(ROOT)}")
        return 0
    GOLDEN.write_text(rendered)
    print(f"✅ Golden written ({len(rendered)} chars): {GOLDEN.relative_to(ROOT)}")
    print("   (run this BEFORE editing core/prompts/classify.py — it must")
    print("    capture the pre-change rendering)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
