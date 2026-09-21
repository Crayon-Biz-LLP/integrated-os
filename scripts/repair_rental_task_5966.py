"""One-shot repair for the Sep 14 incident (explicitly approved by the user).

Undoes the erroneous closure of task #5966 across the OS and restores it to
tomorrow 5pm IST, then neutralizes the stale clarification artifacts so the
deployed confirmation-flow fixes start from a clean slate.

Uses the OS's own machinery (NOT ad-hoc writes):
  - core.pulse.tools.update_task_status('todo', reminder_at=...) — the same
    tool the executor/planner use; reopens the task, resyncs the Google Task,
    and recreates the Google Calendar event (executor compensation semantics:
    reopen + clear completed_at).
  - Direct deletes ONLY for the two artifacts the closure wrongly produced:
    the completion memory and the two dropped-button clarification rows.

FOLLOW-UP (same day, user clarification): the reminder was then restored to
its ORIGINAL pre-incident slot — Sept 14 5:00pm IST (11:30 UTC) — undoing the
reschedule entirely, via the executor's reschedule semantics (sync_to_calendar
patch of event 72a6pd1uv9chp62442g471eis8 + sync_to_google due resync). NOTE:
update_task_status alone cannot do this on an already-open task — its
"already <status>" no-change guard returns before the calendar-resync block;
use the sync_to_calendar/sync_to_google pair instead.

Aspect marker: decision

Run: python3 scripts/repair_rental_task_5966.py
"""
import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv(os.path.join(os.getcwd(), ".env"))

from core.lib.time_utils import get_user_timezone  # noqa: E402
from core.pulse.tools import update_task_status  # noqa: E402
from core.services.db import tenant_aware_client, tenant_scope  # noqa: E402

DANNY = "c302706e-fe61-422a-b384-68e3bc8f6f8e"
TASK_ID = 5966
IST = ZoneInfo("Asia/Kolkata")
COMPLETION_MEMORY_ID = 7630
STALE_CLARIFICATION_IDS = [5108, 5109]  # the two dropped-button prompts


def show_task(sb):
    t = sb.table("tasks").select("*").eq("id", TASK_ID).limit(1).execute().data[0]
    print(f"  #{t['id']} status={t['status']} v{t['version']} cur={t['is_current']}")
    print(f"    title: {t['title']}")
    print(f"    reminder_at={t.get('reminder_at')} deadline={t.get('deadline') or '-'} "
          f"completed_at={t.get('completed_at') or '-'}")
    print(f"    google_event_id={t.get('google_event_id') or '-'} "
          f"google_task_id={t.get('google_task_id') or '-'}")
    return t


def main():
    with tenant_scope(DANNY):
        sb = tenant_aware_client()

        print("=== BEFORE ===")
        before = show_task(sb)

        if before["status"] != "done":
            print(f"\n⚠️ Task is status={before['status']!r}, not 'done' — nothing to reopen.")
            if input("Continue anyway with the reschedule? (y/N): ").strip().lower() != "y":
                return

        # ── 1. Undo the closure + reschedule to tomorrow 5pm IST ──
        # One update_task_status call: reopen (todo), resync the Google Task,
        # recreate the calendar event at the new reminder time. Same semantics
        # the executor's compensation path relies on.
        tz = get_user_timezone()
        now_local = datetime.now(tz)
        tomorrow_5pm = (now_local + timedelta(days=1)).replace(
            hour=17, minute=0, second=0, microsecond=0)
        new_reminder = tomorrow_5pm.isoformat()
        print(f"\n=== REPAIR ===\n  new reminder_at = {new_reminder} ({IST.key} wall clock: "
              f"{tomorrow_5pm.astimezone(IST).strftime('%Y-%m-%d %H:%M %Z')})")

        result = update_task_status(task_id=TASK_ID, status="todo", reminder_at=new_reminder)
        print(f"  update_task_status: {result}")
        if "FAIL" in result:
            raise SystemExit("update_task_status failed — aborting before artifact cleanup.")

        # Mirror compensate_action: closure stamped completed_at; reopening must clear it.
        sb.table("tasks").update({"completed_at": None}).eq("id", TASK_ID).execute()

        # Deadline follows the new date (date-only rule from the planner contract).
        sb.table("tasks").update({"deadline": str(tomorrow_5pm.date())}).eq("id", TASK_ID).execute()

        # ── 2. Delete the completion memory the closure produced (id 7630) ──
        mem = sb.table("memories").select("id, content, memory_type").eq("id", COMPLETION_MEMORY_ID).execute()
        if mem.data:
            content = mem.data[0].get("content", "")
            if "sign the rental agreement" in content.lower():
                sb.table("memories").delete().eq("id", COMPLETION_MEMORY_ID).execute()
                print(f"  deleted completion memory #{COMPLETION_MEMORY_ID}: {content[:80]}")
            else:
                print(f"  ⚠️ memory #{COMPLETION_MEMORY_ID} content mismatch — NOT deleted: {content[:80]}")
        else:
            print(f"  memory #{COMPLETION_MEMORY_ID} not found (already gone)")

        # ── 3. Neutralize the stale dropped-button clarifications ──
        # Raw-dump artifacts of the two 10:44/10:33 prompts. Marked, not deleted
        # (audit trail). conversations rows are left intact for the same reason;
        # the deployed resolved-marker mechanism will consume them once.
        try:
            upd = sb.table("raw_dumps").update(
                {"status": "clarification_cancelled"}).in_("id", STALE_CLARIFICATION_IDS).execute()
            print(f"  neutralized {len(upd.data or [])} stale clarification dumps: {STALE_CLARIFICATION_IDS}")
        except Exception as e:
            print(f"  ⚠️ could not mark clarification dumps (non-fatal, leaving intact): {e}")

        print("\n=== AFTER ===")
        after = show_task(sb)

        # ── 4. Google Task completion state (best-effort mirror check) ──
        g_id = after.get("google_task_id")
        if g_id:
            try:
                from googleapiclient.discovery import build
                from google.oauth2.credentials import Credentials
                from core.services.google_service import get_refresh_token
                creds = Credentials(None, refresh_token=get_refresh_token(DANNY),
                                    token_uri="https://oauth2.googleapis.com/token",
                                    client_id=os.getenv("GOOGLE_CLIENT_ID"),
                                    client_secret=os.getenv("GOOGLE_CLIENT_SECRET"))
                svc = build("tasks", "v1", credentials=creds)
                gt = svc.tasks().get(tasklist="@default", task=g_id).execute()
                print(f"\n  Google Task: status={gt.get('status')} due={gt.get('due')} "
                      f"completed={gt.get('completed')}")
            except Exception as e:
                print(f"\n  (Google Task mirror check failed, non-fatal: {e})")

        ok = (after["status"] == "todo"
              and (after.get("reminder_at") or "").startswith(str(tomorrow_5pm.date()))
              and after.get("google_event_id")
              and not after.get("completed_at"))
        print("\n=== VERDICT:", "REPAIRED ✅" if ok else "INCOMPLETE ❌ — review above", "===")


if __name__ == "__main__":
    main()
