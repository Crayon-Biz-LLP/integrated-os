import os
import httpx
from datetime import timedelta
from core.lib.audit_logger import audit_log_sync


def get_outlook_calendar_events(target_date):
    try:
        from core.skills.outlook_token_helper import refresh_outlook_token
        token_data = refresh_outlook_token(write_back=False)
        access_token = token_data["access_token"]

        start_dt = target_date.replace(hour=0, minute=0, second=0, microsecond=0)
        end_dt = start_dt + timedelta(days=1)

        headers = {
            "Authorization": f"Bearer {access_token}",
            "Prefer": 'outlook.timezone="Asia/Kolkata"',
        }

        params = {
            "startDateTime": start_dt.isoformat(),
            "endDateTime": end_dt.isoformat(),
            "$orderby": "start/dateTime",
            "$select": "subject,start,end,id",
            "$top": 50,
        }

        url = "https://graph.microsoft.com/v1.0/me/calendarview"
        events = []
        while url:
            if url.startswith("https://"):
                resp = httpx.get(url, headers=headers, params=params, timeout=30)
            else:
                resp = httpx.get(url, headers=headers, timeout=30)
            resp.raise_for_status()
            data = resp.json()
            for item in data.get("value", []):
                events.append({
                    "time": item["start"]["dateTime"],
                    "title": item.get("subject", "Untitled"),
                    "source": "outlook",
                    "id": item.get("id"),
                })
            url = data.get("@odata.nextLink")
            params = None
        return events
    except Exception as e:
        audit_log_sync("outlook_service", "WARNING", f"Outlook calendar fetch failed: {e}")
        return []


def get_outlook_calendar_events_range(start_date, end_date):
    try:
        from core.skills.outlook_token_helper import refresh_outlook_token
        token_data = refresh_outlook_token(write_back=False)
        access_token = token_data["access_token"]

        start_dt = start_date.replace(hour=0, minute=0, second=0, microsecond=0)
        end_dt = end_date.replace(hour=23, minute=59, second=59)

        headers = {
            "Authorization": f"Bearer {access_token}",
            "Prefer": 'outlook.timezone="Asia/Kolkata"',
        }

        params = {
            "startDateTime": start_dt.isoformat(),
            "endDateTime": end_dt.isoformat(),
            "$orderby": "start/dateTime",
            "$select": "subject,start,end,id",
            "$top": 100,
        }

        url = "https://graph.microsoft.com/v1.0/me/calendarview"
        events = []
        while url:
            if url.startswith("https://"):
                resp = httpx.get(url, headers=headers, params=params, timeout=30)
            else:
                resp = httpx.get(url, headers=headers, timeout=30)
            resp.raise_for_status()
            data = resp.json()
            for item in data.get("value", []):
                events.append({
                    "time": item["start"]["dateTime"],
                    "title": item.get("subject", "Untitled"),
                    "source": "outlook",
                    "id": item.get("id"),
                })
            url = data.get("@odata.nextLink")
            params = None
        return events
    except Exception as e:
        audit_log_sync("outlook_service", "WARNING", f"Outlook calendar range fetch failed: {e}")
        return []
