from core.services.db import get_supabase, maybe_single_safe

from datetime import datetime, timezone, timedelta
from googleapiclient.discovery import build
from googleapiclient.discovery_cache import base
from core.lib.audit_logger import audit_log_sync
from core.services.google_service import get_google_creds, format_rfc3339

from core.services.outlook_service import get_outlook_calendar_events

from core.pulse.context import context_provider

supabase = get_supabase()


class MemoryCache(base.Cache):
    _cache = {}
    def get(self, url):
        return self._cache.get(url)
    def set(self, url, content):
        self._cache[url] = content


def get_calendar_context(target_date):
    """Merge Google + Outlook calendar events into a formatted string for prompts."""
    all_events = get_google_calendar_events(target_date) + get_outlook_calendar_events(target_date)
    if not all_events:
        return "None"
    all_events.sort(key=lambda x: x["time"])
    lines = []
    for e in all_events:
        try:
            t = e["time"][:16].replace("T", " ")
            src = "Google" if e["source"] == "google" else "Outlook"
            lines.append(f"- {t} - {e['title']} ({src})")
        except Exception:
            lines.append(f"- {e['title']}")
    return "\n".join(lines)

def check_conflict(start_iso, exclude_event_id=None):
    """Radar: Checks if a 30-minute window is already booked."""
    try:
        service = build('calendar', 'v3', credentials=get_google_creds(), cache=MemoryCache())
        rfc_time = format_rfc3339(start_iso)

        start_dt = datetime.fromisoformat(rfc_time.replace('Z', '+00:00'))
        end_dt = start_dt + timedelta(minutes=30)

        events_res = service.events().list(
            calendarId='primary',
            timeMin=rfc_time,
            timeMax=end_dt.isoformat(),
            singleEvents=True
        ).execute()

        events = events_res.get('items', [])
        if exclude_event_id:
            events = [e for e in events if e.get('id') != exclude_event_id]
        return events[0].get('summary') if events else None
    except Exception as e:
        audit_log_sync("pulse", "WARNING", f"⚠️ Conflict check failed: {e}")
        return None

def sync_completed_tasks_from_google(supabase_client, tasks_service):
    """Pulls completed status from Google Tasks and updates Supabase. Returns list of (title, proj_name) for completed tasks."""
    completed = []
    try:
        result = supabase_client.table('tasks')\
            .select('id, title, google_task_id, status')\
            .eq('status', 'todo')\
            .eq('is_current', True)\
            .not_.is_('google_task_id', None)\
            .is_('recurrence', None)\
            .execute()

        tasks_to_sync = result.data or []
        if not tasks_to_sync:
            print("📋 No Google Tasks to sync.")
            return completed

        print(f"🔍 Checking {len(tasks_to_sync)} tasks against Google Tasks...")

        synced_count = 0
        for task in tasks_to_sync:
            task_id = task['id']
            google_task_id = task['google_task_id']
            title = task.get('title', 'Untitled')

            try:
                google_task = tasks_service.tasks().get(
                    tasklist='@default',
                    task=google_task_id
                ).execute()

                if google_task.get('status') == 'completed':
                    try:
                        # Standard update relies on temporal lineage BEFORE UPDATE trigger
                        supabase_client.table('tasks').update({
                            'status': 'done',
                            'completed_at': datetime.now(timezone.utc).isoformat()
                        }).eq('id', task_id).execute()
                        # Invalidate tasks cache so Google-completed tasks disappear from next briefing
                        try:
                            context_provider.caches['tasks'].invalidate()
                            context_provider.caches['recent_tasks'].invalidate()
                        except Exception:
                            pass
                    except Exception as e:
                        audit_log_sync("pulse", "ERROR", f"Failed to mark task {task_id} as done: {e}")

                    # 🧠 Collect for outcome memory — caller will fire as background tasks
                    proj_name = None
                    proj_id = task.get('project_id')
                    if proj_id:
                        proj_lookup = maybe_single_safe(supabase_client.table('projects').select('name').eq('id', proj_id))
                        proj_name = proj_lookup.data['name'] if proj_lookup.data else None
                    completed.append((title, proj_name))

                    print(f"✅ Synced from Google: '{title}' (ID: {task_id})")
                    synced_count += 1

            except Exception as e:
                if 'notFound' in str(e):
                    audit_log_sync("pulse", "WARNING", f"⚠️ Google Task {google_task_id} not found, skipping.")
                else:
                    audit_log_sync("pulse", "WARNING", f"⚠️ Error checking Google Task {google_task_id}: {e}")

        print(f"📊 Google→Supabase Sync complete: {synced_count}/{len(tasks_to_sync)} tasks marked done.")

    except Exception as e:
        audit_log_sync("pulse", "ERROR", f"❌ sync_completed_tasks_from_google failed: {e}")

    return completed

def get_google_calendar_events(target_date):
    """Fetch calendar events from Google Calendar for a given date.
    Returns list of {time, title, source, id} or [] on failure."""
    try:
        service = build("calendar", "v3", credentials=get_google_creds(), cache=MemoryCache())
        start_dt = target_date.replace(hour=0, minute=0, second=0, microsecond=0)
        end_dt = start_dt + timedelta(days=1)
        rfc_start = format_rfc3339(start_dt.isoformat())
        rfc_end = format_rfc3339(end_dt.isoformat())
        events_res = service.events().list(
            calendarId="primary",
            timeMin=rfc_start,
            timeMax=rfc_end,
            singleEvents=True,
            orderBy="startTime",
        ).execute()
        events = []
        for e in events_res.get("items", []):
            start = e.get("start", {})
            dt = start.get("dateTime") or start.get("date", "")
            events.append({
                "time": dt,
                "title": e.get("summary", "Untitled"),
                "source": "google",
                "id": e.get("id"),
            })
        return events
    except Exception as e:
        audit_log_sync("pulse", "WARNING", f"Google calendar fetch failed: {e}")
        return []

def get_google_calendar_events_range(start_date, end_date):
    """Fetch calendar events from Google Calendar for a date range (for week/month views)."""
    try:
        service = build("calendar", "v3", credentials=get_google_creds(), cache=MemoryCache())
        start_dt = start_date.replace(hour=0, minute=0, second=0, microsecond=0)
        end_dt = end_date.replace(hour=23, minute=59, second=59)
        rfc_start = format_rfc3339(start_dt.isoformat())
        rfc_end = format_rfc3339(end_dt.isoformat())
        events_res = service.events().list(
            calendarId="primary",
            timeMin=rfc_start,
            timeMax=rfc_end,
            singleEvents=True,
            orderBy="startTime",
            maxResults=100,
        ).execute()
        events = []
        for e in events_res.get("items", []):
            start = e.get("start", {})
            dt = start.get("dateTime") or start.get("date", "")
            events.append({
                "time": dt,
                "title": e.get("summary", "Untitled"),
                "source": "google",
                "id": e.get("id"),
            })
        return events
    except Exception as e:
        audit_log_sync("pulse", "WARNING", f"Google calendar range fetch failed: {e}")
        return []
