import re
from datetime import datetime, timezone, timedelta, tzinfo
from typing import Optional
from zoneinfo import ZoneInfo

# ── Shared IST timezone (UTC+05:30) — pre-M2 default / fallback ──
_IST = timezone(timedelta(hours=5, minutes=30))


def now_ist() -> datetime:
    """Return current datetime in Indian Standard Time (UTC+05:30)."""
    return datetime.now(_IST)


# Re-export the timezone object for callers that need it
IST_TIMEZONE = _IST


_tz_cache: dict[str, tzinfo] = {}


def is_valid_timezone(name: str | None) -> bool:
    """True if `name` is a real IANA timezone name (best-effort).

    The shared gate for every path that writes user_settings.timezone — the
    onboarding device string and the admin seed paths — so a garbage value
    can never poison the row.
    """
    if not name or not isinstance(name, str):
        return False
    try:
        ZoneInfo(name.strip())
        return True
    except Exception:
        return False


def get_user_timezone(user_id: str | None = None) -> tzinfo:
    """Resolve the user's IANA timezone → tzinfo (M2 de-personalization).

    Resolution order: user_settings.timezone → USER_TIMEZONE env → IST.
    Invalid/unknown names fall back to IST (never crash on a bad setting).
    """
    name: str | None = None
    try:
        from core.services.user_settings import resolve_timezone
        name = resolve_timezone(user_id)
    except Exception:
        name = None
    if not name:
        name = "Asia/Kolkata"
    if name in _tz_cache:
        return _tz_cache[name]
    try:
        tz = ZoneInfo(name)
    except Exception:
        tz = _IST
    _tz_cache[name] = tz
    return tz


def now_for_user(user_id: str | None = None) -> datetime:
    """Current datetime in the user's timezone (M2)."""
    return datetime.now(get_user_timezone(user_id))


def tz_label(user_id: str | None = None) -> str:
    """The tenant's timezone abbreviation (e.g. 'IST', 'JST', 'EDT') — computed
    from the resolved tzinfo, never a constant (M9.4). Fallback 'IST' on any
    error (invalid name, empty tzname) — never a crash.
    """
    try:
        tz = get_user_timezone(user_id)
        now = datetime.now(tz)
        label = now.strftime("%Z") or tz.tzname(now) or ""
        return label or "IST"
    except Exception:
        return "IST"


def tz_offset_str(user_id: str | None = None) -> str:
    """The tenant's UTC offset (e.g. '+05:30', '+09:00', '-04:00') — computed
    from the resolved tzinfo, never a constant (M9.4). Fallback '+05:30' on
    any error — never a crash.
    """
    try:
        tz = get_user_timezone(user_id)
        off = tz.utcoffset(datetime.now(tz))
        if off is None:
            return "+05:30"
        total = int(off.total_seconds())
        sign = "+" if total >= 0 else "-"
        total = abs(total)
        return f"{sign}{total // 3600:02d}:{total % 3600 // 60:02d}"
    except Exception:
        return "+05:30"


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


def resolve_time_delta(delta: dict, reference: Optional[datetime] = None) -> datetime:
    """Compute an absolute datetime from a structured time delta.

    The LLM extracts `{amount, unit, direction}` from the phrasing (it reads
    language well); this function does the arithmetic (LLMs are unreliable at
    calendar math — the Aug 12 "defer by 7 days" failure). Always returns a
    timezone-aware datetime in the tenant's zone, anchored to `reference`
    (default: now).

    Args:
        delta: {"amount": int > 0, "unit": "days"|"weeks"|"hours",
                "direction": "later"|"earlier"}
        reference: anchor datetime (default: now in the tenant's timezone)

    Returns:
        reference ± amount×unit, timezone-aware.
    """
    ref = reference or datetime.now(get_user_timezone())
    try:
        amount = int(delta.get("amount", 0) or 0)
    except (TypeError, ValueError):
        amount = 0
    if amount <= 0:
        amount = 1
    unit = (delta.get("unit") or "days").lower()
    direction = (delta.get("direction") or "later").lower()
    if unit == "weeks":
        step = timedelta(weeks=amount)
    elif unit == "hours":
        step = timedelta(hours=amount)
    else:
        step = timedelta(days=amount)
    return ref - step if direction == "earlier" else ref + step


def resolve_relative_dates(text: str, reference_date: datetime) -> str:
    """Resolve relative date words in text against a reference timestamp.

    Replaces ambiguous relative words with absolute dates so the LLM can't
    misinterpret "tomorrow" in a 30-day-old message as "tomorrow from today."

    Handles:
    - "tomorrow" -> "on {reference_date + 1 day}"
    - "today" / "tonight" -> "on {reference_date}"
    - "this Monday/Tuesday..." -> next occurrence from reference_date
    - "next Monday/Tuesday..." -> next occurrence in the following week
    - "in/by N days/weeks", "next week", "in a week", "a week from now"
      -> "on {reference_date + delta}"

    Returns the text with relative words replaced by absolute dates.
    """
    text_lower = text.lower()
    result = text

    # "day after tomorrow" or "day after" -> "on June 22, 2026"
    day_after_pattern = r'\bday\s+after(?:\s+tomorrow)?\b'
    if re.search(day_after_pattern, text_lower):
        day_after = reference_date + timedelta(days=2)
        date_str = day_after.strftime('%B %d, %Y')
        result = re.sub(day_after_pattern, f'on {date_str}', result, flags=re.I)

    # "tomorrow" -> "on June 21, 2026"
    if re.search(r'\btomorrow\b', text_lower):
        tomorrow = reference_date + timedelta(days=1)
        date_str = tomorrow.strftime('%B %d, %Y')
        result = re.sub(r'\btomorrow\b', f'on {date_str}', result, flags=re.I)

    # "today" / "tonight" -> "on June 20, 2026"
    for word in ['today', 'tonight']:
        if re.search(rf'\b{word}\b', text_lower):
            date_str = reference_date.strftime('%B %d, %Y')
            result = re.sub(rf'\b{word}\b', f'on {date_str}', result, flags=re.I)

    # "next Monday/Tuesday/..." -> next occurrence in the following week
    for i, day in enumerate(_DAY_NAMES):
        pattern = rf'\bnext\s+{day}\b'
        if re.search(pattern, text_lower):
            days_ahead = i - reference_date.weekday()
            if days_ahead <= 0:
                days_ahead += 7
            target = reference_date + timedelta(days=days_ahead + 7)
            date_str = target.strftime('%B %d, %Y')
            result = re.sub(pattern, f'on {date_str}', result, flags=re.I)

    # "this Monday/Tuesday/..." or bare "Monday/Tuesday/..." -> next occurrence from reference_date
    for i, day in enumerate(_DAY_NAMES):
        pattern = rf'\b(this\s+)?{day}\b'
        if re.search(pattern, result, flags=re.I): # search in result so we don't match already replaced "next Tuesday"
            days_ahead = i - reference_date.weekday()
            if days_ahead <= 0:
                days_ahead += 7
            target = reference_date + timedelta(days=days_ahead)
            date_str = target.strftime('%B %d, %Y')
            result = re.sub(pattern, f'on {date_str}', result, flags=re.I)

    # "in/by N days/weeks" -> "on {reference_date + N units}"
    for m in re.finditer(r'\b(?:in|by)\s+(\d+)\s+(day|week)s?\b', text_lower):
        amount = int(m.group(1))
        unit = m.group(2)
        target = reference_date + (timedelta(days=amount) if unit == 'day' else timedelta(weeks=amount))
        date_str = target.strftime('%B %d, %Y')
        result = re.sub(re.escape(m.group(0)), f'on {date_str}', result, flags=re.I)

    # "next week" / "in a week" / "a week from now" -> "on {reference_date + 1 week}"
    for phrase in ('next week', 'in a week', 'a week from now'):
        if phrase in text_lower:
            target = reference_date + timedelta(weeks=1)
            date_str = target.strftime('%B %d, %Y')
            result = re.sub(re.escape(phrase), f'on {date_str}', result, flags=re.I)

    return result


_DELTA_UNITS = {
    "day": "days", "days": "days",
    "week": "weeks", "weeks": "weeks",
    "hour": "hours", "hours": "hours",
}
_WORD_NUMBERS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5}


def extract_time_delta(text: str) -> Optional[dict]:
    """Extract a structured {amount, unit, direction} delta from relative
    time phrasing, or None when the text carries no computable delta.

    Invariant #2 backstop: the LLM reads the phrasing into a delta (or an
    absolute date); this is the deterministic fallback that re-reads the raw
    text when the LLM drops the time on a time-bearing action — so "defer by
    7 days" can never be silently acked and is never asked about (the Aug 12
    failure class). Code does the arithmetic; the LLM never does.

    Handles: "by/in N days|weeks|hours", "N days/weeks from now",
    "push it back a week", "give me two more weeks",
    "move the sync up 2 days" (earlier).
    """
    if not text or not isinstance(text, str):
        return None
    t = text.lower()
    direction = "later"

    # "by/in N days|weeks|hours" — the Aug 12 phrasing
    m = re.search(r'\b(?:in|by)\s+(\d+)\s+(day|week|hour)s?\b', t)
    # "N days/weeks from now"
    if not m:
        m = re.search(r'\b(\d+)\s+(day|week|hour)s?\s+from\s+now\b', t)
    if m:
        amount = int(m.group(1))
        if amount <= 0:
            return None
        # earlier markers before the number ("move it up 2 days")
        prefix = t[max(0, m.start() - 24):m.start()]
        if re.search(r'\b(?:up|earlier|sooner)\b', prefix):
            direction = "earlier"
        return {"amount": amount, "unit": _DELTA_UNITS[m.group(2)], "direction": direction}

    # "push it back a week" / "give me a week" / "in a week" (no digits)
    m = re.search(r'\b(?:a|one)\s+(day|week|hour)s?\b', t)
    if m:
        return {"amount": 1, "unit": _DELTA_UNITS[m.group(1)], "direction": direction}

    # "give me two more weeks" / "three more days"
    m = re.search(r'\b(two|three|four|five)\s+more\s+(day|week|hour)s?\b', t)
    if m:
        return {"amount": _WORD_NUMBERS[m.group(1)], "unit": _DELTA_UNITS[m.group(2)], "direction": direction}

    # "move the sync up 2 days" / "back 2 days" (marker-led, no in/by)
    m = re.search(r'\b(up|earlier|sooner|back|forward|ahead)\s+(\d+)\s+(day|week|hour)s?\b', t)
    if m:
        direction = "earlier" if m.group(1) in ("up", "earlier", "sooner") else "later"
        amount = int(m.group(2))
        if amount <= 0:
            return None
        return {"amount": amount, "unit": _DELTA_UNITS[m.group(3)], "direction": direction}

    return None


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

    # "day after tomorrow" / "day after" — expires at end of 2 days out
    if re.search(r'\bday\s+after(?:\s+tomorrow)?\b', text_lower):
        day_after = created_at + timedelta(days=2)
        if parsed_time:
            return day_after.replace(hour=parsed_time.hour, minute=parsed_time.minute, second=0, microsecond=0)
        return _end_of_day(day_after)

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
