import os
import functools
from datetime import datetime, timedelta, timezone
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.discovery_cache import base
from core.lib.audit_logger import audit_log_sync


class _MemoryCache(base.Cache):
    _cache = {}

    def get(self, url):
        return self._cache.get(url)

    def set(self, url, content):
        self._cache[url] = content


@functools.lru_cache(maxsize=1)
def get_google_creds():
    return Credentials(
        None,
        refresh_token=os.getenv("GOOGLE_REFRESH_TOKEN"),
        client_id=os.getenv("GOOGLE_CLIENT_ID"),
        client_secret=os.getenv("GOOGLE_CLIENT_SECRET"),
        token_uri="https://oauth2.googleapis.com/token"
    )

@functools.lru_cache(maxsize=4)
def get_cached_service(service_name, version):
    return build(service_name, version, credentials=get_google_creds(), cache=_MemoryCache())


def get_tasks_service():
    return get_cached_service('tasks', 'v1')


def format_rfc3339(date_str):
    if not date_str:
        return None
    clean = str(date_str).replace(' ', 'T')
    if 'T' not in clean:
        clean = f"{clean}T09:00:00+05:30"
    if not (clean.endswith('Z') or '+' in clean[-6:]):
        clean += "+05:30"
    return clean


def sync_to_calendar(title, start_iso, duration_mins=15, event_id=None, priority='important', recurrence=None):
    service = get_cached_service('calendar', 'v3')
    try:
        rfc_time = format_rfc3339(start_iso)
        start_dt = datetime.fromisoformat(rfc_time.replace('Z', '+00:00'))
        end_dt = start_dt + timedelta(minutes=int(duration_mins))
        
        priority_lower = str(priority).lower() if priority else "important"
        if priority_lower == "urgent":
            prefix = "🔥 CRITICAL: "
        elif priority_lower == "low":
            prefix = "☕ INFO: "
        else:
            prefix = "⚡ ACTION: "
            
        clean_title = title
        for p in ["🔥 CRITICAL: ", "⚡ ACTION: ", "☕ INFO: "]:
            if clean_title.startswith(p):
                clean_title = clean_title[len(p):]
                
        formatted_title = f"{prefix}{clean_title}"

        event_body = {
            'summary': formatted_title,
            'description': 'Rhodey created this for you.',
            'start': {'dateTime': rfc_time, 'timeZone': 'Asia/Kolkata'},
            'end': {'dateTime': end_dt.isoformat(), 'timeZone': 'Asia/Kolkata'},
            'reminders': {
                'useDefault': False,
                'overrides': [
                    {'method': 'popup', 'minutes': 60},
                    {'method': 'popup', 'minutes': 15}
                ]
            }
        }
        
        if recurrence:
            event_body['recurrence'] = [recurrence]

        if event_id:
            res = service.events().patch(calendarId='primary', eventId=event_id, body=event_body).execute()
            print(f"Calendar block edited: {formatted_title}")
        else:
            res = service.events().insert(calendarId='primary', body=event_body).execute()
            print(f"Calendar block secured: {formatted_title}")

        return res.get('id')
    except Exception as e:
        if event_id:
            audit_log_sync("google_service", "WARNING", f"Event ID {event_id} invalid, attempting creation...")
            return sync_to_calendar(title, start_iso, duration_mins, event_id=None, priority=priority, recurrence=recurrence)
        audit_log_sync("google_service", "ERROR", f"Calendar sync failed: {e}")
        return None


def delete_calendar_event(event_id):
    if not event_id:
        return
    service = get_cached_service('calendar', 'v3')
    try:
        service.events().delete(calendarId='primary', eventId=event_id).execute()
    except Exception as e:
        audit_log_sync("google_service", "WARNING", f"Calendar event {event_id} delete failed (likely already gone): {e}")


def delete_calendar_instance(recurring_event_id, instance_id):
    """Delete a single instance of a recurring Google Calendar event.
    recurring_event_id: the ID of the recurring series.
    instance_id: the ID of the specific instance to delete."""
    if not recurring_event_id or not instance_id:
        return
    service = get_cached_service('calendar', 'v3')
    try:
        service.events().delete(calendarId='primary', eventId=instance_id).execute()
        audit_log_sync("google_service", "INFO", f"Deleted calendar instance {instance_id} of {recurring_event_id}")
    except Exception as e:
        audit_log_sync("google_service", "WARNING", f"Calendar instance {instance_id} delete failed: {e}")


def sync_to_google(service, title=None, due_at=None, task_id=None, status='todo', explicit_time=False):
    if task_id and status in ('done', 'cancelled'):
        try:
            service.tasks().patch(tasklist='@default', task=task_id, body={'status': 'completed'}).execute()
            return task_id
        except Exception:
            return None

    rfc_date = format_rfc3339(due_at)

    # Time-Visibility Title Hack — prefix with 🕒 HH:MM when explicit time is set
    if explicit_time and rfc_date and 'T' in str(rfc_date):
        try:
            dt = datetime.fromisoformat(rfc_date.replace('Z', '+00:00'))
            ist_dt = dt.astimezone(timezone(timedelta(hours=5, minutes=30)))
            time_str = ist_dt.strftime('%H:%M')
            if title and f"{time_str}" not in title:
                title = f"🕒 {time_str} | {title}"
        except Exception:
            pass

    try:
        body = {'title': title}
        if rfc_date:
            body['due'] = rfc_date
        if task_id:
            res = service.tasks().patch(tasklist='@default', task=task_id, body=body).execute()
        else:
            res = service.tasks().insert(tasklist='@default', body=body).execute()
        return res.get('id')
    except Exception as e:
        audit_log_sync("google_service", "WARNING", f"Google Tasks sync failed: {e}")
        return None


def get_google_calendar_events(target_date):
    try:
        service = get_cached_service("calendar", "v3")
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
        audit_log_sync("google_service", "WARNING", f"Google calendar fetch failed: {e}")
        return []


def get_google_calendar_events_range(start_date, end_date):
    try:
        service = get_cached_service("calendar", "v3")
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
        audit_log_sync("google_service", "WARNING", f"Google calendar range fetch failed: {e}")
        return []


def check_conflict(start_iso):
    try:
        service = get_cached_service('calendar', 'v3')
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
        return events[0].get('summary') if events else None
    except Exception as e:
        audit_log_sync("google_service", "WARNING", f"Conflict check failed: {e}")
        return None


def get_calendar_context(target_date):
    events = get_google_calendar_events(target_date)
    if not events:
        return "None"
    events.sort(key=lambda x: x["time"])
    lines = []
    for e in events:
        try:
            t = e["time"][:16].replace("T", " ")
            lines.append(f"- {t} - {e['title']} (Google)")
        except Exception:
            lines.append(f"- {e['title']}")
    return "\n".join(lines)
