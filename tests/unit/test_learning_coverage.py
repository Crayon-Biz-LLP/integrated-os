"""
Guard test — learning-loop read-coverage.

Asserts every subsystem that emits observations via emit_observation() has at
least one consuming read site (compute_pattern_confidence or
get_pattern_summary).  This prevents the exact "write-but-never-read" rot
that broke focal_selection, home_mode, fyi_pipeline, and email_drafts.

How it works:
1. Scan production Python files for `subsystem='...'` in emit_observation calls
2. Scan production Python files for `compute_pattern_confidence(..., "...")` and
   `get_pattern_summary("...")` calls
3. Assert every emitter has at least one reader

The scan is deterministic (regex over source) and runs in <1s.  If a new
subsystem is added via emit_observation() without a corresponding read, this
test fails and tells the developer exactly which subsystem is missing its loop.
"""

import ast
import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.learning

# ── Source paths to scan (production code only, no tests/plans/docs) ────────
_SCAN_ROOTS = [
    Path("core"),
    Path("api"),
]
_IGNORE_DIRS = {"__pycache__", "node_modules", ".git", "htmlcov"}
_IGNORE_FILES = {"__init__.py"}

# ── Patterns to extract subsystem names ─────────────────────────────────────
# Matches: subsystem='foo', subsystem="foo", subsystem=f'{x}_pipeline'
_EMIT_PATTERN = re.compile(
    r"""subsystem\s*=\s*(?:f['"][^'"]*['"]|['"]([a-z_]+)['"])"""
)
# Matches: compute_pattern_confidence(..., "foo") or get_pattern_summary("foo")
_READ_PATTERN = re.compile(
    r"""(?:compute_pattern_confidence|get_pattern_summary)\s*\([^)]*['"]([a-z_]+)['"]"""
)


def _scan_emitters() -> set[str]:
    """Find all subsystem names used in emit_observation() calls."""
    emitters = set()
    for root in _SCAN_ROOTS:
        if not root.exists():
            continue
        for py_file in root.rglob("*.py"):
            if py_file.name in _IGNORE_FILES:
                continue
            if any(part in _IGNORE_DIRS for part in py_file.parts):
                continue
            content = py_file.read_text(errors="replace")
            # Only scan near emit_observation calls (within 5 lines)
            for match in re.finditer(r"emit_observation\s*\(", content):
                start = match.start()
                # Look in a window around the call (previous line + next 5)
                lines_before = content[:start].count("\n")
                window = "\n".join(
                    content.split("\n")[max(0, lines_before - 1): lines_before + 6]
                )
                for m in _EMIT_PATTERN.finditer(window):
                    if m.group(1):  # Direct string, not f-string
                        emitters.add(m.group(1))
    return emitters


def _scan_readers() -> set[str]:
    """Find all subsystem names used in compute_pattern_confidence / get_pattern_summary calls."""
    readers = set()
    for root in _SCAN_ROOTS:
        if not root.exists():
            continue
        for py_file in root.rglob("*.py"):
            if py_file.name in _IGNORE_FILES:
                continue
            if any(part in _IGNORE_DIRS for part in py_file.parts):
                continue
            content = py_file.read_text(errors="replace")
            for m in _READ_PATTERN.finditer(content):
                readers.add(m.group(1))
    return readers


def _scan_dynamic_emitters() -> set[str]:
    """Find dynamic subsystem names like f'{channel}_pipeline'."""
    dynamic = set()
    for root in _SCAN_ROOTS:
        if not root.exists():
            continue
        for py_file in root.rglob("*.py"):
            if py_file.name in _IGNORE_FILES:
                continue
            if any(part in _IGNORE_DIRS for part in py_file.parts):
                continue
            content = py_file.read_text(errors="replace")
            # Match: subsystem=f'{channel}_pipeline' or subsystem=f"{x}_pipeline"
            for m in re.finditer(
                r"""subsystem\s*=\s*f['"][^'"]*\{[^}]+\}[^'"]*_pipeline['"]""",
                content,
            ):
                dynamic.add("*_pipeline")  # Mark as dynamic
    return dynamic


def test_all_emitters_have_readers():
    """Every subsystem that emits observations must have at least one reader.

    If this test fails, the failing subsystem needs either:
    1. A compute_pattern_confidence() call in a consumer, OR
    2. A get_pattern_summary() call in a consumer, OR
    3. Removal from emit_observation() if the telemetry is not needed
    """
    emitters = _scan_emitters()
    readers = _scan_readers()
    dynamic = _scan_dynamic_emitters()

    # Dynamic emitters like f'{channel}_pipeline' expand to email_pipeline,
    # call_pipeline, whatsapp_pipeline, teams_pipeline — all should be readers
    if "*_pipeline" in dynamic:
        for suffix in ("email_pipeline", "call_pipeline", "whatsapp_pipeline", "teams_pipeline"):
            readers.add(suffix)

    missing = emitters - readers
    # Filter out known acceptable gaps (subsystems that are hint-only or
    # intentionally write-only for now — add exceptions here with a comment)
    acceptable_gaps = set()  # Add subsystem names here if intentionally write-only

    truly_missing = missing - acceptable_gaps

    assert not truly_missing, (
        f"Learning loop rot detected! These subsystems emit observations "
        f"but have NO consuming read site (compute_pattern_confidence or "
        f"get_pattern_summary): {sorted(truly_missing)}. "
        f"Emitters: {sorted(emitters)}. Readers: {sorted(readers)}. "
        f"Fix: add a read site for each, or remove the emit_observation() call."
    )


def test_subsystems_list_matches_emitters():
    """The SUBSYSTEMS list in telemetry.py should include every emitter.

    This catches the exact bug that broke focal_selection: it was emitting
    but not in SUBSYSTEMS, so get_pattern_summary() never queried it.
    """
    telemetry_path = Path("core/lib/telemetry.py")
    if not telemetry_path.exists():
        pytest.skip("telemetry.py not found")

    content = telemetry_path.read_text(errors="replace")

    # Extract SUBSYSTEMS list via AST
    tree = ast.parse(content)
    subsystems = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "SUBSYSTEMS":
                    if isinstance(node.value, ast.List):
                        for elt in node.value.elts:
                            if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                                subsystems.add(elt.value)

    emitters = _scan_emitters()
    missing_from_list = emitters - subsystems

    assert not missing_from_list, (
        f"These subsystems emit observations but are NOT in the SUBSYSTEMS list "
        f"in telemetry.py: {sorted(missing_from_list)}. "
        f"Add them to SUBSYSTEMS so get_pattern_summary() can query their patterns. "
        f"Current SUBSYSTEMS: {sorted(subsystems)}"
    )
