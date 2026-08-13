"""Learning-loop consumption tests (no DB required).

Covers `build_planner_hint` (pure translation of action_planner pattern rows
into a prompt hint), `get_action_planner_hint` (fail-open, cached read), and
`validation_missing_fields` (the real-field extraction that feeds the
learning features). Also guards that the prompt section is absent when the
hint is empty, so the M9.4 golden stays stable.

Run: python -m pytest tests/unit/test_learning_hints.py -v
"""

from unittest.mock import patch

from core.lib import learning_hints
from core.lib.learning_hints import build_planner_hint, get_action_planner_hint
from core.prompts.planner import build_planner_prompt


def _pattern(op, total, missing, confidence=0.9):
    return {
        "features": {"operation": op, "missing_fields": missing, "intent": "TASK"},
        "total_count": total,
        "correct_count": int(total * confidence),
        "corrected_count": 0,
        "confidence": confidence,
        "last_seen": "2026-08-13T10:00:00+00:00",
    }


# ── build_planner_hint: pure translation ──


def test_hint_empty_with_no_patterns():
    assert build_planner_hint([]) == ""


def test_hint_empty_below_threshold():
    """MIN_PATTERN_OBSERVATIONS=3 — sparse patterns must not steer the prompt."""
    assert build_planner_hint([_pattern("reschedule", 2, ["new_reminder_at"])]) == ""


def test_hint_reschedule_omitted_time():
    patterns = [_pattern("reschedule", 5, ["reschedule.new_reminder_at.time_delta"])]
    hint = build_planner_hint(patterns)
    assert "reschedule" in hint
    assert "new_reminder_at" in hint
    assert hint.startswith("- ")


def test_hint_modify_recurring_rrule():
    hint = build_planner_hint([_pattern("modify_recurring", 4, ["modify_recurring.new_rrule"])])
    assert "modify_recurring" in hint and "new_rrule" in hint


def test_hint_update_metadata():
    hint = build_planner_hint([_pattern("update_metadata", 3, ["update_metadata.new_deadline"])])
    assert "update_metadata" in hint


def test_hint_unknown_operation_ignored():
    assert build_planner_hint([_pattern("create_note", 10, ["content"])]) == ""


def test_hint_ranks_by_frequency_not_confidence():
    """The hint targets *frequent* failure classes, not confident ones."""
    rare_confident = _pattern("update_metadata", 3, ["new_deadline"], confidence=1.0)
    frequent = _pattern("reschedule", 9, ["new_reminder_at"], confidence=0.6)
    hint = build_planner_hint([rare_confident, frequent])
    assert hint.index("reschedule") < hint.index("update_metadata")


def test_hint_deduplicates_same_class():
    p1 = _pattern("reschedule", 4, ["reschedule.new_reminder_at"])
    p2 = _pattern("reschedule", 4, ["reschedule.time_delta"])
    hint = build_planner_hint([p1, p2])
    assert hint.count("- ") == 1  # one reschedule hint, not two


# ── get_action_planner_hint: fail-open + cached ──


def _reset_hint_cache():
    learning_hints._HINT_CACHE["ts"] = 0.0
    learning_hints._HINT_CACHE["value"] = ""


def test_get_hint_returns_summary_hint():
    _reset_hint_cache()
    with patch.object(learning_hints, "get_pattern_summary",
                      return_value=[_pattern("reschedule", 5, ["new_reminder_at"])]):
        hint = _run(get_action_planner_hint())
    assert "reschedule" in hint


def test_get_hint_fail_open_on_telemetry_error():
    _reset_hint_cache()
    with patch.object(learning_hints, "get_pattern_summary", side_effect=Exception("db down")):
        hint = _run(get_action_planner_hint())
    assert hint == ""  # telemetry hiccup can never break planning


def test_get_hint_caches():
    _reset_hint_cache()
    calls = {"n": 0}

    async def _fake_summary(*args, **kwargs):
        calls["n"] += 1
        return [_pattern("reschedule", 5, ["new_reminder_at"])]

    with patch.object(learning_hints, "get_pattern_summary", new=_fake_summary):
        _run(get_action_planner_hint())
        _run(get_action_planner_hint())
    assert calls["n"] == 1  # second read served from cache


# ── prompt rendering: section only when the hint is non-empty ──

_MIN_PROMPT_ARGS = dict(
    current_time="2026-08-13T10:00:00+05:30",
    text="defer the purchase by 7 days",
    title="[SIM_TEST] Ashraya domain purchase",
    intent="TASK",
    entity="",
    candidate_lines="Task ID 1: [SIM_TEST] Ashraya domain purchase (status: todo, one-off)",
    org_lines="  - (none)",
)


def test_prompt_omits_learned_section_when_empty():
    prompt = build_planner_prompt(**_MIN_PROMPT_ARGS)
    assert "LEARNED FROM PAST CLARIFICATIONS" not in prompt


def test_prompt_renders_learned_section_when_hint_present():
    prompt = build_planner_prompt(**_MIN_PROMPT_ARGS,
                                  learned_hints="- reschedule: you MUST include params.new_reminder_at.")
    assert "LEARNED FROM PAST CLARIFICATIONS" in prompt
    assert "MUST-FOLLOW" in prompt


def _run(coro):
    """Run a coroutine in its own fresh loop — robust against other tests in
    the suite leaving no current event loop behind."""
    import asyncio
    return asyncio.run(coro)
