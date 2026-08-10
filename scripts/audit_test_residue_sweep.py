#!/usr/bin/env python3
"""audit_test_residue_sweep.py — post-live-testing unbracketed residue sweep.

Phase 2 (session-notes/59) cleaned the DB after the UAT run. Since then the
live-DB integration suites (tests/sim, tests/clusters) run real rows against
the project. This script re-verifies zero test residue:

  1. Test chat_ids (sim suites use a fixed block) must exist ONLY in the
     Test tenant (owner_id = test tenant uid) — anywhere else is a leak.
  2. Bracket markers ([TEST], [SIM_TEST], [UAT]) and known test labels in
     label-ish columns must not appear in any OTHER tenant's rows.
  3. Rows in the Test tenant itself are legitimate (they're the manual-test
     user's data).

SCAN-ONLY by default: prints a per-table report and exits. Pass --delete to
remove confirmed leak rows (rows whose owner is NOT the test tenant).

Schema notes (verified live, Aug 2026):
  - organizations / people / project_creation_signals / enrichment_jobs no
    longer exist (consolidated into graph_nodes / other tables).
  - entity_briefs has NO id column — composite key (owner_id, entity_name,
    entity_type); deletes filter on entity_name + owner_id.
  - pending_graph_clarifications has NO owner_id — chat_id-keyed; scanned by
    chat_id markers only, never auto-deleted (no tenant scope to assert).

Run:  python3 scripts/audit_test_residue_sweep.py [--delete] [--verbose]
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tests.fixtures.test_tenant import fresh_supabase, resolve_test_tenant_uid  # noqa: E402

# Sim-suite test chat ids (from tests/sim — the fixed block used by the flows).
TEST_CHAT_IDS = [
    9000000, 9000001, 9000002, 9000003, 9000005, 9000006, 9000007,
    9000008, 9000009, 9000010, 9000019, 999999999,
]

# Bracket markers that identify test data anywhere (case-sensitive — these
# only ever appear in test rows; a real memory would never contain "[UAT]").
MARKERS = ["[TEST]", "[SIM_TEST]", "[UAT]"]

# Case-sensitive label patterns that identify test data anywhere.
LABEL_PATTERNS = ["Test Rhodey", "SIM_TEST", "TestOrg", "sim-test"]

# Per-table extra patterns (case-sensitive). "Decision-<n>" was UAT
# pending_nodes contamination; a real Danny row like "Decision-Approval Memo"
# must NEVER be matched — restricted to pending_nodes + require a digit.
TABLE_PATTERNS = {
    "pending_nodes": [r"Decision-\d+"],
}

# (table, owner_column, id_column, [text columns to scan]) — owner-scoped.
# id_column=None → composite key; deletes filter on owner + text column.
OWNER_TABLES = [
    ("conversation_workflows", "owner_id", "id", ["chat_id"]),
    ("conversation_threads", "owner_id", "id", ["chat_id"]),
    ("conversations", "owner_id", "id", ["chat_id", "content"]),
    ("tasks", "owner_id", "id", ["title", "notes"]),
    ("memories", "owner_id", "id", ["content"]),
    ("graph_nodes", "owner_id", "id", ["label", "normalized_label"]),
    ("raw_dumps", "owner_id", "id", ["content"]),
    ("pending_graph_edges", "owner_id", "id", ["source_label", "target_label", "source_text"]),
    ("pending_nodes", "owner_id", "id", ["label", "source_text", "context"]),
    ("resources", "owner_id", "id", ["title", "url", "summary"]),
    ("projects", "owner_id", "id", ["name", "description"]),
    ("retrieval_passages", "owner_id", "id", ["text", "raw_text"]),
    ("retrieval_phrase_nodes", "owner_id", "id", ["normalized_text", "display_text"]),
    ("pending_enrichment_jobs", "owner_id", "id", ["content"]),
    ("audit_logs", "owner_id", "id", ["message"]),
    ("entity_briefs", "owner_id", None, ["entity_name", "brief_text"]),
]

# No owner_id — scanned for test chat_ids only, never auto-deleted.
CHAT_KEYED_TABLES = [
    ("pending_graph_clarifications", ["chat_id", "label"]),
]


def _matches(row: dict, cols: list[str], table: str = "") -> tuple[bool, str]:
    table_patterns = TABLE_PATTERNS.get(table, [])
    for col in cols:
        val = row.get(col)
        if val is None:
            continue
        sval = str(val)
        # chat-id tables: check the fixed test block
        if col == "chat_id":
            try:
                if int(sval) in TEST_CHAT_IDS:
                    return True, f"test chat_id {sval}"
            except (TypeError, ValueError):
                pass
        for marker in MARKERS:
            if marker in sval:
                return True, f"marker {marker} in {col}"
        for pat in LABEL_PATTERNS:
            if pat in sval:
                return True, f"label pattern {pat!r} in {col}"
        for pat in table_patterns:
            if re.search(pat, sval):
                return True, f"table pattern {pat!r} in {col}"
    return False, ""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--delete", action="store_true",
                    help="DELETE confirmed leak rows (owner != test tenant)")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    uid = resolve_test_tenant_uid()
    if not uid:
        print("✗ Cannot resolve test tenant uid — refusing to run (would misclassify rows).")
        return 2
    print(f"Test tenant uid: {uid}")

    sb = fresh_supabase()
    total_leaks = 0
    total_test_rows = 0
    chat_keyed_hits = 0
    per_table = []

    for table, owner_col, id_col, cols in OWNER_TABLES:
        try:
            select_cols = (cols + [owner_col])
            if id_col:
                select_cols = [id_col] + select_cols
            res = sb.table(table).select(",".join(select_cols)).execute()
        except Exception as e:
            print(f"  ⚠ {table}: query failed — {type(e).__name__}: {str(e)[:90]}")
            continue
        rows = res.data or []
        leaks, test_rows = [], 0
        for r in rows:
            owner = r.get(owner_col)
            hit, why = _matches(r, cols, table)
            if not hit:
                continue
            if str(owner) == str(uid):
                test_rows += 1
            else:
                leaks.append((r, owner, why))
        total_test_rows += test_rows
        total_leaks += len(leaks)
        per_table.append((table, owner_col, id_col, cols, leaks))
        if args.verbose or leaks or test_rows:
            print(f"  {table}: test-tenant rows={test_rows} leaks={len(leaks)}")
            for row, owner, why in leaks[:8]:
                key = row.get("id") or row.get("entity_name")
                print(f"    ⚠ LEAK key={key} owner={owner} — {why}")
            if len(leaks) > 8:
                print(f"    … {len(leaks) - 8} more")

    for table, cols in CHAT_KEYED_TABLES:
        try:
            res = sb.table(table).select("id," + ",".join(cols)).execute()
        except Exception as e:
            print(f"  ⚠ {table}: query failed — {type(e).__name__}: {str(e)[:90]}")
            continue
        for r in res.data or []:
            hit, why = _matches(r, cols, table)
            if hit:
                chat_keyed_hits += 1
                print(f"  ⚠ {table} id={r.get('id')} — {why} (no owner_id — review manually)")

    print(f"\n=== Summary: {total_leaks} leak rows in non-test tenants, "
          f"{total_test_rows} legitimate test-tenant rows, "
          f"{chat_keyed_hits} chat-keyed hits (no owner) ===")
    if total_leaks == 0:
        print("✓ Clean — no test residue outside the Test tenant.")
        return 0

    if not args.delete:
        print("Scan-only: re-run with --delete to remove the leak rows.")
        return 1

    confirm = input(
        f"Delete {total_leaks} leak rows from the live DB? Type 'yes' to confirm: "
    ).strip()
    if confirm.lower() != "yes":
        print("Aborted — no rows deleted.")
        return 1

    deleted = 0
    for table, owner_col, id_col, cols, leaks in per_table:
        if not leaks:
            continue
        for row, owner, _why in leaks:
            try:
                q = sb.table(table).delete().eq(owner_col, owner)
                if id_col:
                    q = q.eq(id_col, row.get("id"))
                else:
                    # Composite-key tables (entity_briefs): filter on the
                    # identifying text column + owner so we never touch a
                    # real entity that shares a label prefix.
                    for c in cols:
                        if c == "chat_id":
                            continue
                        v = row.get(c)
                        if v is not None:
                            q = q.eq(c, v)
                q.execute()
                deleted += 1
            except Exception as e:
                print(f"  ✗ {table} delete failed: {type(e).__name__}: {str(e)[:90]}")
    print(f"\n=== Deleted {deleted} leak rows ===")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
