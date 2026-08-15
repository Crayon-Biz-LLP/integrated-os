#!/usr/bin/env python3
"""check_marker_presence.py — marker-presence lint (plans/75 §5, L0).

The anti-0% rule: every test must carry its primary aspect marker, so a new
feature can never land with untagged tests (a suite that can't be selected by
aspect is invisible to `-m <aspect>` and to per-aspect coverage).

Rule (Phase-1 convention, exclusive-primary):
  - Every test module must declare a module-level
    `pytestmark = pytest.mark.<aspect>` — the primary aspect for all its tests.
  - Future-proofing: a module WITHOUT pytestmark passes only if EVERY test
    function in it carries at least one @pytest.mark.* decorator.
  - Ops surfaces (rate limiter, providers/failover) are exempt by design
    (plan §3: covered by per-layer floors, tag with the layer only).
  - fixtures/ and conftest.py are not test modules — never checked.

Marker VALUE validation (is the marker registered / an allowed aspect) is
handled by pytest's `--strict-markers` at collection — this script checks
PRESENCE only.

Usage:
    python scripts/check_marker_presence.py     # full tree check
Exit code: 0 = clean, 1 = violations found (CI fails).
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TESTS = ROOT / "tests"

REGISTERED_ASPECTS = {
    "pulse", "briefing", "sentinel", "decision", "learning", "ingest",
    "webhook", "auth", "calendar", "email", "sync", "retrieval", "graph",
    "llm_live", "google_live",
}

# Ops surfaces — no primary aspect by design (plan §3): rate limiter,
# LLM provider failover, the migration-chain replay (infrastructure), the
# API-contract suite (the whole API surface, no single aspect owns it), and
# the health-check wrapper (workflow-gate behavior, no product aspect).
OPS_EXEMPT = {
    "tests/test_rate_limiter.py",
    "tests/test_migrations_replay.py",
    "tests/unit/test_providers_shape.py",
    "tests/unit/test_api_contract.py",
    "tests/unit/test_health_wrapper.py",
}


def _has_pytestmark(tree: ast.Module) -> bool:
    return any(
        isinstance(n, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "pytestmark" for t in n.targets)
        for n in tree.body
    )


def _mark_decorators(node: ast.AST) -> list[str]:
    """Names of @pytest.mark.* decorators on a function/class."""
    marks = []
    for dec in node.decorator_list:
        # pytest.mark.<name>(...) or pytest.mark.<name>
        if (isinstance(dec, ast.Attribute) and isinstance(dec.value, ast.Attribute)
                and dec.value.attr == "mark"):
            marks.append(dec.attr)
    return marks


def _param_marks(node: ast.AST) -> list[str]:
    """Marks carried by pytest.param(..., marks=pytest.mark.<name>) items inside
    a @pytest.mark.parametrize decorator.

    L4-style adapters mark each parametrized scenario with its own primary
    aspect via per-item marks (exclusive-primary holds per TEST, not per
    file) — those count toward the presence rule.
    """
    marks = []
    for dec in node.decorator_list:
        if not (isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute)
                and dec.func.attr == "parametrize"):
            continue
        for arg in dec.args:
            if not isinstance(arg, (ast.List, ast.Tuple)):
                continue
            for item in arg.elts:
                if not isinstance(item, ast.Call) or not isinstance(item.func, ast.Name):
                    continue
                if item.func.id != "param":
                    continue
                for kw in item.keywords:
                    if kw.arg != "marks":
                        continue
                    m = kw.value
                    # marks=pytest.mark.X or marks=[pytest.mark.X, pytest.mark.Y]
                    if isinstance(m, ast.Attribute) and isinstance(m.value, ast.Attribute) \
                            and m.value.attr == "mark":
                        marks.append(m.attr)
                    elif isinstance(m, ast.List):
                        for el in m.elts:
                            if isinstance(el, ast.Attribute) and isinstance(el.value, ast.Attribute) \
                                    and el.value.attr == "mark":
                                marks.append(el.attr)
    return marks


def _test_functions(tree: ast.Module) -> list[ast.AST]:
    """Top-level test functions + classes containing test methods."""
    nodes: list[ast.AST] = []
    for n in tree.body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name.startswith("test_"):
            nodes.append(n)
        elif isinstance(n, ast.ClassDef) and n.name.startswith("Test"):
            for m in n.body:
                if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef)) and m.name.startswith("test_"):
                    nodes.append(m)
    return nodes


def check_file(rel: str) -> list[str]:
    path = ROOT / rel
    try:
        src = path.read_text(encoding="utf-8")
        tree = ast.parse(src)
    except (SyntaxError, OSError) as e:
        return [f"{rel}: unparsable ({type(e).__name__}) — marker lint cannot verify it"]
    if _has_pytestmark(tree):
        return []
    tests = _test_functions(tree)
    if not tests:
        return []
    missing = [n for n in tests if not (_mark_decorators(n) or _param_marks(n))]
    if missing:
        names = ", ".join(f"test_{n.name[5:] if n.name.startswith('test_') else n.name}" for n in missing[:5])
        return [f"{rel}: module has no pytestmark and {len(missing)} test(s) lack a mark "
                f"({names}{'…' if len(missing) > 5 else ''})"]
    return []


def main() -> int:
    violations: list[str] = []
    checked = 0
    for p in sorted(TESTS.rglob("test_*.py")):
        rel = p.relative_to(ROOT).as_posix()
        if "fixtures" in p.parts or p.name == "conftest.py" or rel in OPS_EXEMPT:
            continue
        checked += 1
        violations.extend(check_file(rel))
    print(f"🔎 Marker-presence lint — {checked} test modules checked")
    if not violations:
        print("✅ Every test module carries its primary aspect marker.")
        return 0
    print(f"❌ {len(violations)} violation(s):")
    for v in violations:
        print(f"   {v}")
    print("   Fix: add a module-level `pytestmark = pytest.mark.<aspect>`")
    print(f"   (registered: {', '.join(sorted(REGISTERED_ASPECTS))})")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
