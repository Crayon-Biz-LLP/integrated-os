import os
from datetime import datetime, timezone, timedelta
from supabase import create_client, Client
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.discovery_cache import base
from core.lib.audit_logger import audit_log_sync
from core.services.google_service import get_google_creds, format_rfc3339
from core.lib.temporal_lineage import create_versioned_task
from core.services.outlook_service import get_outlook_calendar_events, get_outlook_calendar_events_range

supabase: Client = create_client(
    os.getenv("SUPABASE_URL"),
    os.getenv("SUPABASE_SERVICE_ROLE_KEY")
)


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

def sync_to_calendar(title, start_iso, duration_mins=15, event_id=None):
    """Creates or UPDATES a block on the grid with dynamic duration."""
    service = build('calendar', 'v3', credentials=get_google_creds(), cache=MemoryCache())
    try:
        rfc_time = format_rfc3339(start_iso)
        start_dt = datetime.fromisoformat(rfc_time.replace('Z', '+00:00'))

        # 🕒 DYNAMIC DURATION (Defaulting to 15 now)
        end_dt = start_dt + timedelta(minutes=int(duration_mins))

        event_body = {
            'summary': f"🔥 CRITICAL: {title}",
            'description': 'Automated via Integrated-OS Sync',
            'start': {'dateTime': rfc_time, 'timeZone': 'Asia/Kolkata'},
            'end': {'dateTime': end_dt.isoformat(), 'timeZone': 'Asia/Kolkata'},
            'reminders': {'useDefault': True}
        }

        if event_id:
            res = service.events().patch(calendarId='primary', eventId=event_id, body=event_body).execute()
            print(f"🔄 SUCCESS: Calendar slot edited for {title}")
        else:
            res = service.events().insert(calendarId='primary', body=event_body).execute()
            print(f"📅 SUCCESS: New calendar block secured for {title}")

        return res.get('id')
    except Exception as e:
        # Fallback logic: If the event_id was invalid, try creating fresh
        if event_id:
            audit_log_sync("pulse", "WARNING", f"⚠️ Event ID {event_id} invalid. Attempting fresh creation...")
            return sync_to_calendar(title, start_iso, event_id=None)
        audit_log_sync("pulse", "ERROR", f"❌ CRITICAL: Calendar sync failed: {e}")
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
                    # Versioned insert for task completion
                    try:
                        current = supabase.table('tasks').select('*').eq('id', task_id).execute()
                        if current.data:
                            old_task = current.data[0]
                            new_payload = {
                                **{k: v for k, v in old_task.items() if k not in ['id', 'created_at', 'version', 'is_current', 'supersedes_id']},
                                'status': 'done',
                                'completed_at': datetime.now(timezone.utc).isoformat()
                            }
                            create_versioned_task(
                                title=new_payload.get('title'),
                                project_id=new_payload.get('project_id'),
                                old_task_id=task_id,
                                **new_payload
                            )
                    except Exception as ve:
                        # Fallback to direct update
                        supabase.table('tasks').update({
                            'status': 'done',
                            'completed_at': datetime.now(timezone.utc).isoformat()
                        }).eq('id', task_id).execute()

                    # 🧠 Collect for outcome memory — caller will fire as background tasks
                    proj_name = None
                    proj_id = task.get('project_id')
                    if proj_id:
                        proj_lookup = supabase_client.table('projects').select('name').eq('id', proj_id).maybe_single().execute()
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
