import httpx
from datetime import timedelta
from core.lib.audit_logger import audit_log_sync


def _outlook_timezone_name() -> str:
    """The tenant's IANA tz name for the Outlook Prefer header (M9.4).

    Resolution: user_settings.timezone → USER_TIMEZONE env → Asia/Kolkata.
    The mailbox event times are interpreted in this zone. A stored value
    that isn't a real IANA name (invalid-but-non-empty) falls back to
    Asia/Kolkata so the API never 400s — never a crash.
    """
    from zoneinfo import ZoneInfo
    try:
        from core.services.user_settings import resolve_timezone
        name = resolve_timezone()
        if name:
            ZoneInfo(name)  # sanity: reject invalid-but-non-empty names
            return name
    except Exception:
        pass
    return "Asia/Kolkata"


def get_outlook_calendar_events(target_date):
    try:
        from core.skills.outlook_token_helper import refresh_outlook_token
        token_data = refresh_outlook_token(write_back=False)
        if not token_data:
            # Tenant has no Outlook token — skip cleanly (never fall back to
            # another tenant's env credential; no warning spam for tenants
            # that simply don't use Outlook).
            return []
        access_token = token_data["access_token"]

        start_dt = target_date.replace(hour=0, minute=0, second=0, microsecond=0)
        end_dt = start_dt + timedelta(days=1)

        headers = {
            "Authorization": f"Bearer {access_token}",
            "Prefer": f'outlook.timezone="{_outlook_timezone_name()}"',
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
        if not token_data:
            # Tenant has no Outlook token — skip cleanly (no cross-tenant
            # env fallback, no warning spam).
            return []
        access_token = token_data["access_token"]

        start_dt = start_date.replace(hour=0, minute=0, second=0, microsecond=0)
        end_dt = end_date.replace(hour=23, minute=59, second=59)

        headers = {
            "Authorization": f"Bearer {access_token}",
            "Prefer": f'outlook.timezone="{_outlook_timezone_name()}"',
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
