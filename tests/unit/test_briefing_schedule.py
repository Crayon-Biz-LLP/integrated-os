"""Boundary-clock matrix for the M9.7 briefing-schedule gate (pulse aspect).

Covers `core/services/briefing_schedule.py`:
  - `briefing_due_now`: the PURE schedule-window gate. Weekday slots apply
    Mon–Fri, weekend slots Sat–Sun, each slot fires within ±window_minutes.
    This is the boundary matrix the plan's #6 clock item demands: window
    edges (07:44 vs 07:45), weekday/weekend separation, the single-fire
    guarantee on the :00/:30 heartbeat grid, midnight rollover.
  - `_validate_schedule`: malformed slots ("99:99", "8:00", non-numeric)
    rejected fail-closed; window clamped 1..15 (|30| > 15 keeps each slot
    hit by exactly ONE heartbeat).
  - `resolve_briefing_schedule`: fail-closed → balanced default when the
    owner-scoped row is missing or broken.
  - `schedule_for_preset` / `presets_payload`: template + picker payload.

All pure or mocked-DB — no network, no real rows.
"""

import json
import pytest
from datetime import datetime
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

from core.services.briefing_schedule import (
    PRESETS,
    DEFAULT_PRESET,
    briefing_due_now,
    clear_cache,
    presets_payload,
    resolve_briefing_schedule,
    schedule_for_preset,
    _validate_schedule,
)

pytestmark = pytest.mark.pulse

IST = ZoneInfo("Asia/Kolkata")


@pytest.fixture(autouse=True)
def _fresh_schedule_cache():
    """The module caches per-user schedules for 60s — clear so tests never
    serve each other's rows."""
    clear_cache()
    yield
    clear_cache()


def _ist(day: int, hour: int, minute: int = 0) -> datetime:
    """Monday-anchored helper: day 1 = Monday, 7 = Sunday (isoweekday)."""
    return datetime(2026, 1, 5, hour, minute, tzinfo=IST) + __import__("datetime").timedelta(days=day - 1)


# ------------------------------------------------- briefing_due_now: slots

def test_weekday_slots_fire_on_weekdays():
    balanced = schedule_for_preset("balanced")  # weekday 08:00/13:00/19:00
    for hh, mm in [(8, 0), (13, 0), (19, 0)]:
        assert briefing_due_now(balanced, _ist(1, hh, mm)) is True  # Mon


def test_weekday_slots_do_not_fire_on_weekend():
    balanced = schedule_for_preset("balanced")
    # Sunday: weekday slot 08:00 is NOT due (weekend slots are 09:00/17:00)
    assert briefing_due_now(balanced, _ist(7, 8, 0)) is False
    assert briefing_due_now(balanced, _ist(7, 13, 0)) is False
    # weekend slots DO fire
    assert briefing_due_now(balanced, _ist(7, 9, 0)) is True
    assert briefing_due_now(balanced, _ist(7, 17, 0)) is True


def test_saturday_uses_weekend_slots():
    balanced = schedule_for_preset("balanced")
    assert briefing_due_now(balanced, _ist(6, 9, 0)) is True
    assert briefing_due_now(balanced, _ist(6, 8, 0)) is False  # weekday-only slot


def test_friday_evening_slot_is_still_weekday():
    classic = schedule_for_preset("classic")  # weekday 20:00 included
    assert briefing_due_now(classic, _ist(5, 20, 0)) is True


# ------------------------------------------- briefing_due_now: window edges

def test_window_edges_are_inclusive_at_15_minutes():
    classic = schedule_for_preset("classic")  # slot 07:30, window 15
    assert briefing_due_now(classic, _ist(1, 7, 15)) is True   # exactly 15 min before
    assert briefing_due_now(classic, _ist(1, 7, 45)) is True   # exactly 15 min after
    assert briefing_due_now(classic, _ist(1, 7, 14)) is False  # 16 min before
    assert briefing_due_now(classic, _ist(1, 7, 46)) is False  # 16 min after


def test_single_fire_guarantee_on_heartbeat_grid():
    """A heartbeat lands in at most ONE slot's window — classic slots are
    30+ min apart, so |diff| > 15 always for the non-matching neighbor."""
    classic = schedule_for_preset("classic")  # 07:30, 11:30, ...
    # 07:45 heartbeat: inside 07:30's window, OUTSIDE 11:30's
    assert briefing_due_now(classic, _ist(1, 7, 45)) is True
    # 11:15 heartbeat: inside 11:30's window, OUTSIDE 07:30's (|675-450|=225)
    assert briefing_due_now(classic, _ist(1, 11, 15)) is True


# --------------------------------------------- briefing_due_now: midnight

def test_midnight_rollover_does_not_fire_previous_day_slot():
    """A 00:00 slot fires after midnight only — 23:45 the day before is 23h45m
    away in absolute minutes, so it must NOT be due."""
    sched = {"preset": "x", "weekday": ["00:00"], "weekend": ["00:00"], "window_minutes": 15}
    assert briefing_due_now(sched, _ist(1, 0, 0)) is True
    assert briefing_due_now(sched, _ist(1, 23, 45)) is False  # previous day, next slot


def test_slot_near_midnight_does_not_leak_into_next_day():
    """A 23:50 slot fires at 23:55 Monday; at 00:05 Tuesday it is 23h45m away
    (absolute minutes, no wraparound) — so the next day must NOT fire."""
    sched = {"preset": "x", "weekday": ["23:50"], "weekend": ["23:50"], "window_minutes": 15}
    assert briefing_due_now(sched, _ist(1, 23, 55)) is True
    assert briefing_due_now(sched, _ist(2, 0, 5)) is False


# ------------------------------------------- briefing_due_now: fail-closed

def test_malformed_schedule_fail_closed():
    assert briefing_due_now(None, _ist(1, 8, 0)) is False
    assert briefing_due_now({}, _ist(1, 8, 0)) is False
    assert briefing_due_now({"weekday": [], "weekend": []}, _ist(1, 8, 0)) is False
    # bad slot strings are skipped, never raised
    bad = {"weekday": ["boom", "08:00"], "weekend": ["09:00"], "window_minutes": 15}
    assert briefing_due_now(bad, _ist(1, 8, 0)) is True
    assert briefing_due_now(bad, _ist(1, 7, 0)) is False


# --------------------------------------------------- _validate_schedule

def test_validate_rejects_bad_slots():
    for bad_slot in ["99:99", "8:00", "08:0", "8", "ab:cd", "08:00:00"]:
        assert _validate_schedule({"weekday": [bad_slot], "weekend": ["09:00"]}) is None, bad_slot
    assert _validate_schedule({"weekday": ["08:00"], "weekend": "not-a-list"}) is None
    assert _validate_schedule("not-a-dict") is None


def test_validate_clamps_window_to_1_15():
    # 120 → capped at 15 (the heartbeat-grid guarantee: |30| > 15)
    v = _validate_schedule({"weekday": ["08:00"], "weekend": ["09:00"], "window_minutes": 120})
    assert v["window_minutes"] == 15
    # 0 → the `or 15` fallback snaps it to the default (never a zero-width slot)
    v = _validate_schedule({"weekday": ["08:00"], "weekend": ["09:00"], "window_minutes": 0})
    assert v["window_minutes"] == 15
    # junk → default 15
    v = _validate_schedule({"weekday": ["08:00"], "weekend": ["09:00"], "window_minutes": "junk"})
    assert v["window_minutes"] == 15
    # negative → clamped UP to the 1-minute floor
    v = _validate_schedule({"weekday": ["08:00"], "weekend": ["09:00"], "window_minutes": -5})
    assert v["window_minutes"] == 1


def test_validate_accepts_valid():
    v = _validate_schedule({"preset": "balanced", "weekday": ["08:00", "13:00"], "weekend": ["09:00"], "window_minutes": 15})
    assert v == {"preset": "balanced", "weekday": ["08:00", "13:00"], "weekend": ["09:00"], "window_minutes": 15}


# ----------------------------------------- resolve_briefing_schedule fail-closed

def test_resolve_falls_back_to_balanced_when_row_missing():
    supabase = MagicMock()
    # maybe_single() returns no data → fail-closed to balanced
    supabase.table.return_value.select.return_value.eq.return_value.eq.return_value.limit.return_value.maybe_single.return_value.execute.return_value.data = None
    with patch("core.services.briefing_schedule.get_supabase", return_value=supabase):
        schedule = resolve_briefing_schedule("uid-missing")
    assert schedule == json.loads(json.dumps(PRESETS[DEFAULT_PRESET]))


def test_resolve_falls_back_to_balanced_on_garbage_row():
    supabase = MagicMock()
    supabase.table.return_value.select.return_value.eq.return_value.eq.return_value.limit.return_value.maybe_single.return_value.execute.return_value.data = {"content": '{"weekday": "not-a-list", "weekend": "nope"}'}
    with patch("core.services.briefing_schedule.get_supabase", return_value=supabase):
        schedule = resolve_briefing_schedule("uid-garbage")
    assert schedule["preset"] == DEFAULT_PRESET


def test_resolve_uses_validated_row_when_present():
    supabase = MagicMock()
    supabase.table.return_value.select.return_value.eq.return_value.eq.return_value.limit.return_value.maybe_single.return_value.execute.return_value.data = {
        "content": json.dumps({"preset": "custom", "weekday": ["06:00"], "weekend": ["07:00"], "window_minutes": 10})
    }
    with patch("core.services.briefing_schedule.get_supabase", return_value=supabase):
        schedule = resolve_briefing_schedule("uid-valid")
    assert schedule["weekday"] == ["06:00"]
    assert schedule["window_minutes"] == 10


# ------------------------------------------------- templates + picker

def test_presets_and_payload_stay_in_sync():
    payload = presets_payload()
    assert payload["default"] == DEFAULT_PRESET
    assert set(payload["presets"]) == set(PRESETS)
    for pid, template in PRESETS.items():
        assert payload["presets"][pid]["weekday"] == template["weekday"]
        assert payload["presets"][pid]["weekend"] == template["weekend"]


def test_schedule_for_preset_unknown_returns_default():
    assert schedule_for_preset("nope")["preset"] == DEFAULT_PRESET
    assert schedule_for_preset(None)["preset"] == DEFAULT_PRESET
