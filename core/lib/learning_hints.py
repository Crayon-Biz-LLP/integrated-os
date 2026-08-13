"""Learning-loop consumption for the action planner (vision #4).

Phase 4 emits clarification observations (``subsystem="action_planner"``) into
the codebase's telemetry machinery (``subsystem_telemetry`` +
``subsystem_patterns`` pattern counters). This module is the read side: at
plan time it summarizes which operations keep needing clarification and turns
that into a targeted prompt hint — so Rhodey gets better at the classes of
mistakes the user has already corrected. The loop closes instead of only
logging.

Fail-open by design: a telemetry/DB hiccup can never break planning (an empty
hint is rendered as nothing). The read is TTL-cached per process so it adds
no per-message latency beyond the cache window.
"""

import time

from core.lib.audit_logger import audit_log_sync
from core.lib.telemetry import MIN_PATTERN_OBSERVATIONS, get_pattern_summary

# Prompt hints per operation, keyed on the missing parameter token that
# triggered the clarification. Field tokens are matched as substrings of the
# joined missing_fields, so "reschedule.new_reminder_at.time_delta" hits the
# new_reminder_at hint.
_OP_FIELD_HINTS = {
    "reschedule": {
        "new_reminder_at": (
            "reschedule: you have repeatedly omitted the new time on this task. "
            "You MUST include params.new_reminder_at (absolute ISO time) or "
            "params.time_delta."
        ),
        "time_delta": (
            "reschedule: you have repeatedly omitted the new time on this task. "
            "You MUST include params.new_reminder_at (absolute ISO time) or "
            "params.time_delta."
        ),
    },
    "modify_recurring": {
        "new_rrule": (
            "modify_recurring: you have repeatedly omitted the schedule change. "
            "You MUST include params.new_rrule and/or params.new_reminder_at / "
            "params.time_delta."
        ),
        "new_reminder_at": (
            "modify_recurring: you have repeatedly omitted the new time. "
            "You MUST include params.new_rrule and/or params.new_reminder_at / "
            "params.time_delta."
        ),
    },
    "update_metadata": {
        "new_priority": (
            "update_metadata: you have repeatedly omitted what to change. "
            "You MUST include params.new_priority and/or params.new_deadline."
        ),
        "new_deadline": (
            "update_metadata: you have repeatedly omitted what to change. "
            "You MUST include params.new_priority and/or params.new_deadline."
        ),
    },
}

_HINT_CACHE: dict = {"ts": 0.0, "value": ""}
_HINT_TTL_S = 300  # 5 min — clarifications are rare, staleness is harmless


def build_planner_hint(patterns: list) -> str:
    """Translate action_planner pattern rows into a multi-line prompt hint.

    Only operations with >= MIN_PATTERN_OBSERVATIONS clarifications in the
    window get a hint (the codebase's bar for "meaningful pattern"). Rows are
    re-ranked by total_count — the hint targets the *frequent* failure
    classes, not the confident ones. Returns "" (renders as nothing) when
    there is nothing to learn yet.
    """
    if not patterns:
        return ""
    ranked = sorted(patterns, key=lambda p: (p.get("total_count") or 0), reverse=True)
    seen: set = set()  # dedupe on resolved hint text (new_reminder_at + time_delta
    # map to the same reschedule reminder)
    lines: list = []
    for p in ranked:
        features = p.get("features") or {}
        op = features.get("operation")
        total = p.get("total_count") or 0
        if not op or total < MIN_PATTERN_OBSERVATIONS:
            continue
        missing = features.get("missing_fields")
        missing_text = " ".join(missing) if isinstance(missing, list) else str(missing or "")
        for field, hint in (_OP_FIELD_HINTS.get(op) or {}).items():
            if field in missing_text and hint not in seen:
                seen.add(hint)
                lines.append(f"- {hint}")
    return "\n".join(lines)


async def get_action_planner_hint() -> str:
    """Fetch the current planner learning hint (async, cached, fail-open)."""
    now = time.monotonic()
    if _HINT_CACHE["ts"] and now - _HINT_CACHE["ts"] < _HINT_TTL_S:
        return _HINT_CACHE["value"]
    hint = ""
    try:
        patterns = await get_pattern_summary(
            "action_planner",
            min_observations=MIN_PATTERN_OBSERVATIONS,
            max_patterns=50,
            days_back=30,
        )
        hint = build_planner_hint(patterns)
    except Exception as e:
        audit_log_sync("telemetry", "WARNING",
                       f"action_planner hint failed (non-critical): {e}")
    _HINT_CACHE["ts"] = now
    _HINT_CACHE["value"] = hint
    return hint
