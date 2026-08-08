"""briefing_schedule.py — M9.7 per-tenant briefing schedule (presets + gate).

The pulse heartbeat (GHA `*/30 * * * *`) wakes the engine every 30 minutes;
this module decides, per tenant, whether THIS heartbeat is one of THEIR
briefing slots. The GHA cron can never be per-tenant (it is a static UTC
list), so the schedule lives in the DB and the code gates per user.

Storage: `core_config` key `briefing_schedule` (owner_id-scoped, same pattern
as `briefing_sections`). Content is the RESOLVED schedule JSON — editable
directly in the table, never derived at read time:

    {
      "preset": "balanced",
      "weekday": ["08:00", "13:00", "19:00"],
      "weekend": ["09:00", "17:00"],
      "window_minutes": 15
    }

Presets are templates used at SEED time (Danny → classic, new tenants →
balanced). An admin can edit the row's times afterward and the gate follows.

Resolution order (mirrors user_settings resolvers):
  1. core_config `briefing_schedule` row (owner-scoped) — validated, used
  2. DEFAULT_PRESET template (balanced) — never another tenant's row,
     never a crash, never a briefing at the wrong time

The gate function `briefing_due_now()` is PURE (no DB/IO) so it is trivially
testable — scripts/verify_m9_7_schedule.py exercises it for Danny's exact
slots, the balanced default, a Canada timezone, determinism, fail-closed
behaviour, and the single-fire-per-slot guarantee.
"""

from __future__ import annotations

import json
import time
from datetime import datetime

from core.services.db import get_supabase


# The facade and the raw client both work here: inside a tenant_scope the
# facade auto-scopes by context; outside one (admin scripts, tests) we pass
# an explicit owner_id filter to the raw client — exactly the pattern
# user_settings.py uses for user_settings reads. Core_config is keyed
# (owner_id, key), so an explicit owner filter is ALWAYS applied either way.


# ── Presets (templates — seed-time only; the DB row is the source of truth) ─

PRESETS: dict[str, dict] = {
    # Danny's exact current schedule (pulse.yml before M9.7) — seeded so his
    # behaviour is byte-identical. Weekday: 07:30/11:30/14:30/17:30 + 20:00
    # evening wind-down. Weekend: 08:00/15:00.
    "classic": {
        "preset": "classic",
        "weekday": ["07:30", "11:30", "14:30", "17:30", "20:00"],
        "weekend": ["08:00", "15:00"],
        "window_minutes": 15,
    },
    # Default for new tenants. Weekday 08:00/13:00/19:00, weekend 09:00/17:00.
    "balanced": {
        "preset": "balanced",
        "weekday": ["08:00", "13:00", "19:00"],
        "weekend": ["09:00", "17:00"],
        "window_minutes": 15,
    },
    # Light: bookend the day. Weekday 08:00/20:00, weekend 10:00.
    "bookends": {
        "preset": "bookends",
        "weekday": ["08:00", "20:00"],
        "weekend": ["10:00"],
        "window_minutes": 15,
    },
    # Power user: five touchpoints across the workday.
    "through_the_day": {
        "preset": "through_the_day",
        "weekday": ["08:00", "11:00", "14:00", "17:00", "20:00"],
        "weekend": ["08:00", "11:00", "17:00"],
        "window_minutes": 15,
    },
}

DEFAULT_PRESET = "balanced"

# Schedule rows are small and change rarely; a 60s per-user TTL cache keeps
# the 30-minute heartbeat cheap (one DB read per user per heartbeat at most).
_schedule_cache: dict[str, tuple[float, dict]] = {}
_CACHE_TTL_S = 60


def clear_cache(user_id: str | None = None) -> None:
    """Drop cached schedules (tests / admin edits)."""
    if user_id is None:
        _schedule_cache.clear()
    else:
        _schedule_cache.pop(user_id, None)


def _validate_schedule(raw: dict) -> dict | None:
    """Coerce/validate a raw schedule dict → clean schedule, or None if bad."""
    if not isinstance(raw, dict):
        return None
    weekday = raw.get("weekday")
    weekend = raw.get("weekend")
    if not isinstance(weekday, list) or not isinstance(weekend, list):
        return None
    def _ok_slot(slot) -> str | None:
        if not isinstance(slot, str) or len(slot) != 5 or slot[2] != ":":
            return None
        hh, mm = slot[:2], slot[3:]
        if not (hh.isdigit() and mm.isdigit()):
            return None
        # Range-check so a table edit like "99:99" is a dead slot, not a
        # silently-never-firing one.
        if not (0 <= int(hh) <= 23 and 0 <= int(mm) <= 59):
            return None
        return slot

    clean = {"preset": str(raw.get("preset") or ""), "weekday": [], "weekend": []}
    for slot in weekday:
        ok = _ok_slot(slot)
        if ok is None:
            return None
        clean["weekday"].append(ok)
    for slot in weekend:
        ok = _ok_slot(slot)
        if ok is None:
            return None
        clean["weekend"].append(ok)
    try:
        window = int(raw.get("window_minutes", 15) or 15)
    except (TypeError, ValueError):
        window = 15
    # Cap at 15: the 30-min heartbeat must never have TWO heartbeats inside
    # one slot's window (|30| > 15 always) — that is what guarantees each
    # slot fires exactly once. A larger window would need a matching guard.
    clean["window_minutes"] = max(1, min(window, 15))
    return clean


def resolve_briefing_schedule(user_id: str | None = None) -> dict:
    """The tenant's resolved briefing schedule (fail-closed → balanced).

    Reads the owner-scoped `core_config.briefing_schedule` row. Any failure
    (row missing, unparseable JSON, invalid shape, DB error) falls back to
    the DEFAULT_PRESET template — never a crash, never another tenant's row.
    """
    cached = _schedule_cache.get(user_id) if user_id else None
    if cached and (time.time() - cached[0]) < _CACHE_TTL_S:
        return cached[1]

    schedule: dict = json.loads(json.dumps(PRESETS[DEFAULT_PRESET]))
    if user_id:
        try:
            q = (
                get_supabase()
                .table("core_config")
                .select("content")
                .eq("owner_id", user_id)
                .eq("key", "briefing_schedule")
                .limit(1)
                .maybe_single()
            )
            # Inside a tenant_scope the facade re-scopes the query; the
            # explicit owner_id filter is the floor either way.
            res = q.execute()
            content = (res.data or {}).get("content") if res.data else None
            if content:
                parsed = json.loads(content) if isinstance(content, str) else content
                validated = _validate_schedule(parsed)
                if validated is not None:
                    schedule = validated
        except Exception:
            pass  # fail-closed → DEFAULT_PRESET

    if user_id:
        _schedule_cache[user_id] = (time.time(), schedule)
    return schedule


def schedule_for_preset(preset_id: str | None) -> dict:
    """The preset template (for seeding/onboarding), or the default."""
    if preset_id and preset_id in PRESETS:
        return json.loads(json.dumps(PRESETS[preset_id]))
    return json.loads(json.dumps(PRESETS[DEFAULT_PRESET]))


# Human display names for the onboarding picker (app-facing; kept beside the
# presets so times AND labels share one source of truth).
PRESET_NAMES: dict[str, str] = {
    "classic": "Classic",
    "balanced": "Balanced",
    "bookends": "Bookends",
    "through_the_day": "Through the day",
}


def presets_payload() -> dict:
    """The onboarding picker payload (M9.8) — server-authoritative.

    The Flutter picker renders THESE times (never a copied list), so the
    displayed slots can't drift from the gate's PRESETS. Static config.
    """
    return {
        "default": DEFAULT_PRESET,
        "presets": {
            pid: {
                "name": PRESET_NAMES.get(pid, pid),
                "weekday": list(p.get("weekday") or []),
                "weekend": list(p.get("weekend") or []),
            }
            for pid, p in PRESETS.items()
        },
    }


# ── The gate (pure — no IO) ────────────────────────────────────────────────

def briefing_due_now(schedule: dict, now: datetime) -> bool:
    """True if `now` falls within a window of one of the tenant's slots.

    PURE function — unit-testable without a DB. Weekday slots apply
    Mon–Fri, weekend slots Sat–Sun, evaluated on the caller-provided
    `now` (which MUST already be in the tenant's timezone).

    The 15-minute window plus the 30-minute heartbeat guarantees each slot
    is hit by exactly ONE heartbeat: slots live on the :00/:30 grid, and a
    slot 30 minutes away is outside the window (|diff| > 15).
    """
    if not isinstance(schedule, dict):
        return False
    weekday = now.weekday() < 5
    slots = schedule.get("weekday") if weekday else schedule.get("weekend")
    if not slots:
        return False
    window = int(schedule.get("window_minutes", 15) or 15)
    now_min = now.hour * 60 + now.minute
    for slot in slots:
        try:
            hh, mm = slot.split(":")
            slot_min = int(hh) * 60 + int(mm)
        except Exception:
            continue
        if abs(now_min - slot_min) <= window:
            return True
    return False
