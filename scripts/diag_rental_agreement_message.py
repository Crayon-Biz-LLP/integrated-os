"""Read-only: find the 'move ... sign the rental agreement' message and what happened to it.

Aspect marker: app
"""
import os

from dotenv import load_dotenv
from supabase import create_client

load_dotenv(os.path.join(os.getcwd(), ".env"))
sb = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_ROLE_KEY"])
DANNY = "c302706e-fe61-422a-b384-68e3bc8f6f8e"

print("=== 1. Inbound messages (last 48h, newest first) ===")
dumps = sb.table("raw_dumps").select("id, created_at, source, direction, sender, content, metadata") \
    .eq("owner_id", DANNY).eq("direction", "inbound") \
    .order("created_at", desc=True).limit(20).execute().data or []
for d in dumps:
    content = str(d.get("content", "")).replace("\n", " ")
    print(f"  #{d['id']} [{d['created_at'][:16]}] src={d.get('source','-')} :: {content[:110]}")

print("\n=== 2. Outgoing replies + suggestion dumps (last 48h) ===")
out = sb.table("raw_dumps").select("id, created_at, source, direction, message_type, content, metadata") \
    .eq("owner_id", DANNY).eq("direction", "outgoing") \
    .order("created_at", desc=True).limit(20).execute().data or []
for d in out:
    content = str(d.get("content", "")).replace("\n", " ")
    print(f"  #{d['id']} [{d['created_at'][:16]}] src={d.get('source','-')} type={d.get('message_type','-')} :: {content[:110]}")

print("\n=== 3. Tasks mentioning rental/agreement/lease (any state) ===")
tasks = sb.table("tasks").select(
    "id, title, created_at, updated_at, deadline, reminder_at, status, version, "
    "is_current, completed_at, google_event_id, google_task_id"
).eq("owner_id", DANNY).or_(
    "title.ilike.%rental%,title.ilike.%agreement%,title.ilike.%lease%"
).order("updated_at", desc=True).limit(10).execute().data or []
for t in tasks:
    print(f"  #{t['id']} [{t['updated_at'][:16] if t.get('updated_at') else t['created_at'][:16]}] "
          f"status={t['status']} v{t.get('version')} cur={t.get('is_current')}")
    print(f"      title: {str(t.get('title', ''))[:90]}")
    print(f"      deadline={t.get('deadline') or '-'} reminder_at={(t.get('reminder_at') or '-')[:16]} "
          f"completed_at={(t.get('completed_at') or '-')[:16] if t.get('completed_at') else '-'} "
          f"event={'Y' if t.get('google_event_id') else 'n'} gtask={'Y' if t.get('google_task_id') else 'n'}")

print("\n=== 4. All tasks updated in last 24h ===")
from datetime import datetime, timedelta, timezone
cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
recent = sb.table("tasks").select(
    "id, title, updated_at, status, version, is_current, completed_at, reminder_at"
).eq("owner_id", DANNY).gte("updated_at", cutoff) \
    .order("updated_at", desc=True).limit(15).execute().data or []
for t in recent:
    print(f"  #{t['id']} [{t['updated_at'][:16]}] status={t['status']} v{t.get('version')} "
          f"cur={t.get('is_current')} completed_at={(t.get('completed_at') or '-')[:16] if t.get('completed_at') else '-'} "
          f":: {str(t.get('title', ''))[:70]}")

print("\n=== 5. Audit log entries mentioning rental/agreement (last 48h) ===")
try:
    logs = sb.table("audit_log").select("created_at, level, message") \
        .eq("owner_id", DANNY).gte("created_at", cutoff) \
        .order("created_at", desc=True).limit(50).execute().data or []
    for l in logs:
        msg = str(l.get("message", ""))
        if any(k in msg.lower() for k in ("rental", "agreement", "lease", "close", "reschedul", "move")):
            print(f"  [{l['created_at'][11:19]}] {l.get('level')}: {msg[:130]}")
except Exception as ex:
    print(f"  (audit_log not readable: {ex})")
