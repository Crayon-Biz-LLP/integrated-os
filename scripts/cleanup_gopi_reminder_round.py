"""Purge ALL artifacts of the 'Remind me to call Gopi' round from Danny's tenant.

Aspect marker: app

Scope (the Sep 10 17:19-17:27 UTC round):
  - tasks 5866 (direct pipeline) + 5867 (card-confirm twin)
  - graph nodes: Gopi (person), Nithminds Recruitment (organization),
    the task-node mirror of the reminder
  - graph_edges touching those nodes (deleted before nodes — FK order)
  - raw_dumps 4940-4943 (user note, chat reply, telegram ack, card ack)
  - memories (relationship_note / ack notes) created in the window

Dry-run by default. `--execute` performs the deletions, children-first
(FK order: edges -> tasks -> nodes), then re-scans and exits non-zero if
anything survives. Anchored to the 2026-09-10 17:15-17:30 UTC window and
Gopi/Nithminds labels — historical mentions elsewhere are untouched.

Run: python3 scripts/cleanup_gopi_reminder_round.py [--execute]
"""
import os
import re
import subprocess
import sys
from urllib.parse import quote

from dotenv import load_dotenv

load_dotenv(os.path.join(os.getcwd(), ".env"))

EXECUTE = "--execute" in sys.argv

_ref = re.search(r"https?://([^.]+)\.supabase\.co", os.getenv("SUPABASE_URL", "")).group(1)
DSN = f"postgresql://postgres:{quote(os.environ['SUPABASE_DB_PASSWORD'], safe='')}@db.{_ref}.supabase.co:5432/postgres"

WIN_START = "2026-09-10 17:15:00+00"
WIN_END = "2026-09-10 17:30:00+00"


def psql(sql: str) -> str:
    out = subprocess.run(["psql", DSN, "-tA", "-c", sql],
                         capture_output=True, text=True, timeout=90)
    if out.returncode != 0:
        raise RuntimeError(out.stderr.strip())
    return out.stdout.strip()


DANNY = psql("SELECT id FROM users WHERE email='daniel@crayonbiz.com' LIMIT 1").split("|")[0]
print(f"Danny tenant: {DANNY}\n")

# ── Step 1: discovery ────────────────────────────────────────────────────────
# NOTE: word-boundary matching (\ygopi\y) is MANDATORY — ILIKE '%gopi%' also
# matches 'Gopinath YS', a real historical person node + his 3 memory nodes
# (caught in dry-run verification before execute).
GOPI_RE = "\\ygopi\\y"
NITH_RE = "\\ynithminds\\y"
print("=== Discovery ===")
task_ids = [r.split("|")[0] for r in psql(
    f"SELECT id::text FROM tasks WHERE owner_id='{DANNY}' AND (title ~* '{GOPI_RE}' OR notes ~* '{GOPI_RE}') "
).splitlines() if r]
print(f"tasks (Gopi word-boundary): {task_ids or '(none)'}")

node_ids = [r.split("|")[0] for r in psql(
    f"SELECT id::text FROM graph_nodes WHERE owner_id='{DANNY}' "
    f"AND (label ~* '{GOPI_RE}' OR label ~* '{NITH_RE}' "
    f"OR (type='task' AND created_at BETWEEN '{WIN_START}' AND '{WIN_END}'))"
).splitlines() if r]
print(f"graph_nodes (Gopi/Nithminds/window task mirrors): {node_ids or '(none)'}")

edge_count = int(psql(
    f"SELECT count(*) FROM graph_edges WHERE owner_id='{DANNY}' AND ("
    + " OR ".join(f"source_node_id='{n}' OR target_node_id='{n}'" for n in node_ids) + ")"
) or 0) if node_ids else 0
print(f"graph_edges touching those nodes: {edge_count}")

dump_ids = [r.split("|")[0] for r in psql(
    f"SELECT id::text FROM raw_dumps WHERE owner_id='{DANNY}' "
    "AND ((created_at BETWEEN '" + WIN_START + "' AND '" + WIN_END + "') "
    f"OR content ~* '{GOPI_RE}' OR metadata->>'title' ~* '{GOPI_RE}')"
).splitlines() if r]
# The 15-min window is round-exclusive (verified: 4940-4943 all belong to it —
# incl. the card ack whose text carries no 'gopi'), so take it whole.
print(f"raw_dumps (window ∪ Gopi): {dump_ids or '(none)'}")

mem_ids = [r.split("|")[0] for r in psql(
    f"SELECT id::text FROM memories WHERE owner_id='{DANNY}' "
    f"AND created_at BETWEEN '{WIN_START}' AND '{WIN_END}' "
    f"AND (content ~* '{GOPI_RE}' OR content ~* '{NITH_RE}')"
).splitlines() if r]
print(f"memories (window + Gopi/Nithminds): {mem_ids or '(none)'}")

pending_ids = [r.split("|")[0] for r in psql(
    f"SELECT id::text FROM pending_nodes WHERE owner_id='{DANNY}' "
    f"AND (label ~* '{GOPI_RE}' OR label ~* '{NITH_RE}')"
).splitlines() if r]
print(f"pending_nodes: {pending_ids or '(none)'}")

pge_ids = [r.split("|")[0] for r in psql(
    f"SELECT id::text FROM pending_graph_edges WHERE owner_id='{DANNY}' "
    f"AND created_at BETWEEN '{WIN_START}' AND '{WIN_END}' "
    f"AND (source_label ~* '{GOPI_RE}' OR source_label ~* '{NITH_RE}' "
    f"OR target_label ~* '{GOPI_RE}' OR target_label ~* '{NITH_RE}')"
).splitlines() if r]
print(f"pending_graph_edges: {pge_ids or '(none)'}")

total = len(task_ids) + len(node_ids) + edge_count + len(dump_ids) + len(mem_ids) + len(pending_ids) + len(pge_ids)
print(f"\nTotal artifacts found: {total}")

if not EXECUTE:
    print("\nDRY RUN — re-run with --execute to delete.")
    sys.exit(0)

# ── Step 2: delete (children first — FK order) ───────────────────────────────────────
print("\n=== Executing deletions ===")


def _in(ids: list) -> str:
    """Quoted SQL array literal — UUIDs MUST be quoted (unquoted UUID text
    parses as numeric junk). Numeric ids work quoted too."""
    return "ANY(ARRAY[" + ",".join(f"'{i}'" for i in ids) + "]::text[])"


def delete(table: str, where: str, label: str) -> None:
    stmt = f"DELETE FROM {table} WHERE owner_id='{DANNY}' AND " + where + ";"
    out = subprocess.run(
        ["psql", DSN, "-tA", "-c", stmt],
        capture_output=True, text=True, timeout=90,
    )
    tag = out.stdout.strip() or out.stderr.strip()
    print(f"  {label}: {tag if out.returncode == 0 else 'FAILED: ' + tag}")
    if out.returncode != 0:
        raise RuntimeError(f"{label} delete failed: {tag}")


# 1. edges before nodes
if node_ids:
    node_filter = " OR ".join(f"source_node_id='{n}' OR target_node_id='{n}'" for n in node_ids)
    delete("graph_edges", f"({node_filter})", "graph_edges")
# 2. tasks before nodes (tasks.organization_id → graph_nodes)
if task_ids:
    delete("tasks", f"id::text = {_in(task_ids)}", "tasks")
# 3. nodes
if node_ids:
    delete("graph_nodes", f"id::text = {_in(node_ids)}", "graph_nodes")
# 4. append-only / independent tables
if dump_ids:
    delete("raw_dumps", f"id::text = {_in(dump_ids)}", "raw_dumps")
if mem_ids:
    delete("memories", f"id::text = {_in(mem_ids)}", "memories")
if pending_ids:
    delete("pending_nodes", f"id::text = {_in(pending_ids)}", "pending_nodes")
if pge_ids:
    delete("pending_graph_edges", f"id::text = {_in(pge_ids)}", "pending_graph_edges")

# ── Step 3: post-delete verification (mandatory) ─────────────────────────────
print("\n=== Post-delete verification ===")
survivors = {
    "tasks (Gopi)": psql(
        f"SELECT count(*) FROM tasks WHERE owner_id='{DANNY}' AND (title ~* '{GOPI_RE}' OR notes ~* '{GOPI_RE}')"),
    "graph_nodes (Gopi/Nithminds)": psql(
        f"SELECT count(*) FROM graph_nodes WHERE owner_id='{DANNY}' AND (label ~* '{GOPI_RE}' OR label ~* '{NITH_RE}')"),
    "raw_dumps (window)": psql(
        f"SELECT count(*) FROM raw_dumps WHERE owner_id='{DANNY}' AND created_at BETWEEN '{WIN_START}' AND '{WIN_END}' AND content ~* '{GOPI_RE}'"),
    "memories (window)": psql(
        f"SELECT count(*) FROM memories WHERE owner_id='{DANNY}' AND created_at BETWEEN '{WIN_START}' AND '{WIN_END}' AND (content ~* '{GOPI_RE}' OR content ~* '{NITH_RE}')"),
}
failed = False
for k, v in survivors.items():
    print(f"  {k}: {v} remaining")
    if v.strip() != "0":
        failed = True

if failed:
    print("\n❌ SURVIVORS REMAIN — investigate before re-testing.")
    sys.exit(1)
print("\n✅ Danny's tenant is clean — ready for the re-test.")
