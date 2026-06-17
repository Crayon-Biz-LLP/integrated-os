import re
from datetime import datetime, timezone, timedelta
from typing import Optional


def age_tag(created_at_str: str | None) -> str:
    """Returns a bracketed age string for an ISO timestamp, or empty string.
    
    Handles:
    - Timezone-aware UTC/offset timestamps (e.g. "2026-06-16T22:15:28.476718+00:00")
    - Timezone-naive timestamps (assumed UTC)
    - None or empty input
    
    Returns: "[Today]", "[Yesterday]", "[N days ago]", or ""
    """
    if not created_at_str:
        return ""

    try:
        dt = datetime.fromisoformat(created_at_str)
    except (ValueError, TypeError):
        return ""

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)

    now = datetime.now(timezone.utc)
    delta = now.date() - dt.date()
    days = delta.days

    if days < 0:
        return ""
    if days == 0:
        return "[Today]"
    if days == 1:
        return "[Yesterday]"
    return f"[{days} days ago]"


_DAY_NAMES = ['monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday']

_TIME_PATTERN = re.compile(r'\b(?:at\s+)?(\d{1,2}):(\d{2})\s*(AM|PM|am|pm)\b')


def _end_of_day(dt: datetime) -> datetime:
    """Return end-of-day for the given datetime."""
    return dt.replace(hour=23, minute=59, second=59, microsecond=0)


def _parse_timestamp(ts_str: str) -> Optional[datetime]:
    """Parse an ISO 8601 timestamp string, assumed UTC if naive."""
    if not ts_str:
        return None
    try:
        dt = datetime.fromisoformat(ts_str)
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def compute_expires_at(content: str, created_at_iso: str) -> Optional[str]:
    """Convenience: parse timestamp, resolve expiry, return ISO string or None."""
    dt = _parse_timestamp(created_at_iso)
    if dt is None:
        dt = datetime.now(timezone.utc)
    expiry = resolve_expiry(content, dt)
    return expiry.isoformat() if expiry else None


def resolve_expiry(content: str, created_at: datetime) -> Optional[datetime]:
    """Detect relative time phrases in content and resolve them against created_at.
    
    Returns expires_at datetime, or None if no time-sensitive content detected.
    """
    text_lower = content.lower()

    # "today" — expires at end of created_at's day
    has_today = bool(re.search(r'\btoday\b', text_lower))

    # Extract time reference if present (e.g. "at 8:15 PM")
    time_match = _TIME_PATTERN.search(text_lower)
    parsed_time = None
    if time_match:
        hour = int(time_match.group(1))
        minute = int(time_match.group(2))
        ampm = time_match.group(3).lower()
        if ampm == 'pm' and hour < 12:
            hour += 12
        elif ampm == 'am' and hour == 12:
            hour = 0
        parsed_time = created_at.replace(hour=hour, minute=minute, second=0, microsecond=0)

    if has_today:
        if parsed_time and parsed_time > created_at:
            return parsed_time
        return _end_of_day(created_at)

    # "tomorrow" — expires at end of next day
    if re.search(r'\btomorrow\b', text_lower):
        tomorrow = created_at + timedelta(days=1)
        if parsed_time:
            return tomorrow.replace(hour=parsed_time.hour, minute=parsed_time.minute, second=0, microsecond=0)
        return _end_of_day(tomorrow)

    # "this Sunday/Monday..." — expires at end of that day
    for i, day in enumerate(_DAY_NAMES):
        if re.search(rf'\bthis\s+{day}\b', text_lower):
            days_ahead = i - created_at.weekday()
            if days_ahead <= 0:
                days_ahead += 7
            target = created_at + timedelta(days=days_ahead)
            if parsed_time:
                return target.replace(hour=parsed_time.hour, minute=parsed_time.minute, second=0, microsecond=0)
            return _end_of_day(target)

    # "next Monday..." — expires at end of that day, two weeks out
    for i, day in enumerate(_DAY_NAMES):
        if re.search(rf'\bnext\s+{day}\b', text_lower):
            days_ahead = i - created_at.weekday()
            if days_ahead <= 0:
                days_ahead += 7
            target = created_at + timedelta(days=days_ahead + 7)
            if parsed_time:
                return target.replace(hour=parsed_time.hour, minute=parsed_time.minute, second=0, microsecond=0)
            return _end_of_day(target)

    # Standalone time reference (no date word) — expires today at that time
    if parsed_time and parsed_time > created_at:
        return parsed_time

    return None
