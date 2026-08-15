"""Boundary-clock matrix for briefing-mode selection (briefing aspect).

Covers the extracted pure functions in `core/pulse/briefing.py`:
  - `_resolve_time_intelligence(now, user_name)`: the weekday/weekend /
    pre-Monday / Monday-morning / time-of-day branch that decides WHICH
    briefing the pulse engine produces. `now` is a timezone-aware datetime
    in the tenant's zone. Pins every boundary the plan's #6 clock matrix
    demands: Monday morning, afternoon/wrap-up split, Friday wrap-up,
    night wind-down, weekend, Friday-evening weekend entry, Sunday
    pre-Monday precedence, and the documented midnight edge (hour < 12
    includes 00:00 → a midnight pulse reads as "Morning check.").
  - `_map_pulse_mode(briefing_mode)`: the string → app pulse_mode mapping.

Pure — no DB, no network, no LLM.
"""

import pytest
from datetime import datetime
from zoneinfo import ZoneInfo

from core.pulse.briefing import _map_pulse_mode, _resolve_time_intelligence

pytestmark = pytest.mark.briefing

IST = ZoneInfo("Asia/Kolkata")


def _ist(day: int, hour: int, minute: int = 0) -> datetime:
    """Monday-anchored: day 1 = Monday, 7 = Sunday (isoweekday)."""
    return datetime(2026, 1, 5, hour, minute, tzinfo=IST) + __import__("datetime").timedelta(days=day - 1)


def _mode(day: int, hour: int, minute: int = 0):
    return _resolve_time_intelligence(_ist(day, hour, minute), user_name="Danny")


# ------------------------------------------------- time-of-day (weekday)

def test_monday_morning_09_30_is_morning_check_and_monday_morning():
    ti = _mode(1, 9, 30)
    assert ti["briefing_mode"] == "Morning check."
    assert ti["is_monday_morning"] is True
    assert ti["is_weekend"] is False
    assert "Danny" in ti["system_persona"]  # persona is personalized


def test_monday_morning_boundary_11_00_not_monday_morning():
    assert _mode(1, 10, 59)["is_monday_morning"] is True
    assert _mode(1, 11, 0)["is_monday_morning"] is False  # is_monday_morning = hour < 11
    assert _mode(1, 11, 0)["briefing_mode"] == "Morning check."  # hour < 12


def test_afternoon_window_12_00_to_15_29():
    assert _mode(1, 12, 0)["briefing_mode"] == "Afternoon check."
    assert _mode(1, 14, 59)["briefing_mode"] == "Afternoon check."
    assert _mode(1, 15, 0)["briefing_mode"] == "Afternoon check."   # hour == 15, minute < 30
    assert _mode(1, 15, 29)["briefing_mode"] == "Afternoon check."
    assert _mode(1, 15, 30)["briefing_mode"] == "Wrap-up."          # minute >= 30 flips


def test_wrap_up_window_15_30_to_18_59():
    assert _mode(1, 15, 30)["briefing_mode"] == "Wrap-up."
    assert _mode(1, 18, 59)["briefing_mode"] == "Wrap-up."
    assert _mode(1, 19, 0)["briefing_mode"] == "Night wind-down."


def test_friday_gets_friday_wrap_up_not_plain_wrap_up():
    assert _mode(5, 16, 0)["briefing_mode"] == "Friday wrap-up."
    assert _mode(5, 16, 0)["system_persona"] == "Help Danny close the work week: what's done, what can wait. Be dry."
    # Thursday same time → plain wrap-up
    assert _mode(4, 16, 0)["briefing_mode"] == "Wrap-up."


def test_night_wind_down_after_19_00():
    ti = _mode(1, 19, 0)
    assert ti["briefing_mode"] == "Night wind-down."
    assert ti["is_weekend"] is False
    assert _mode(1, 23, 0)["briefing_mode"] == "Night wind-down."


# --------------------------------------------- weekend / pre-Monday edges

def test_saturday_is_weekend_mode():
    ti = _mode(6, 10, 0)
    assert ti["is_weekend"] is True
    assert ti["briefing_mode"] == "Weekend: Chores and Ideas."
    assert "Home, Family, and Chores" in ti["system_persona"]


def test_sunday_before_19_00_is_weekend_not_pre_monday():
    ti = _mode(7, 18, 0)
    assert ti["is_weekend"] is True
    assert ti["is_pre_monday"] is False
    assert ti["briefing_mode"] == "Weekend: Chores and Ideas."


def test_sunday_19_00_pre_monday_takes_precedence_over_weekend():
    ti = _mode(7, 20, 0)
    assert ti["is_weekend"] is True       # Sunday is weekend...
    assert ti["is_pre_monday"] is True    # ...but ≥19:00 flips to pre-Monday
    assert ti["briefing_mode"] == "Pre-Monday: Loading the Week."
    assert "Pre-load Monday" in ti["system_persona"]


def test_friday_evening_19_00_enters_weekend_mode():
    """Friday ≥19:00 counts as weekend — the work week is over."""
    ti = _mode(5, 19, 30)
    assert ti["is_weekend"] is True
    assert ti["briefing_mode"] == "Weekend: Chores and Ideas."
    assert _mode(5, 18, 59)["is_weekend"] is False  # 18:59 still weekday
    assert _mode(5, 18, 59)["briefing_mode"] == "Friday wrap-up."  # day-5 branch


# ------------------------------------------------------- midnight edge

def test_midnight_reads_as_morning_check_documented_edge():
    """Current behavior: hour < 12 INCLUDES 00:00, so a midnight pulse is a
    \"Morning check.\" Pinned as the documented edge — not a crash, not a
    weekend mode. If this ever changes, the change is intentional + tested."""
    ti = _mode(1, 0, 30)
    assert ti["briefing_mode"] == "Morning check."
    assert ti["is_monday_morning"] is True


def test_midnight_saturday_weekend_flag_wins_over_hour():
    """Saturday 00:15 is a weekend pulse — the day-based is_weekend flag
    takes precedence over the hour-based mode branch."""
    ti = _mode(6, 0, 15)
    assert ti["is_weekend"] is True
    assert ti["briefing_mode"] == "Weekend: Chores and Ideas."


# --------------------------------------------------- _map_pulse_mode

def test_pulse_mode_mapping_covers_every_branch():
    assert _map_pulse_mode("Morning check.") == "morning"
    assert _map_pulse_mode("Afternoon check.") == "afternoon"
    assert _map_pulse_mode("Friday wrap-up.") == "closing_loop"
    assert _map_pulse_mode("Wrap-up.") == "closing_loop"
    assert _map_pulse_mode("Weekend: Chores and Ideas.") == "weekend"
    assert _map_pulse_mode("Pre-Monday: Loading the Week.") == "pre_monday"
    assert _map_pulse_mode("Night wind-down.") == "intel"


def test_pulse_mode_mapping_fallback_and_substring_guards():
    assert _map_pulse_mode("Something else entirely") == "check_in"
    assert _map_pulse_mode("") == "check_in"
    # substrings still match: custom modes carrying the keywords
    assert _map_pulse_mode("Closing the loop now") == "closing_loop"
    assert _map_pulse_mode("Sign off") == "closing_loop"
    assert _map_pulse_mode("Intel briefing") == "intel"
