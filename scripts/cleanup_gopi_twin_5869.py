"""Delete the Gopi twin task #5869 (card-confirm duplicate) from Danny's tenant.

Aspect marker: app

The twin: #5869 was created by the suggestion-card confirm 88s after the
direct pipeline created #5868 (dedup keys diverged because the org state
changed between arrival and confirm). #5869 carries its own Google Calendar
event + Google Task — deleting it must delete those first so the calendar
isn't left double-booked or orphaned.

Order: 1) fetch artifact IDs + prove the row is the twin, 2) delete Google
Calendar event + Google Task, 3) delete referencing rows (edges, pending
edges, task mirror node), 4) delete the task row, 5) verify #5868 intact.

Dry-run by default. `--execute` performs deletions.
"""
import os
import re
import subprocess
import sys
from urllib.parse import quote

from dotenv import load_dotenv

load_dotenv(os.path.join(os.getcwd(), ".env"))
sys.path.insert(0, os.getcwd())  # allow `from core...` imports when run as a script

EXECUTE = "--execute" in sys.argv

_ref = re.search(r"https?://([^.]+)\.supabase\.co", os.getenv("SUPABASE_URL", "")).group(1)
DSN = f"postgresql://postgres:{quote(os.environ['SUPABASE_DB_PASSWORD'], safe='')}@db.{_ref}.supabase.co:5432/postgres"

TWIN_ID = 5869
KEEP_ID = 5868
DANNY = "c302706e-fe61-422a-b384-68e3bc8f6f8e"


def psql(sql: str) -> str:
    out = subprocess.run(["psql", DSN, "-tA", "-c", sql], capture_output=True, text=True)
    if out.returncode != 0:
        raise SystemExit(f"psql failed: {out.stderr}\nSQL: {sql[:200]}")
    return out.stdout.strip()


print("=== Step 1: prove the row is the twin + fetch Google artifact IDs ===")
row = psql(f"""
SELECT id, title, version, is_current, COALESCE(google_event_id,'-'), COALESCE(google_task_id,'-'),
       COALESCE(supersedes_id::text,'-'), created_at
FROM tasks WHERE id = {TWIN_ID} AND owner_id = '{DANNY}';
""")
print(row)
if not row:
    print(f"Task {TWIN_ID} not found in Danny's tenant — nothing to do (already cleaned?).")
    sys.exit(0)

parts = row.split("|")
# Field order: id(0) title(1) version(2) is_current(3) event(4) task(5) supersedes(6) created(7)
if len(parts) < 8 or parts[2] != "1" or parts[3] != "t" or parts[6] != "-":
    raise SystemExit(f"SAFETY ABORT: task {TWIN_ID} no longer looks like the standalone twin "
                     f"(version={parts[2] if len(parts)>2 else '?'}, is_current={parts[3] if len(parts)>3 else '?'}, "
                     f"supersedes={parts[6] if len(parts)>6 else '?'}). "
                     "Refusing to delete — inspect manually.")

event_id = None if parts[4] == "-" else parts[4]
task_id = None if parts[5] == "-" else parts[5]
print(f"\nGoogle event: {event_id or 'none'} | Google task: {task_id or 'none'}")

keep = psql(f"SELECT id, COALESCE(google_event_id,'-') FROM tasks WHERE id = {KEEP_ID} AND owner_id = '{DANNY}';")
if not keep:
    raise SystemExit(f"SAFETY ABORT: canonical task {KEEP_ID} missing — refusing to proceed.")
print(f"Canonical task present: {keep}")

if EXECUTE and (event_id or task_id):
    print("\n=== Step 2: delete Google artifacts ===")
    from core.services.google_service import delete_calendar_event, delete_google_task
    if event_id:
        ok = delete_calendar_event(event_id)
        print(f"  calendar event {event_id}: {'deleted' if ok else 'DELETE FAILED'}")
    if task_id:
        ok = delete_google_task(task_id)
        print(f"  google task {task_id}: {'deleted' if ok else 'DELETE FAILED'}")

print("\n=== Step 3: referencing rows ===")
# Task-mirror nodes would link via db_record_id; verified none exist for either
# twin this round. Pending edges (34480/34481) key on the task TITLE, which the
# kept task #5868 shares — they belong to the kept task and must NOT be deleted.
refs = psql(f"""
SELECT 'graph_nodes(task_mirror)' AS t, id::text FROM graph_nodes
  WHERE owner_id='{DANNY}' AND type='task' AND db_record_id::text='{TWIN_ID}';
""")
print(refs or "(none — nothing referencing the twin in graph tables)")

if EXECUTE:
    print("\n=== Executing deletions (children first) ===")
    if refs:
        psql(f"DELETE FROM graph_edges WHERE owner_id='{DANNY}' AND (source_node_id IN (SELECT id FROM graph_nodes WHERE owner_id='{DANNY}' AND type='task' AND db_record_id::text='{TWIN_ID}') OR target_node_id IN (SELECT id FROM graph_nodes WHERE owner_id='{DANNY}' AND type='task' AND db_record_id::text='{TWIN_ID}'));")
        psql(f"DELETE FROM graph_nodes WHERE owner_id='{DANNY}' AND type='task' AND db_record_id::text='{TWIN_ID}';")
        print("  graph edges + task mirror node deleted")
    psql(f"DELETE FROM tasks WHERE id = {TWIN_ID} AND owner_id = '{DANNY}';")
    print(f"  task {TWIN_ID} deleted")

print("\n=== Step 4: verification ===")
print(psql(f"SELECT 'twin gone' AS check, CASE WHEN COUNT(*)=0 THEN 'OK' ELSE 'STILL PRESENT' END FROM tasks WHERE id={TWIN_ID};"))
print(psql(f"SELECT 'canonical intact' AS check, COUNT(*)::text || ' row, event=' || COALESCE(MAX(google_event_id),'-') FROM tasks WHERE id={KEEP_ID};"))
if not EXECUTE:
    print("\nDRY RUN — re-run with --execute to perform deletions.")
