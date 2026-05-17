"""
verify_imports.py — Import verification for Integrated-OS refactoring.

Usage:
    python scripts/verify_imports.py [--strict]

Checks that all expected module paths resolve correctly and all .py files
are syntactically valid. New service modules are designed to be side-effect-free
(lazy initialization) so they can be imported without env vars.

Flags:
    --strict    Fail if any *unexpected* module fails (for CI gate)
"""
import importlib
import sys
import os
import py_compile
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("SUPABASE_URL", "https://dummy.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "dummy-key-for-import-check")
os.environ.setdefault("GEMINI_API_KEY", "dummy-key")
os.environ.setdefault("GEMINI_API_KEY", "")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "")
os.environ.setdefault("TELEGRAM_CHAT_ID", "")
os.environ.setdefault("PULSE_SECRET", "")
os.environ.setdefault("GOOGLE_REFRESH_TOKEN", "")
os.environ.setdefault("GOOGLE_CLIENT_ID", "")
os.environ.setdefault("GOOGLE_CLIENT_SECRET", "")
os.environ.setdefault("OPENROUTER_API_KEY", "")

PASS = 0
FAIL = 0
ERRORS = []


def ok(msg):
    global PASS
    PASS += 1
    print(f"  ✓ {msg}")


def fail(msg, detail=""):
    global FAIL
    FAIL += 1
    detail_str = f" — {detail}" if detail else ""
    print(f"  ✗ {msg}{detail_str}")
    ERRORS.append((msg, detail))


def check_dir_exists(path):
    label = str(path.relative_to(ROOT)) if path.is_relative_to(ROOT) else str(path)
    if path.is_dir():
        ok(f"Directory exists: {label}")
    else:
        fail(f"Directory missing: {label}")


def check_file_exists(path):
    label = str(path.relative_to(ROOT)) if path.is_relative_to(ROOT) else str(path)
    if path.is_file():
        ok(f"File exists: {label}")
    else:
        fail(f"File missing: {label}")


def check_py_compiles(path):
    label = str(path.relative_to(ROOT)) if path.is_relative_to(ROOT) else str(path)
    try:
        with open(path) as f:
            compile(f.read(), str(path), "exec")
        ok(f"Syntax OK: {label}")
    except SyntaxError as e:
        fail(f"Syntax error: {label}", str(e))


def try_import(name):
    try:
        importlib.import_module(name)
        ok(f"Import OK: {name}")
    except Exception as e:
        fail(f"Import failed: {name}", str(e))


def check_parse_only(path):
    label = str(path.relative_to(ROOT)) if path.is_relative_to(ROOT) else str(path)
    try:
        with open(path) as f:
            compile(f.read(), str(path), "exec")
        ok(f"Parse OK: {label}")
    except SyntaxError as e:
        fail(f"Parse error: {label}", str(e))


def print_section(title):
    print(f"\n--- {title} ---")


if __name__ == "__main__":
    strict = "--strict" in sys.argv

    print("=" * 60)
    print("Integrated-OS Import Verification")
    print(f"Mode: {'strict' if strict else 'report'}")
    print("=" * 60)

    # ── Phase 0: Directory & init structure ──
    print_section("Phase 0: Directory Structure")
    check_dir_exists(ROOT / "core" / "services")
    check_dir_exists(ROOT / "core" / "models")
    check_dir_exists(ROOT / "core" / "webhook")
    check_dir_exists(ROOT / "core" / "pulse")
    check_dir_exists(ROOT / "core" / "lib")
    check_dir_exists(ROOT / "core" / "agents")
    check_dir_exists(ROOT / "scripts")

    print_section("Package __init__.py files")
    check_file_exists(ROOT / "core" / "services" / "__init__.py")
    check_file_exists(ROOT / "core" / "models" / "__init__.py")
    check_file_exists(ROOT / "core" / "webhook" / "__init__.py")
    check_file_exists(ROOT / "core" / "pulse" / "__init__.py")
    check_file_exists(ROOT / "core" / "lib" / "__init__.py")
    check_file_exists(ROOT / "core" / "agents" / "__init__.py")

    # ── Phase 0: Existing safe modules ──
    print_section("Phase 0: Existing Safe Modules (clean imports)")
    for mod in [
        "core.lib.conversation",
        "core.lib.rate_limiter",
        "core.lib.people_utils",
        "core.lib.constants",
        "core.lib.duplicate_guard",
    ]:
        try_import(mod)

    # ── Phase 0: Unsafe existing modules (parse-only check) ──
    print_section("Phase 0: Existing Unsafe Modules (parse check only)")
    for path in [
        ROOT / "core" / "pulse" / "engine.py",
        ROOT / "core" / "webhook" / "handler.py",
        ROOT / "core" / "agents" / "quick_process.py",
        ROOT / "core" / "agents" / "research_agent.py",
        ROOT / "core" / "agents" / "janitor_check.py",
        ROOT / "core" / "agents" / "cleanup_orphans.py",
        ROOT / "core" / "lib" / "audit_logger.py",
        ROOT / "core" / "lib" / "temporal_lineage.py",
        ROOT / "core" / "pulse_cli.py",
    ]:
        check_parse_only(path)

    # ── Phase 0: Skill modules (parse-only check) ──
    print_section("Phase 0: Skill Modules (parse check only)")
    for path in sorted((ROOT / "core" / "skills").glob("*.py")):
        if path.name == "__init__.py":
            continue
        check_parse_only(path)

    # ── Phase 1: New service modules ──
    print_section("Phase 1: Service Modules (clean imports)")
    for mod in [
        "core.services",
        "core.services.db",
        "core.services.llm",
        "core.services.telegram",
        "core.services.google_service",
        "core.services.outlook_service",
        "core.services.pipeline_service",

    ]:
        try_import(mod)

    # ── Phase 2: Models ──
    print_section("Phase 2: Models")
    try_import("core.models")

    # ── Phase 4: Webhook package ──
    print_section("Phase 4: Webhook Package")
    try_import("core.webhook")
    check_parse_only(ROOT / "core" / "webhook" / "handler.py")
    for mod in [
        "core.webhook.telegram",
        "core.webhook.classify",
        "core.webhook.utils",
        "core.webhook.dispatch",
        "core.webhook.email",
        "core.webhook.commands",
        "core.webhook.multimodal",
    ]:
        try_import(mod)

    # ── Phase 5: Pulse package ──
    print_section("Phase 5: Pulse Package")
    try_import("core.pulse")
    check_parse_only(ROOT / "core" / "pulse" / "engine.py")
    for mod in [
        "core.pulse.llm",
        "core.pulse.utils",
        "core.pulse.graph",
        "core.pulse.memory",
        "core.pulse.practices",
        "core.pulse.calendar",
        "core.pulse.pipeline",
        "core.pulse.resources",
    ]:
        try_import(mod)

    # ── Summary ──
    print("\n" + "=" * 60)
    print(f"Results: {PASS} passed, {FAIL} failed")
    if ERRORS:
        print("\nFailures:")
        for name, detail in ERRORS:
            print(f"  ✗ {name}: {detail}" if detail else f"  ✗ {name}")
    print("=" * 60)

    if strict and FAIL > 0:
        sys.exit(1)
