"""Read-only: verify the latest task-related message in Danny's tenant.

Aspect marker: app

Checks the last 48h:
  1. Inbound user messages (raw_dumps, non-system)
  2. Tasks created in the same window — due fields + Google sync
  3. Twin check (same-title tasks)
  4. Verdict per the user's sync rule:
       date+time -> Google Calendar event + Google Task
       date only -> Google Task
       no date   -> simple Rhodey task
"""
import os
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv
from supabase import create_client

load_dotenv(os.path.join(os.getcwd(), ".env"))
sb = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_ROLE_KEY"])
DANNY = "c302706e-fe61-422a-b384-68e3bc8f6f8e"
CUTOFF = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()


def short(ts):
    return (ts or "")[:16].replace("T", " ")


print("=== 1. Inbound user messages (last 48h) ===")
dumps = sb.table("raw_dumps").select("id, created_at, source, content, metadata") \
    .eq("owner_id", DANNY).gte("created_at", CUTOFF) \
    .order("created_at", desc=True).limit(10).execute().data or []
for d in dumps:
    meta = d.get("metadata") or {}
    extras = []
    if meta.get("matched_task_id"):
        extras.append(f"matched_task={meta['matched_task_id']}")
    if meta.get("suggestion_breakdown"):
        extras.append("has_breakdown")
    if meta.get("executed_receipt"):
        extras.append("has_receipt")
    print(f"  #{d['id']} [{short(d['created_at'])}] src={d.get('source','-')}"
          f" :: {str(d.get('content',''))[:90]} {' | '.join(extras)}")

print("\n=== 2. Tasks created in the last 48h ===")
tasks = sb.table("tasks").select(
    "id, title, created_at, deadline, reminder_at, google_event_id, google_task_id, "
    "status, version, is_current, dedup_key, organization_id"
).eq("owner_id", DANNY).gte("created_at", CUTOFF) \
    .order("created_at", desc=True).limit(10).execute().data or []
for t in tasks:
    ev = "EVENT" if t.get("google_event_id") else "no-event"
    gt = "GTASK" if t.get("google_task_id") else "no-gtask"
    print(f"  #{t['id']} [{short(t['created_at'])}] v{t.get('version')} "
          f"cur={t.get('is_current')} status={t.get('status')}")
    print(f"      title: {str(t.get('title',''))[:80]}")
    print(f"      deadline={t.get('deadline') or '-'} reminder_at={short(t.get('reminder_at')) or '-'}"
          f"  {ev} {gt} org={t.get('organization_id') or '-'}")

print("\n=== 3. Twin check (same normalized title, last 48h) ===")
from collections import Counter
norm = Counter(str(t.get("title", "")).strip().lower() for t in tasks)
twins = {k: v for k, v in norm.items() if v > 1}
print(f"  duplicates: {twins if twins else 'none'}")

print("\n=== 4. Google calendar events created in last 48h (Danny) ===")
try:
    events = sb.table("google_calendar_events").select("id, summary, created") \
        .eq("owner_id", DANNY).gte("created", CUTOFF) \
        .order("created", desc=True).limit(5).execute().data or []
    for e in events:
        print(f"  [{short(e['created'])}] {str(e.get('summary',''))[:70]}")
except Exception as ex:
    print(f"  (google_calendar_events not readable: {ex})")
