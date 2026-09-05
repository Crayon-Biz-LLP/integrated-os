"""Guard test — memories writes stay single-row and canonical.

AST-level scan of production code (core/, api/). Closes the
"same-transaction duplicate" class at the structural level:

The db/108 dedup trigger (`dedup_memories_insert`) performs its duplicate
lookup with a plain SELECT, so it CANNOT see uncommitted sibling rows
inserted in the same statement. A single-statement batch insert of N
duplicate rows therefore leaks rows 2..N — the confirmed "I have sent the
pitch deck to David Orban" 3-row chain (all rows, same microsecond, from one
webhook_completion batch). Single-row inserts are each their own statement,
so the trigger sees every previously-committed sibling and swallows the
duplicate.

Invariants:

1. **No batch inserts** — a `memories.insert([...])` whose payload is a list
   / list-comprehension / set / generator is forbidden anywhere in core/ +
   api/. Every memories write must be a single-dict insert (own statement).
   If a producer ever batches again, the same-transaction leak returns.

2. **Executor routes through the canonical writer** — executor.py (the
   closure-history, Guard-B, and fallback paths) must contain ZERO raw
   `memories` insert call sites; every note it writes goes through
   `create_note_direct`. If a future refactor reintroduces a raw insert in
   the executor, this fails.

The scan is deterministic (AST over source) and runs in <1s.

Marker: graph (graph/memory-write integrity).
"""

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.graph

# ── Source paths to scan (production code only, no tests/scripts/docs) ─────
_SCAN_ROOTS = [Path("core"), Path("api")]
_IGNORE_DIRS = {"__pycache__", "htmlcov"}


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
    cur = parent_map.get(node)
    while cur is not None:
        if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return cur.name
        cur = parent_map.get(cur)
    return None


def _memories_inserts(tree, source):
    """Return [(enclosing_func_name, lineno, payload_node)] for memories inserts."""
    parent_map = _build_parent_map(tree)
    hits = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not (isinstance(node.func, ast.Attribute) and node.func.attr == "insert"):
            continue
        seg = ast.get_source_segment(source, node) or ""
        if "memories" not in seg:
            continue
        if not node.args:
            continue
        payload = node.args[0]
        hits.append((_enclosing_func_name(parent_map, node), node.lineno, payload))
    return hits


_BATCH_NODE_TYPES = (ast.List, ast.ListComp, ast.Set, ast.SetComp, ast.GeneratorExp)


def _resolves_to_dict_literal(tree, enclosing, payload):
    """True when the payload is a dict literal or a Name bound to one.

    Single-row inserts are dict literals, or a local var assigned a dict
    literal earlier in the same function (e.g. create_note_direct builds
    `insert_data = {...}` then inserts it). Names bound to anything else
    (a builder call, a list, an unknown) stay flagged for human review.
    """
    if isinstance(payload, ast.Dict):
        return True
    if not isinstance(payload, ast.Name):
        return False
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name != enclosing:
            continue
        for child in ast.walk(node):
            if isinstance(child, ast.Assign):
                if any(isinstance(t, ast.Name) and t.id == payload.id for t in child.targets):
                    if isinstance(child.value, ast.Dict):
                        return True
            elif isinstance(child, ast.AnnAssign) and isinstance(child.target, ast.Name):
                if child.target.id == payload.id and isinstance(child.value, ast.Dict):
                    return True
        return False
    return False


# ── Invariant 1: no batch inserts ──────────────────────────────────────────
def test_no_batch_memories_insert():
    violations = []
    for py_file in _py_files():
        source = py_file.read_text(errors="replace")
        tree = ast.parse(source)
        for enclosing, lineno, payload in _memories_inserts(tree, source):
            if isinstance(payload, _BATCH_NODE_TYPES):
                violations.append(
                    f"{py_file}:{lineno}: memories.insert() with a batch payload "
                    f"({type(payload).__name__}) inside '{enclosing}' — a single-"
                    f"statement batch defeats the db/108 dedup trigger's "
                    f"intra-transaction visibility (same-transaction duplicate class)"
                )
    assert not violations, (
        "Batch memories.insert() found — the same-transaction duplicate class "
        "is reintroduced:\n" + "\n".join(violations)
    )


# ── Invariant 2: executor writes notes only via the canonical writer ───────
def test_executor_has_no_raw_memories_insert():
    executor_path = Path("core/actions/executor.py")
    if not executor_path.exists():
        pytest.skip("core/actions/executor.py not present")
    source = executor_path.read_text(errors="replace")
    tree = ast.parse(source)
    hits = _memories_inserts(tree, source)
    assert not hits, (
        "executor.py contains raw memories.insert() call site(s) — the "
        "closure-history / Guard-B / fallback paths must route every note "
        "through create_note_direct (single canonical writer):\n"
        + "\n".join(f"  line {lineno} ({enclosing})" for enclosing, lineno, _ in hits)
    )


# ── Invariant 3: every memories insert is a single-row dict payload ────────
def test_all_memories_inserts_are_single_row():
    violations = []
    for py_file in _py_files():
        source = py_file.read_text(errors="replace")
        tree = ast.parse(source)
        for enclosing, lineno, payload in _memories_inserts(tree, source):
            # Single-row inserts are dict literals (or a var bound to one).
            if isinstance(payload, _BATCH_NODE_TYPES):
                continue  # covered by invariant 1
            if _resolves_to_dict_literal(tree, enclosing, payload):
                continue
            # Unresolvable payload (builder call, unknown var): can't prove it's
            # a single dict at scan time — flag it so a human confirms.
            violations.append(
                f"{py_file}:{lineno}: memories.insert() payload does not resolve "
                f"to a dict literal ({type(payload).__name__}) inside "
                f"'{enclosing}' — confirm it is always a single-row insert"
            )
    assert not violations, (
        "Non-literal memories.insert() payloads — confirm each is single-row:\n"
        + "\n".join(violations)
    )