"""verify_m9_2_examples.py — M9.2 equivalence gate (plans/70 §M9.2 Step 4).

Proves the data-driven ROLE_UPDATE example swap is safe:

  1. BYTE-IDENTICAL FOR DANNY — the ROLE_UPDATE example line rendered from
     his mocked graph == the example line in the committed baseline
     (tests/golden/classify_tenant1.txt). The ONLY permitted whole-prompt
     diffs vs the baseline are the two whitelisted cosmetic doc lines
     (role_title/org_name field descriptions that were neutralized); any
     other change fails the gate.
  2. NEUTRAL FRESH TENANT — no canonical pages ⇒ the neutral example line.
  3. DETERMINISM — two cold-cache resolutions ⇒ identical output.
  4. FAIL-CLOSED — a DB exception ⇒ neutral line, never a raise.
  5. NO CROSS-TENANT LEAK — cache is keyed by owner id; two tenants resolve
     their own examples, and each cache entry is per-owner.

Run:  python3 scripts/verify_m9_2_examples.py
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

GOLDEN = ROOT / "tests" / "golden" / "classify_tenant1.txt"
from core.services.example_entities import NEUTRAL_EXAMPLE  # noqa: E402
NEUTRAL = NEUTRAL_EXAMPLE

# Danny's stored shape (dispatch.handle_role_update): enrichment.role is the
# full "Role of Org" string; enrichment.organization_name mirrors it.
MARCUS = {
    "label": "Marcus Durai",
    "role": "Pastor of Ashraya Chennai Central",
    "org": "Ashraya Chennai Central",
    "_meta": {"enrichment": {"role": "Pastor of Ashraya Chennai Central", "organization_name": "Ashraya Chennai Central"}},
}
# A second tenant's data — must NEVER bleed into tenant #1's example.
PRIYA_ORG = {
    "label": "Rajesh Kumar",
    "role": "COO of Acme",
    "org": "Acme",
    "_meta": {"enrichment": {"role": "COO of Acme", "organization_name": "Acme"}},
}

# Same fixed inputs as scripts/capture_classify_baseline.py.
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


def render_prompt() -> str:
    from core.prompts.classify import build_classify_intent_prompt
    return build_classify_intent_prompt(**FIXED_INPUTS)


def role_update_line(prompt: str) -> str:
    for line in prompt.splitlines():
        if line.strip().startswith("- ROLE_UPDATE:"):
            return line
    return ""


# ── 1. Byte-identical for Danny (whitelisted-diff gate) ─────────────────────

print("\n[1] Byte-identical for Danny — ROLE_UPDATE example vs committed baseline")

from core.services import example_entities  # noqa: E402
example_entities.clear_cache()

with patch("core.services.example_entities.get_tenant", return_value="uid-danny"), \
     patch("core.services.example_entities._fetch_important_titles", return_value={"marcus durai", "ashraya"}), \
     patch("core.services.example_entities._fetch_role_people", return_value=[MARCUS]):
    rendered = render_prompt()

new_line = role_update_line(rendered)
golden_text = GOLDEN.read_text()
golden_line = role_update_line(golden_text)

check(
    "ROLE_UPDATE example line byte-identical to baseline (Danny's graph → Marcus)",
    new_line == golden_line,
    f"len(new)={len(new_line)} len(golden)={len(golden_line)}",
)

# Whole-prompt diff: the ONLY permitted changes are the two neutralized doc
# lines. Extract doc lines and whitelist them.
doc_old_role = '"role_title": "role title like Pastor or Treasurer (for ROLE_UPDATE only)",'
doc_old_org = '"org_name": "organization name like Ashraya Chennai Central (for ROLE_UPDATE only)",'
doc_new_role = '"role_title": "role title (their role; for ROLE_UPDATE only)",'
doc_new_org = '"org_name": "organization name (their org; for ROLE_UPDATE only)",'

other_diffs = []
for old, new in zip(golden_text.splitlines(), rendered.splitlines()):
    if old != new:
        if old.strip() == doc_old_role and new.strip() == doc_new_role:
            continue
        if old.strip() == doc_old_org and new.strip() == doc_new_org:
            continue
        other_diffs.append((old[:60], new[:60]))

check(
    "Whole-prompt: no diffs beyond the 2 whitelisted doc lines",
    not other_diffs,
    f"{len(other_diffs)} unexpected diff(s)",
)
if other_diffs:
    for old, new in other_diffs[:3]:
        print(f"      -OLD: {old}\n      +NEW: {new}")


# ── 2. Neutral fresh tenant ─────────────────────────────────────────────────

print("\n[2] Neutral example for a fresh tenant (no canonical pages)")
example_entities.clear_cache()
with patch("core.services.example_entities.get_tenant", return_value="uid-fresh"), \
     patch("core.services.example_entities._fetch_important_titles", return_value=set()), \
     patch("core.services.example_entities._fetch_role_people", return_value=[]):
    rendered_fresh = render_prompt()
fresh_line = role_update_line(rendered_fresh)
check(
    "Fresh tenant renders the neutral example (no names, no fake facts)",
    NEUTRAL in fresh_line and "Marcus" not in fresh_line,
    f"len={len(fresh_line)}",
)


# ── 3. Determinism ──────────────────────────────────────────────────────────

print("\n[3] Determinism — two cold-cache resolutions, identical output")
example_entities.clear_cache()
with patch("core.services.example_entities.get_tenant", return_value="uid-danny"), \
     patch("core.services.example_entities._fetch_important_titles", return_value={"marcus durai"}), \
     patch("core.services.example_entities._fetch_role_people", return_value=[MARCUS]):
    a = example_entities.resolve_role_update_example("uid-danny")
example_entities.clear_cache()
with patch("core.services.example_entities.get_tenant", return_value="uid-danny"), \
     patch("core.services.example_entities._fetch_important_titles", return_value={"marcus durai"}), \
     patch("core.services.example_entities._fetch_role_people", return_value=[MARCUS]):
    b = example_entities.resolve_role_update_example("uid-danny")
check("resolve_role_update_example() → identical output on repeat", a == b)


# ── 4. Fail-closed ──────────────────────────────────────────────────────────

print("\n[4] Fail-closed — DB error ⇒ neutral line, never a raise")
example_entities.clear_cache()
with patch("core.services.example_entities.get_tenant", return_value="uid-danny"), \
     patch("core.services.example_entities._fetch_important_titles", side_effect=Exception("db down")), \
     patch("core.services.example_entities._fetch_role_people", return_value=[]):
    try:
        failed = example_entities.resolve_role_update_example("uid-danny")
        check("DB exception → neutral line (no crash)", failed == NEUTRAL)
    except Exception as e:
        check("DB exception → neutral line (no crash)", False, f"raised {type(e).__name__}")


# ── 5b. Real _fetch_role_people: exact-match IN + case-drift fallback ─────

print("\n[5b] _fetch_role_people real logic (title-case labels, case-drift guard)")


class _FakeRes:
    def __init__(self, data):
        self.data = data


class _FakeDb:
    """Minimal fake of the facade for _fetch_role_people."""

    def __init__(self, nodes_by_label: dict):
        self._nodes = nodes_by_label
        self.queries = []

    def table(self, name):
        return _FakeTable(self, name)


class _FakeTable:
    def __init__(self, db, name):
        self._db = db
        self._name = name
        self._in_vals = None
        self._limit = 0

    def select(self, *cols):
        return self

    def eq(self, *a):
        return self

    def in_(self, col, vals):
        self._in_vals = list(vals)
        return self

    def limit(self, n):
        self._limit = n
        return self

    def execute(self):
        self._db.queries.append((self._name, self._in_vals))
        if self._in_vals is not None:
            rows = [self._db._nodes[label] for label in self._in_vals if label in self._db._nodes]
            return _FakeRes(rows)
        # fallback scan path: all nodes
        return _FakeRes(list(self._db._nodes.values()))


marcus_row = {"label": "Marcus Durai", "metadata": {"enrichment": {"role": "Pastor of Ashraya Chennai Central", "organization_name": "Ashraya Chennai Central"}}}

# (i) exact-match path: canonical page title "Marcus Durai" (original case)
# matches the stored label "Marcus Durai"
db_exact = _FakeDb({"Marcus Durai": marcus_row})
found = example_entities._fetch_role_people(db_exact, {"Marcus Durai"}, {"marcus durai"})
check(
    "exact-match IN path finds title-case labels (original-case titles)",
    len(found) == 1 and found[0]["label"] == "Marcus Durai",
)

# (ii) case-drift guard: page title "marcus durai" (lower), label "Marcus Durai"
db_drift = _FakeDb({"Marcus Durai": marcus_row})
found = example_entities._fetch_role_people(db_drift, {"marcus durai"}, {"marcus durai"})
check(
    "case-drift fallback matches label case-insensitively",
    len(found) == 1 and found[0]["label"] == "Marcus Durai",
)

# (iii) no role on the person ⇒ excluded (injection guard)
db_norole = _FakeDb({"Marcus Durai": {"label": "Marcus Durai", "metadata": {"enrichment": {}}}})
found = example_entities._fetch_role_people(db_norole, {"Marcus Durai"}, {"marcus durai"})
check("person without stored role never enters the example", found == [])


# ── 5. No cross-tenant leak ─────────────────────────────────────────────────

print("\n[5] No cross-tenant leak — per-owner cache + per-owner data")
example_entities.clear_cache()
with patch("core.services.example_entities.get_tenant", return_value="uid-danny"), \
     patch("core.services.example_entities._fetch_important_titles", return_value={"marcus durai"}), \
     patch("core.services.example_entities._fetch_role_people", return_value=[MARCUS]):
    danny_example = example_entities.resolve_role_update_example("uid-danny")
with patch("core.services.example_entities.get_tenant", return_value="uid-priya"), \
     patch("core.services.example_entities._fetch_important_titles", return_value={"rajesh kumar"}), \
     patch("core.services.example_entities._fetch_role_people", return_value=[PRIYA_ORG]):
    priya_example = example_entities.resolve_role_update_example("uid-priya")
check("Danny's example uses only Danny's graph", "Marcus" in danny_example and "Acme" not in danny_example)
check("Priya's example uses only Priya's graph", "Rajesh Kumar" in priya_example and "Marcus" not in priya_example)
check("Cache holds two distinct per-owner entries", set(example_entities._cache.keys()) == {"uid-danny", "uid-priya"})


# ── Summary ─────────────────────────────────────────────────────────────────

print()
if FAILURES:
    print(f"❌ M9.2 gate FAILED: {len(FAILURES)} check(s) failed")
    for f in FAILURES:
        print(f"   - {f}")
    sys.exit(1)
print("✅ M9.2 gate GREEN — Danny's example byte-identical; fresh tenants neutral; deterministic; fail-closed; no leaks.")
sys.exit(0)
