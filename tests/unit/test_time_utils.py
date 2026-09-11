"""Phase 2 deterministic-time tests (no DB required).

Covers `resolve_time_delta` (the LLM extracts the delta, code does the math —
invariant #2) and the `resolve_relative_dates` delta extensions
("in/by N days/weeks", "next week", "in a week", "a week from now").

Run: python -m pytest tests/unit/test_time_utils.py -v
"""

import pytest


from datetime import datetime, timedelta, timezone

from core.lib.time_utils import (
    derive_due_fields,
    extract_time_delta,
    resolve_relative_dates,
    resolve_time_delta,
)
pytestmark = pytest.mark.decision


IST = timezone(timedelta(hours=5, minutes=30))
# Aug 12, 2026 14:48 IST — the Aug 12 incident's reference time
REF = datetime(2026, 8, 12, 14, 48, tzinfo=IST)


# ── resolve_time_delta: code computes, LLM never does arithmetic ──


def test_delta_days_later():
    assert resolve_time_delta({"amount": 7, "unit": "days", "direction": "later"}, REF) \
        == REF + timedelta(days=7)


def test_delta_weeks():
    assert resolve_time_delta({"amount": 2, "unit": "weeks"}, REF) == REF + timedelta(weeks=2)


def test_delta_earlier():
    assert resolve_time_delta({"amount": 3, "unit": "days", "direction": "earlier"}, REF) \
        == REF - timedelta(days=3)


def test_delta_hours():
    assert resolve_time_delta({"amount": 4, "unit": "hours"}, REF) == REF + timedelta(hours=4)


def test_delta_defaults_to_days_later():
    assert resolve_time_delta({"amount": 2}, REF) == REF + timedelta(days=2)


def test_delta_bad_amount_coerces_to_one():
    assert resolve_time_delta({"amount": 0}, REF) == REF + timedelta(days=1)


def test_delta_result_is_timezone_aware():
    result = resolve_time_delta({"amount": 1, "unit": "days"})
    assert result.tzinfo is not None


# ── resolve_relative_dates: delta phrasings → absolute dates ──


def test_relative_dates_by_n_days():
    out = resolve_relative_dates("defer the purchase by 7 days", REF)
    assert "August 19, 2026" in out
    assert "by 7 days" not in out


def test_relative_dates_in_n_weeks():
    out = resolve_relative_dates("push it back in 2 weeks", REF)
    assert "August 26, 2026" in out


def test_relative_dates_next_week():
    out = resolve_relative_dates("move it to next week", REF)
    assert "August 19, 2026" in out


def test_relative_dates_in_a_week():
    out = resolve_relative_dates("defer it in a week", REF)
    assert "August 19, 2026" in out


def test_relative_dates_a_week_from_now():
    out = resolve_relative_dates("reschedule it a week from now", REF)
    assert "August 19, 2026" in out


def test_relative_dates_coexists_with_tomorrow():
    out = resolve_relative_dates("move it to tomorrow, not in 7 days", REF)
    assert "August 13, 2026" in out  # tomorrow


# ── extract_time_delta: deterministic backstop for the LLM flake ──


def test_extract_by_n_days():
    assert extract_time_delta("Defer the Ashraya domain purchase by 7 days") \
        == {"amount": 7, "unit": "days", "direction": "later"}


def test_extract_in_n_weeks():
    assert extract_time_delta("push it back in 2 weeks") \
        == {"amount": 2, "unit": "weeks", "direction": "later"}


def test_extract_n_days_from_now():
    assert extract_time_delta("reschedule it 5 days from now") \
        == {"amount": 5, "unit": "days", "direction": "later"}


def test_extract_a_week():
    assert extract_time_delta("push it back a week") \
        == {"amount": 1, "unit": "weeks", "direction": "later"}


def test_extract_two_more_weeks():
    assert extract_time_delta("give me two more weeks") \
        == {"amount": 2, "unit": "weeks", "direction": "later"}


def test_extract_marker_led_earlier():
    assert extract_time_delta("move the sync up 2 days") \
        == {"amount": 2, "unit": "days", "direction": "earlier"}


def test_extract_none_when_no_delta():
    assert extract_time_delta("reschedule the purchase") is None
    assert extract_time_delta("what's on my calendar") is None
    assert extract_time_delta("") is None
    assert extract_time_delta(None) is None


# ── derive_due_fields: deterministic (reminder_at, deadline) for creation ──
# Sep 10 Gopi regression: "Remind me to call Gopi ... tomorrow at 11AM" was
# created dateless because the planner prompt nulled reminder_at for
# "tomorrow". These tests pin the code-does-the-arithmetic invariant.


def test_derive_tomorrow_at_time():
    reminder, deadline, _ = derive_due_fields(
        "Remind me to call Gopi from Nithminds Recruitment tomorrow at 11AM.", REF
    )
    assert reminder is not None and deadline is not None
    assert reminder.startswith("2026-08-13T11:00:00")
    assert deadline == "2026-08-13"


def test_derive_today_at_time_lowercase_pm():
    reminder, deadline, _ = derive_due_fields("call the vendor today at 3:30pm", REF)
    assert reminder.startswith("2026-08-12T15:30:00")
    assert deadline == "2026-08-12"


def test_derive_date_only_no_time_invented():
    reminder, deadline, _ = derive_due_fields("submit the invoice by tomorrow", REF)
    assert reminder is None
    assert deadline == "2026-08-13"


def test_derive_no_date_phrase():
    assert derive_due_fields("remind me to call Gopi", REF) == (None, None, None)
    assert derive_due_fields("", REF) == (None, None, None)
    assert derive_due_fields(None, REF) == (None, None, None)


def test_derive_in_days():
    reminder, deadline, _ = derive_due_fields("follow up in 3 days", REF)
    assert reminder is None
    assert deadline == "2026-08-15"


def test_derive_next_weekday():
    # REF is a Wednesday — next Friday = Aug 21.
    reminder, deadline, _ = derive_due_fields("meeting next Friday at 2pm", REF)
    assert reminder.startswith("2026-08-21T14:00:00")
    assert deadline == "2026-08-21"


def test_derive_bare_ambiguous_hour_not_invented():
    # Bare "at 11" without am/pm is ambiguous — must not silently become 11:00.
    reminder, deadline, _ = derive_due_fields("remind me tomorrow at 11", REF)
    assert reminder is None
    assert deadline == "2026-08-13"

def test_derive_absolute_date():
    # Base reference date is 2026-08-12 (Wed)
    # Day-first with time
    rem, dl, dur = derive_due_fields("dinner on 18th September at 7pm till 9pm", REF)
    assert rem.startswith("2026-09-18T19:00:00")
    assert dl == "2026-09-18"
    assert dur == 120

    # Month-first with time
    rem, dl, dur = derive_due_fields("meeting on Sep 18, 7 PM", REF)
    assert rem.startswith("2026-09-18T19:00:00")
    assert dl == "2026-09-18"
    assert dur is None

    # Date only, no time
    rem, dl, dur = derive_due_fields("due 25th October", REF)
    assert rem is None
    assert dl == "2026-10-25"
    assert dur is None

def test_derive_absolute_date_rollover():
    # If the absolute date has passed in the current year, roll to next year.
    # REF is 2026-08-12.
    rem, dl, _ = derive_due_fields("meeting on March 5", REF)
    assert rem is None
    assert dl == "2027-03-05"

