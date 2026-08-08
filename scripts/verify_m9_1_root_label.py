"""verify_m9_1_root_label.py — M9.1 equivalence gate.

Proves the graph root-label fix (plans/70 §M9.1):

  1. BEHAVIORAL: the root-label resolver is per-tenant and fail-closed —
       a. tenant with core_config archive_root_label="Danny"  → resolves "Danny"
       b. tenant with only a user_settings name ("Priya")      → resolves "Priya"
       c. no root resolvable                                   → None (fail-closed,
          no root-anchored writes — never a crash, never a fallback label)
  2. STATIC: no hardcoded "Danny" root-label literal remains in the graph
     write paths (backfill_graph.py ×4, graph.py ×1). The legacy
     ["Danny","user"] label list in graph_rules.get_user_node() is now a
     LAST-resort fallback AFTER the settings-driven root lookup — checked
     explicitly so it doesn't regress.

Run:  python3 scripts/verify_m9_1_root_label.py
Exit 0 = gate green.
"""

import os
import re
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

os.environ.setdefault("SUPABASE_URL", "http://localhost:1")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "test")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

FAILURES: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    mark = "✅" if cond else "❌"
    print(f"  {mark} {name}" + (f"  ({detail})" if detail else ""))
    if not cond:
        FAILURES.append(name)


def _cfg_response(content):
    """maybe_single_safe result: data list with/without content."""
    m = MagicMock()
    m.data = [{"content": content}] if content is not None else []
    return m


# ── 1. Behavioral: resolver chain ──────────────────────────────────────────

print("\n[1] Behavioral — resolve_root_label chain")

from core.lib import graph_rules  # noqa: E402


def _resolver_core_config(content):
    """Call graph_rules.resolve_root_label with core_config mocked."""
    with patch.object(graph_rules, "maybe_single_safe", return_value=_cfg_response(content)):
        return graph_rules.resolve_root_label()


# a. core_config archive_root_label wins
check(
    "core_config archive_root_label='Danny' → 'Danny' (tenant #1 byte-identical)",
    _resolver_core_config("Danny") == "Danny",
)

# b. no config → user_settings name
with patch.object(graph_rules, "maybe_single_safe", return_value=_cfg_response(None)), \
     patch("core.services.user_settings.resolve_user_name", return_value="Priya"), \
     patch("core.services.user_settings.current_user_id", return_value="uid-priya"):
    check(
        "no config → user_settings.name='Priya' (new tenant)",
        graph_rules.resolve_root_label() == "Priya",
    )

# c. nothing → None (fail-closed)
with patch.object(graph_rules, "maybe_single_safe", return_value=_cfg_response(None)), \
     patch("core.services.user_settings.resolve_user_name", return_value=None), \
     patch("core.services.user_settings.current_user_id", return_value=None):
    check(
        "no config, no name → None (fail-closed, no fallback label)",
        graph_rules.resolve_root_label() is None,
    )

# d. graph.py has its own copy — same chain
from core.pulse import graph as graph_mod  # noqa: E402

with patch.object(graph_mod, "maybe_single_safe", return_value=_cfg_response("Danny")):
    check(
        "graph.py _root_person_label resolves 'Danny' for tenant #1",
        graph_mod._root_person_label() == "Danny",
    )


# ── 2. Static: no hardcoded "Danny" in the write paths ─────────────────────

print("\n[2] Static — hardcoded 'Danny' literals removed from write paths")

BACKFILL = ROOT / "core/skills/backfill_graph.py"
GRAPH = ROOT / "core/pulse/graph.py"
GRAPH_RULES = ROOT / "core/lib/graph_rules.py"

backfill_src = BACKFILL.read_text()
graph_src = GRAPH.read_text()
graph_rules_src = GRAPH_RULES.read_text()

# backfill_graph.py: all four literal forms must be gone
for pat, label in [
    (r'get_or_create_node\("Danny"', 'get_or_create_node("Danny")'),
    (r'\.eq\("label", "Danny"\)', '.eq("label", "Danny")'),
    (r'\.ilike\("label", "Danny"\)', '.ilike("label", "Danny")'),
    (r'"source_label": "Danny"', '"source_label": "Danny"'),
    (r'unique_nodes\["Danny"\]', 'unique_nodes["Danny"]'),
    (r'if "Danny" not in', 'if "Danny" not in'),
]:
    check(f"backfill_graph.py: no {label}", not re.search(pat, backfill_src))

# graph.py: pending KNOWS edge must not hardcode the root label
check(
    "graph.py:1544 insert_pending_edge no longer hardcodes 'Danny'",
    not re.search(r'insert_pending_edge\(\s*"Danny"', graph_src),
)

# graph_rules.py: the settings-driven root lookup must PRECEDE the legacy list
idx_root = graph_rules_src.find("root_label = resolve_root_label()")
idx_legacy = graph_rules_src.find('in_("label", ["Danny"')
check(
    "graph_rules.get_user_node(): settings-driven root lookup runs before legacy Danny list",
    idx_root != -1 and idx_legacy != -1 and idx_root < idx_legacy,
    f"root@{idx_root} legacy@{idx_legacy}",
)

# The private alias must still resolve to the public function (no drift)
check(
    "graph_rules._root_person_label aliases resolve_root_label (no drift)",
    graph_rules._root_person_label is graph_rules.resolve_root_label,
)


# ── Summary ────────────────────────────────────────────────────────────────

print()
if FAILURES:
    print(f"❌ M9.1 gate FAILED: {len(FAILURES)} check(s) failed")
    for f in FAILURES:
        print(f"   - {f}")
    sys.exit(1)
print("✅ M9.1 gate GREEN — tenant #1 resolves 'Danny'; new tenants resolve their own label; fail-closed on none.")
sys.exit(0)
