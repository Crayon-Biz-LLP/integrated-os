#!/usr/bin/env python3
"""run_tests.py — unified test runner (plans/75 §12).

Single entry point for every tier of the test suite. CI, the pre-push hook,
and local runs all call this script so there is exactly ONE definition of
what each tier runs.

Tiers:
    fast       L0 (ruff + residue) + L1 (unit, mock) + L2-mock + app if
               present — NEVER touches the database (invariant, plan §2.1).
               Budget: <= 5 minutes.
    nightly    L2-live + L3 + coverage + leak guard against the TEST tenant
               only. Requires explicit --live opt-in, real secrets, and a
               resolvable Test tenant — never silently against prod (plan
               §16 D2). Budget: <= 20 minutes.
    all        fast, then nightly.

Flags:
    --layer unit|sim|clusters|tenants|root   scope pytest to one directory
    --coverage   attach pytest-cov (nightly-only artifact; needs pytest-cov)
    --no-app     skip Flutter widget tests (pre-push hook uses this)
    --inventory  print the Phase-0 inventory report location
    --live       explicit opt-in for nightly (also accepts LIVE_DB=true)
    --verbose    print each subprocess command

Aspect selection (`run_tests.py pulse`) selects every test tagged with that
primary aspect across all layers (`pytest -m pulse`). Mock by default;
--live adds the database through the nightly guard chain.

Exit code: 0 when every step passed, 1 otherwise (first failure reported).
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Budgets (plan §2): fast 5 min, nightly 20 min.
FAST_BUDGET_S = 5 * 60
NIGHTLY_BUDGET_S = 20 * 60

DUMMY_SECRETS = {"http://localhost:8000", "dummy"}


# ── helpers ────────────────────────────────────────────────────────────────

def run(cmd: list[str], label: str, env: dict | None = None, verbose: bool = False,
        cwd: Path = ROOT) -> bool:
    """Run one step; print label + wall-clock; return success."""
    start = time.monotonic()
    if verbose:
        print(f"$ {' '.join(cmd)}")
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd),
            env=env if env is not None else os.environ.copy(),
        )
    except FileNotFoundError:
        print(f"  ✗ {label} — command not found: {cmd[0]}")
        return False
    elapsed = time.monotonic() - start
    ok = proc.returncode == 0
    mark = "✓" if ok else "✗"
    print(f"  {mark} {label}  ({elapsed:.1f}s)")
    return ok


def guard_fast() -> bool:
    """Invariant: fast contains no live-DB tests (plan §2.1)."""
    live = os.environ.get("LIVE_DB", "").strip().lower()
    if live in ("1", "true", "yes"):
        print("  ✗ Refusing to run fast: LIVE_DB=true is set.")
        print("    Fast is mock-only by invariant (plan §2.1). Nightly is the live tier.")
        return False
    return True


def resolve_test_tenant() -> str | None:
    """Resolve the Test tenant via the canonical mechanism (tests/fixtures/test_tenant.py)."""
    path = ROOT / "tests" / "fixtures" / "test_tenant.py"
    if not path.exists():
        return None
    spec = importlib.util.spec_from_file_location("_runner_test_tenant", path)
    if spec is None or spec.loader is None:
        return None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    try:
        return mod.resolve_test_tenant_uid()
    except Exception:
        return None


def guard_nightly(live_flag: bool) -> dict:
    """Env-contract guard: never silently run live against prod (plan §16)."""
    live_env = os.environ.get("LIVE_DB", "").strip().lower() in ("1", "true", "yes")
    if not (live_flag or live_env):
        print("  ✗ Nightly refuses to run without explicit live opt-in.")
        print("    Pass --live (or set LIVE_DB=true). The shared project IS production (plan §16 D2).")
        return {}
    url = os.environ.get("SUPABASE_URL", "")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
    if not url or not key or url in DUMMY_SECRETS or key in DUMMY_SECRETS:
        print("  ✗ Nightly needs real SUPABASE_URL + SUPABASE_SERVICE_ROLE_KEY (found dummies).")
        print("    These are CI secrets — the runner never runs live against a dummy config.")
        return {}
    uid = resolve_test_tenant()
    if not uid:
        print("  ✗ Test tenant unresolvable (no TEST_TENANT_UID, no users row name='Test' active).")
        print("    Live layers skip instead of risking another tenant's data — nightly refuses to start.")
        return {}
    env = os.environ.copy()
    env["LIVE_DB"] = "true"
    env["TEST_TENANT_UID"] = uid
    return env


def pytest_args(layer: str | None, coverage: bool, verbose: bool) -> list[str]:
    target = {
        None: "tests/",
        "unit": "tests/unit",
        "sim": "tests/sim",
        "clusters": "tests/clusters",
        "tenants": "tests/tenants",
        "root": "tests/test_api.py tests/test_retrieval.py tests/test_rate_limiter.py",
    }.get(layer, "tests/")
    args = [sys.executable, "-m", "pytest", target]
    if coverage:
        args += [
            "--cov=core",
            "--cov=api",
            "--cov-report=term-missing",
            "--cov-report=html:htmlcov",
        ]
    if not verbose:
        args.append("-q")
    return args


def app_tests(env: dict, verbose: bool) -> bool:
    """Flutter widget tests — included in fast only when flutter is present."""
    if not shutil.which("flutter"):
        print("  - app · flutter not on PATH — skipping widget tests (CI/app workflows cover them)")
        return True
    app_dir = ROOT / "rhodey_app"
    if not (app_dir / "test").exists():
        print("  - app · no rhodey_app/test — skipping")
        return True
    return run(["flutter", "test", "--no-pub"], "app · flutter widget tests",
                env=env, verbose=verbose, cwd=app_dir)


# ── tiers ──────────────────────────────────────────────────────────────────

def tier_fast(args) -> bool:
    print("\n=== FAST tier  (L0+L1+L2-mock+app, no DB — budget 5 min) ===")
    if not guard_fast():
        return False
    env = os.environ.copy()
    env.pop("LIVE_DB", None)  # never leak a live flag into mock pytest
    results = [
        run(["ruff", "check", "."], "L0 · ruff", env=env, verbose=args.verbose),
        run([sys.executable, "scripts/scan_tenant1_residue.py"], "L0 · residue gate",
            env=env, verbose=args.verbose),
        run([sys.executable, "scripts/check_marker_presence.py"], "L0 · marker-presence lint",
            env=env, verbose=args.verbose),
    ]
    results.append(run(pytest_args(args.layer, coverage=False, verbose=args.verbose),
                       "L1+L2-mock · pytest (mock mode)", env=env, verbose=args.verbose))
    if not args.no_app:
        results.append(app_tests(env, args.verbose))
    elapsed = _report_budget("fast", results, FAST_BUDGET_S)
    if elapsed is not None and elapsed > FAST_BUDGET_S:
        print("  ⚠️  Fast OVER budget — plan §2 rule: shed a tier, never raise the ceiling.")
        print("     See docs/test-inventory.md §5 (L2-mock build + teardown batching are the fixes).")
    return all(results)


def tier_nightly(args) -> bool:
    print("\n=== NIGHTLY tier  (L2-live+L3+coverage+leak guard, TEST tenant — budget 20 min) ===")
    env = guard_nightly(args.live)
    if not env:
        return False
    print(f"  · Test tenant resolved: {env['TEST_TENANT_UID']}")
    # Leak guard = the session-level residue sweep, which runs inside the live
    # pytest session (tests/conftest.py). validate_deployment.py is NOT run
    # here: it is a deployment-window check needing a deploy timestamp, and it
    # already has its own workflow (validate_deployment.yml) — map, don't
    # duplicate (plan §14.2).
    results = [run(pytest_args(args.layer, coverage=args.coverage, verbose=args.verbose),
                   "L2-live+L3 · pytest (live, TEST tenant only)", env=env, verbose=args.verbose)]
    if args.coverage:
        # Per-layer coverage floors (plan §11 Phase 1): ONE measurement from
        # the single pytest-cov run above, then a floor per source layer
        # enforced via coverage report --include on that same .coverage data
        # (instant — no extra pytest runs, no 13× --cov runtime). The
        # anti-0% rule for new features is the marker-presence lint (L0);
        # these floors are the regression guards on top — a suite that stops
        # exercising the core layer can't be hidden by API coverage and vice
        # versa.
        #
        # Floors are env-configurable (COV_FLOOR / API_COV_FLOOR), default
        # 20, set below the measured unit-only baseline (23%) so the nightly
        # live run (which covers more) passes comfortably while still
        # catching a layer that stops being exercised entirely.
        floors = [
            ("core", "COV_FLOOR", 20, "--include=core/*"),
            ("api", "API_COV_FLOOR", 20, "--include=api/*"),
        ]
        for layer, var, default, include in floors:
            floor = int(os.environ.get(var, str(default)))
            results.append(run(
                [sys.executable, "-m", "coverage", "report",
                 include, f"--fail-under={floor}", "--skip-covered"],
                f"L3 · coverage floor ({layer} >= {floor}%)",
                env=env, verbose=args.verbose))
    # L4 (the 22 UAT scenarios) runs inside the same live pytest session —
    # tests/uat/test_uat_l4.py is part of tests/ and needs TEST_CHAT_IDS
    # (nightly.yml sets it) + the Test tenant. Leak guard covers it.
    elapsed = _report_budget("nightly", results, NIGHTLY_BUDGET_S)
    if elapsed is not None and elapsed > NIGHTLY_BUDGET_S:
        print("  ⚠️  Nightly OVER budget — apply mitigations in order (docs/test-inventory.md §5).")
    return all(results)


def _report_budget(name: str, results: list[bool], budget: int) -> float | None:
    print(f"\n  {name} tier: {'ALL PASS' if all(results) else 'FAILED'}")
    return None  # per-step timings already printed; budget enforcement lives in CI timeout


# ── CLI ────────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Unified test runner (plans/75 §12). Tiers: fast | nightly | all",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("tier", nargs="?", default="fast",
                        help="fast | nightly | all | e2e | <aspect>")
    parser.add_argument("--layer", choices=["unit", "sim", "clusters", "tenants", "root"],
                        help="scope pytest to one directory")
    parser.add_argument("--coverage", action="store_true",
                        help="attach pytest-cov (nightly artifact, not a push gate)")
    parser.add_argument("--no-app", action="store_true",
                        help="skip Flutter widget tests (used by the pre-push hook)")
    parser.add_argument("--live", action="store_true",
                        help="explicit opt-in for the nightly tier")
    parser.add_argument("--inventory", action="store_true",
                        help="print the Phase-0 inventory report location")
    parser.add_argument("--verbose", action="store_true", help="print each command")
    args = parser.parse_args()

    if args.inventory:
        print("Phase-0 inventory: docs/test-inventory.md")
        print("  (753 backend functions · mock baseline 854 pass/145 skip in ~30s ·")
        print("   app: 62 flutter tests incl. 2 goldens + 2 on-device integration_test (X8, v2.13) · L2-live ~8-12 min, teardown-dominated — see §5)")
        return 0

    aspects = {"pulse", "briefing", "sentinel", "decision", "learning", "ingest",
               "webhook", "auth", "calendar", "email", "sync", "retrieval", "graph"}
    if args.tier in aspects:
        # Aspect selection: pytest -m <aspect>, any layer. Mock (no DB) by
        # default; --live adds the DB via the nightly guard chain.
        print(f"\n=== ASPECT: {args.tier} ===")
        if args.live:
            env = guard_nightly(True)
            if not env:
                return 1
            print(f"  · Test tenant resolved: {env['TEST_TENANT_UID']}")
        else:
            if not guard_fast():
                return 1
            env = os.environ.copy()
            env.pop("LIVE_DB", None)
        ok = run([sys.executable, "-m", "pytest", "tests/", "-m", args.tier, "-q"],
                 f"{args.tier} · pytest -m {args.tier}", env=env, verbose=args.verbose)
        print("\n" + ("✅ ASPECT GREEN" if ok else "❌ ASPECT RED"))
        return 0 if ok else 1

    if args.tier == "e2e":
        # L4 — the UAT scenarios alone (live, TEST tenant; nightly-appropriate).
        print("\n=== L4 · UAT scenarios (live, TEST tenant) ===")
        env = guard_nightly(args.live)
        if not env:
            return 1
        ok = run([sys.executable, "-m", "pytest", "tests/uat/test_uat_l4.py", "-q"],
                 "L4 · UAT scenarios (22)", env=env, verbose=args.verbose)
        print("\n" + ("✅ E2E GREEN" if ok else "❌ E2E RED"))
        return 0 if ok else 1

    ok = True
    if args.tier in ("fast", "all"):
        ok = tier_fast(args)
    if args.tier in ("nightly", "all"):
        # nightly needs full tier budget on its own; fast must pass first for 'all'
        ok = tier_nightly(args) and ok
    if args.tier not in ("fast", "nightly", "all"):
        parser.error(f"unknown tier: {args.tier}")
    print("\n" + ("✅ SUITE GREEN" if ok else "❌ SUITE RED — see failures above"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
