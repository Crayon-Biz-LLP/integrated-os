"""Guard test — pending-node creation lives only in decision-gated writers.

AST-level scan of production code (core/, api/). Prevents the exact rot we
have been fixing all session: extraction regaining write capability, or a
background producer (sentinel, enrichment queue, new channel, refactor)
silently creating pending_nodes the user never asked for.

Invariants:

1. **Extraction purity** — `_create_pending_org` / `_create_pending_person`
   (entity_context.py) may only be called from `queue_pending_candidates`,
   the single decision-gated writer (card confirm, message/email approval).
   If extraction ever regains write capability, this fails.

2. **Pending INSERT sites** — every `table("pending_nodes").insert(...)` in
   core/ + api/ must sit inside an allowlisted writer:
     - `_create_pending_org` / `_create_pending_person` (via the gated queue)
     - `insert_pending_node` / `persist_label` (frozen legacy chain, see #3)
     - `detect_practices` (allowlisted product feature → Quick Confirmation)
     - `graph_node_delete_route` (user-initiated delete; rejected blocklist row)

3. **Frozen legacy chain** — the old extraction engine
   (`extract_and_link_entities` → `insert_extracted_entities` → `persist_label`)
   is dead. `extract_and_link_entities` must have ZERO live callers, and each
   downstream function may only be called by its frozen predecessor. If anyone
   re-wires the old engine into a live path, this fails.

The scan is deterministic (AST over source) and runs in <1s.

Marker: graph (graph/pending-node integrity).
"""

import ast
import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.graph

# ── Source paths to scan (production code only, no tests/scripts/docs) ─────
_SCAN_ROOTS = [Path("core"), Path("api")]
_IGNORE_DIRS = {"__pycache__", "htmlcov"}

# ── Allowlists ──────────────────────────────────────────────────────────────

# Enclosing functions that may contain a direct pending_nodes INSERT.
_PENDING_INSERT_FUNC_ALLOWLIST = {
    "_create_pending_org",        # entity_context.py — via queue_pending_candidates
    "_create_pending_person",     # entity_context.py — via queue_pending_candidates
    "insert_pending_node",        # node_tables.py util — caller pinned below
    "persist_label",              # graph_rules.py — frozen legacy chain
    "detect_practices",           # practices.py — allowlisted product feature
    "graph_node_delete_route",    # api/index.py — user delete → rejected row
}

# Per-writer caller allowlists (enclosing function of every call site).
_WRITER_CALLER_ALLOWLIST = {
    "_create_pending_org": {"queue_pending_candidates"},
    "_create_pending_person": {"queue_pending_candidates"},
    "insert_pending_node": {"persist_label"},
}

# Frozen legacy chain: the old LLM extraction engine. Head must have no live
# callers; each node may only be called by its frozen predecessor.
_FROZEN_CALLER_ALLOWLIST = {
    "extract_and_link_entities": set(),
    "insert_extracted_entities": {"extract_and_link_entities"},
    "persist_label": {"insert_extracted_entities"},
}


# ── Scan helpers ────────────────────────────────────────────────────────────

def _py_files():
    for root in _SCAN_ROOTS:
        if not root.exists():
            continue
        for py_file in root.rglob("*.py"):
            if any(part in _IGNORE_DIRS for part in py_file.parts):
                continue
            yield py_file


def _build_parent_map(tree):
    parents = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
    return parents


def _enclosing_func_name(parent_map, node):
    """Nearest enclosing def/async-def name, or None at module level."""
    cur = parent_map.get(node)
    while cur is not None:
        if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return cur.name
        cur = parent_map.get(cur)
    return None


def _collect_calls(tree, target_names):
    """Return [(func_name, enclosing_func_name, lineno)] for direct Name calls."""
    parent_map = _build_parent_map(tree)
    calls = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not isinstance(node.func, ast.Name):
            continue
        fname = node.func.id
        if fname in target_names:
            calls.append((fname, _enclosing_func_name(parent_map, node), node.lineno))
    return calls


def _pending_node_inserts(tree, source):
    """Return [(enclosing_func_name, lineno)] for pending_nodes .insert(...) calls."""
    parent_map = _build_parent_map(tree)
    hits = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not (isinstance(node.func, ast.Attribute) and node.func.attr == "insert"):
            continue
        seg = ast.get_source_segment(source, node) or ""
        if re.search(r"['\"]pending_nodes['\"]\s*\)\s*\.\s*insert\s*\(", seg):
            hits.append((_enclosing_func_name(parent_map, node), node.lineno))
    return hits


# ── Invariant 1: extraction purity ─────────────────────────────────────────

def test_extraction_never_writes_pending():
    violations = []
    for py_file in _py_files():
        tree = ast.parse(py_file.read_text(errors="replace"))
        for fname, enclosing, lineno in _collect_calls(
            tree, {"_create_pending_org", "_create_pending_person", "insert_pending_node"}
        ):
            allowed = _WRITER_CALLER_ALLOWLIST.get(fname, set())
            if enclosing not in allowed:
                violations.append(
                    f"{py_file}:{lineno}: {fname}() called from "
                    f"'{enclosing}' — pending-node writers may only be called "
                    f"from {sorted(allowed)}"
                )
    assert not violations, (
        "Pending-node writers escaped their decision-gated allowlist:\n"
        + "\n".join(violations)
    )


# ── Invariant 2: pending INSERT sites ──────────────────────────────────────

def test_pending_node_inserts_only_in_allowlisted_writers():
    violations = []
    for py_file in _py_files():
        source = py_file.read_text(errors="replace")
        tree = ast.parse(source)
        for enclosing, lineno in _pending_node_inserts(tree, source):
            if enclosing not in _PENDING_INSERT_FUNC_ALLOWLIST:
                violations.append(
                    f"{py_file}:{lineno}: pending_nodes.insert() inside "
                    f"'{enclosing}' — not an allowlisted decision-gated writer"
                )
    assert not violations, (
        "pending_nodes.insert() found outside the decision-gated allowlist:\n"
        + "\n".join(violations)
    )


# ── Invariant 3: frozen legacy chain ───────────────────────────────────────

def test_legacy_extraction_engine_frozen():
    violations = []
    for py_file in _py_files():
        tree = ast.parse(py_file.read_text(errors="replace"))
        for fname, enclosing, lineno in _collect_calls(tree, set(_FROZEN_CALLER_ALLOWLIST)):
            allowed = _FROZEN_CALLER_ALLOWLIST.get(fname, set())
            if enclosing not in allowed:
                violations.append(
                    f"{py_file}:{lineno}: {fname}() called from '{enclosing}' — "
                    f"the legacy extraction engine is frozen; allowed callers: "
                    f"{sorted(allowed) or 'NONE (dead code)'}"
                )
    assert not violations, (
        "The legacy extraction engine gained a live caller:\n"
        + "\n".join(violations)
    )