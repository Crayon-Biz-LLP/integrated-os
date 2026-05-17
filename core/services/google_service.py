import os
from datetime import datetime, timedelta
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


def get_google_creds():
    return Credentials(
        None,
        refresh_token=os.getenv("GOOGLE_REFRESH_TOKEN"),
        client_id=os.getenv("GOOGLE_CLIENT_ID"),
        client_secret=os.getenv("GOOGLE_CLIENT_SECRET"),
        token_uri="https://oauth2.googleapis.com/token"
    )


def get_tasks_service():
    return build('tasks', 'v1', credentials=get_google_creds(), cache=_MemoryCache())


def format_rfc3339(date_str):
    if not date_str:
        return None
    clean = str(date_str).replace(' ', 'T')
    if 'T' not in clean:
        clean = f"{clean}T09:00:00+05:30"
    if not (clean.endswith('Z') or '+' in clean[-6:]):
        clean += "+05:30"
    return clean


def sync_to_calendar(title, start_iso, duration_mins=15, event_id=None):
    service = build('calendar', 'v3', credentials=get_google_creds(), cache=_MemoryCache())
    try:
        rfc_time = format_rfc3339(start_iso)
        start_dt = datetime.fromisoformat(rfc_time.replace('Z', '+00:00'))
        end_dt = start_dt + timedelta(minutes=int(duration_mins))
        event_body = {
            'summary': title,
            'start': {'dateTime': rfc_time, 'timeZone': 'Asia/Kolkata'},
            'end': {'dateTime': end_dt.isoformat(), 'timeZone': 'Asia/Kolkata'},
            'reminders': {'useDefault': True}
        }
        if event_id:
            res = service.events().patch(calendarId='primary', eventId=event_id, body=event_body).execute()
        else:
            res = service.events().insert(calendarId='primary', body=event_body).execute()
        return res.get('id')
    except Exception as e:
        if event_id:
            return sync_to_calendar(title, start_iso, event_id=None)
        audit_log_sync("google_service", "ERROR", f"Calendar sync failed: {e}")
        return None


def delete_calendar_event(event_id):
    if not event_id:
        return
    service = build('calendar', 'v3', credentials=get_google_creds(), cache=_MemoryCache())
    try:
        service.events().delete(calendarId='primary', eventId=event_id).execute()
    except Exception:
        pass


def sync_to_google(service, title=None, due_at=None, task_id=None, status='todo', explicit_time=False):
    if task_id and status in ('done', 'cancelled'):
        try:
            service.tasks().patch(tasklist='@default', task=task_id, body={'status': 'completed'}).execute()
            return task_id
        except Exception:
            return None

    rfc_date = format_rfc3339(due_at)
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
        service = build("calendar", "v3", credentials=get_google_creds(), cache=_MemoryCache())
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
        service = build("calendar", "v3", credentials=get_google_creds(), cache=_MemoryCache())
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
        service = build('calendar', 'v3', credentials=get_google_creds(), cache=_MemoryCache())
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
