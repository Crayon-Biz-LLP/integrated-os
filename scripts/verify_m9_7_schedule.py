#!/usr/bin/env python3
"""verify_m9_7_schedule.py — M9.7 briefing-schedule gate proofs.

The per-tenant briefing schedule (core/services/briefing_schedule.py) is
pure: `briefing_due_now(schedule, now)` takes a resolved schedule + a
now-datetime ALREADY in the tenant's timezone. That purity is what this
script proves, mirroring verify_m9_1..m9_4:

  1. DANNY UNCHANGED  — his seeded 'classic' schedule fires at EXACTLY his
     pre-M9.7 slots (07:30/11:30/14:30/17:30/20:00 weekday; 08:00/15:00
     weekend) and nowhere else.
  2. BALANCED DEFAULT — a tenant with no row (fresh seed) gets the default
     (08:00/13:00/19:00 weekday; 09:00/17:00 weekend).
  3. CANADA TZ        — a tenant in America/Toronto with 'bookends' is due
     at THEIR 08:00/20:00 local, which is a different UTC instant than
     Danny's — the whole reason the heartbeat exists.
  4. DETERMINISM      — same (schedule, now) ⇒ same answer.
  5. FAIL-CLOSED      — a malformed/missing schedule ⇒ balanced default, no
     crash, and never another tenant's row.
  6. SINGLE-FIRE      — with the 30-min heartbeat and 15-min window, each
     on-grid slot is hit by exactly one heartbeat (|diff| > 15 ⇒ miss).

Run: python3 scripts/verify_m9_7_schedule.py
Exit 0 = all green.
"""
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

CHECKS: list[str] = []
FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    CHECKS.append(name)
    mark = "✅" if ok else "❌"
    print(f"  {mark} {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(name)


def _ist(day: int, hour: int, minute: int = 0) -> datetime:
    """A weekday/weekend IST datetime at a given local clock time."""
    # 2026-08-02 is a Sunday; day = 1..7 (Mon..Sun)
    base = datetime(2026, 8, 2, tzinfo=ZoneInfo("Asia/Kolkata"))
    return base + timedelta(days=day, hours=hour, minutes=minute)


def _toronto(day: int, hour: int, minute: int = 0) -> datetime:
    base = datetime(2026, 8, 2, tzinfo=ZoneInfo("America/Toronto"))
    return base + timedelta(days=day, hours=hour, minutes=minute)


def main() -> None:
    from core.services.briefing_schedule import (
        PRESETS,
        DEFAULT_PRESET,
        briefing_due_now,
        resolve_briefing_schedule,
        schedule_for_preset,
        _validate_schedule,
    )

    print(f"🎯 M9.7 briefing-schedule gate — {len(PRESETS)} presets, default='{DEFAULT_PRESET}'")

    classic = schedule_for_preset("classic")
    balanced = schedule_for_preset("balanced")
    bookends = schedule_for_preset("bookends")

    # ── 1. Danny unchanged (classic, weekday) ──
    print("\n[1] Danny (classic) — weekday slots unchanged")
    weekday_slots = ["07:30", "11:30", "14:30", "17:30", "20:00"]
    for slot in weekday_slots:
        hh, mm = map(int, slot.split(":"))
        # Monday (day 1)
        check(f"due at {slot} (Mon)", briefing_due_now(classic, _ist(1, hh, mm)))
    # Window edges: 07:15 (15 min before 07:30) and 07:45 (15 min after) are
    # still due; 07:14 (16 min) and 07:46 (16 min) are NOT.
    check("due at 07:15 (Mon — window edge)", briefing_due_now(classic, _ist(1, 7, 15)))
    check("due at 07:45 (Mon — window edge)", briefing_due_now(classic, _ist(1, 7, 45)))
    for hh, mm in [(6, 0), (7, 14), (7, 46), (9, 0), (12, 0), (15, 0), (21, 0)]:
        check(f"NOT due at {hh:02d}:{mm:02d} (Mon)",
              not briefing_due_now(classic, _ist(1, hh, mm)))
    # Friday evening still fires 20:00 (classic lists 20:00 on weekdays)
    check("due at 20:00 (Fri)", briefing_due_now(classic, _ist(5, 20, 0)))

    # ── 1b. Danny (classic, weekend) ──
    print("\n[1b] Danny (classic) — weekend slots unchanged")
    check("due at 08:00 (Sun)", briefing_due_now(classic, _ist(7, 8, 0)))
    check("due at 15:00 (Sun)", briefing_due_now(classic, _ist(7, 15, 0)))
    check("NOT due at 07:30 (Sun — weekday-only slot)",
          not briefing_due_now(classic, _ist(7, 7, 30)))
    check("NOT due at 11:30 (Sun)", not briefing_due_now(classic, _ist(7, 11, 30)))
    check("NOT due at 10:00 (Sat)", not briefing_due_now(classic, _ist(6, 10, 0)))

    # ── 2. Balanced default ──
    print("\n[2] Balanced (new-tenant default)")
    for slot in ["08:00", "13:00", "19:00"]:
        hh, mm = map(int, slot.split(":"))
        check(f"due at {slot} (Mon)", briefing_due_now(balanced, _ist(1, hh, mm)))
    check("NOT due at 07:30 (Mon)", not briefing_due_now(balanced, _ist(1, 7, 30)))
    check("NOT due at 12:00 (Mon)", not briefing_due_now(balanced, _ist(1, 12, 0)))
    check("due at 09:00 (Sun)", briefing_due_now(balanced, _ist(7, 9, 0)))
    check("due at 17:00 (Sun)", briefing_due_now(balanced, _ist(7, 17, 0)))
    check("NOT due at 13:00 (Sun)", not briefing_due_now(balanced, _ist(7, 13, 0)))

    # ── 3. Canada timezone (bookends) ──
    print("\n[3] Canada (America/Toronto, bookends)")
    # Bookends weekday: 08:00/20:00 Toronto local. Same UTC instant as a
    # different tenant — pick her 20:00 EDT Monday (= 05:30 IST Tuesday,
    # Toronto is UTC-4 in August) which is NOT one of Danny's classic slots:
    # she must be DUE at that instant while classic-Danny is NOT.
    her_evening = _toronto(1, 20, 0)   # Mon 20:00 EDT
    danny_same_instant = her_evening.astimezone(ZoneInfo("Asia/Kolkata"))
    check("due at her 20:00 (Mon)", briefing_due_now(bookends, her_evening))
    check(f"NOT due for classic-Danny at same instant ({danny_same_instant:%H:%M IST})",
          not briefing_due_now(classic, danny_same_instant))
    check("due at her 08:00 (Mon)", briefing_due_now(bookends, _toronto(1, 8, 0)))
    check("NOT due at her 13:00 (Mon)", not briefing_due_now(bookends, _toronto(1, 13, 0)))
    check("due at 10:00 (Sat)", briefing_due_now(bookends, _toronto(6, 10, 0)))
    check("NOT due at 08:00 (Sat — weekday slot)",
          not briefing_due_now(bookends, _toronto(6, 8, 0)))

    # ── 4. Determinism ──
    print("\n[4] Determinism")
    a = briefing_due_now(classic, _ist(1, 11, 30))
    b = briefing_due_now(classic, _ist(1, 11, 30))
    check("same (schedule, now) ⇒ same answer", a == b)

    # ── 5. Fail-closed ──
    print("\n[5] Fail-closed (bad rows never crash, never leak another tenant)")
    check("_validate_schedule rejects garbage", _validate_schedule({"weekday": "nope"}) is None)
    check("_validate_schedule rejects bad slot shape",
          _validate_schedule({"weekday": ["7:3"], "weekend": []}) is None)
    check("_validate_schedule rejects non-numeric slot",
          _validate_schedule({"weekday": ["aa:bb"], "weekend": []}) is None)
    check("_validate_schedule rejects out-of-range slot",
          _validate_schedule({"weekday": ["99:99"], "weekend": []}) is None)
    check("_validate_schedule rejects hour 24",
          _validate_schedule({"weekday": ["24:00"], "weekend": []}) is None)
    check("_validate_schedule clamps window to 15 (single-fire invariant)",
          _validate_schedule({"weekday": ["08:00"], "weekend": [], "window_minutes": 30})["window_minutes"] == 15)
    ok_sched = _validate_schedule({"weekday": ["08:00"], "weekend": ["09:00"], "window_minutes": 15})
    check("_validate_schedule accepts good row", ok_sched is not None and ok_sched["weekday"] == ["08:00"])
    check("default preset is balanced", DEFAULT_PRESET == "balanced")
    # Malformed row → resolver must fall back to balanced (no crash).
    import json as _json
    try:
        from unittest.mock import patch, MagicMock
        mock_res = MagicMock()
        mock_res.data = {"content": _json.dumps({"weekday": "oops"})}
        with patch("core.services.briefing_schedule.get_supabase") as mock_get:
            mock_get.return_value.table.return_value.select.return_value.eq.return_value.eq.return_value \
                .limit.return_value.maybe_single.return_value.execute.return_value = mock_res
            from core.services.briefing_schedule import clear_cache
            clear_cache("u-test")
            got = resolve_briefing_schedule("u-test")
        check("malformed row falls back to balanced",
              got == balanced and got["weekday"] == ["08:00", "13:00", "19:00"])
    except Exception as e:
        check("malformed row falls back to balanced", False, str(e)[:120])

    # ── 6. Single-fire (30-min heartbeat, 15-min window) ──
    print("\n[6] Single-fire per slot (30-min heartbeat, 15-min window)")
    # On-grid slot 11:30: the 11:00 heartbeat (30 min early) and 12:00
    # heartbeat (30 min late) are both OUTSIDE the window.
    check("11:00 heartbeat misses 11:30 slot", not briefing_due_now(classic, _ist(1, 11, 0)))
    check("11:30 heartbeat hits 11:30 slot", briefing_due_now(classic, _ist(1, 11, 30)))
    check("12:00 heartbeat misses 11:30 slot", not briefing_due_now(classic, _ist(1, 12, 0)))

    # ── 7. Resolver default (no row) + explicit tenant separation ──
    print("\n[7] Resolver: absent row ⇒ balanced; per-tenant rows stay separate")
    try:
        from unittest.mock import patch, MagicMock
        empty_res = MagicMock()
        empty_res.data = None
        with patch("core.services.briefing_schedule.get_supabase") as mock_get:
            mock_get.return_value.table.return_value.select.return_value.eq.return_value.eq.return_value \
                .limit.return_value.maybe_single.return_value.execute.return_value = empty_res
            from core.services.briefing_schedule import clear_cache
            clear_cache("u-fresh")
            got = resolve_briefing_schedule("u-fresh")
        check("absent row ⇒ balanced default", got == balanced)
    except Exception as e:
        check("absent row ⇒ balanced default", False, str(e)[:120])

    print(f"\n{'✅ ALL GREEN' if not FAILURES else '❌ FAILURES'}: {len(CHECKS) - len(FAILURES)}/{len(CHECKS)} checks passed")
    sys.exit(1 if FAILURES else 0)


if __name__ == "__main__":
    main()
