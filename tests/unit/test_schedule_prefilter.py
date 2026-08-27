"""Gap C question gate — schedule pre-filter must only fire on interrogatives.

Regression coverage for the Run-3 PB incident: "We have scheduled a meeting
today at 8:30 PM" matched the bare schedule pattern and was force-routed to
QUERY with confidence 1.0, which suppressed the suggestion card and broke the
confirm pipeline. The gate now requires question shape ('?' suffix or
interrogative prefix) before the deterministic shortcut fires.

HERMETIC-DEPENDENCY NOTE: these tests walk classify_intent() up to the Gap C
block without mocks. That works because every earlier branch fails open or
short-circuits without a DB: the mark-done filter doesn't match, Guard 1's
completion verbs don't match, resolve_user_name() falls through to the env
name with no tenant context (and fail-opens anyway), and audit_log_sync is a
no-op without a supabase client. If a DB/settings lookup is ever inserted
above the Gap C block, these tests will start failing — fix by mocking that
lookup here rather than removing it from production.
"""

import pytest

from core.webhook.classify import classify_intent
from core.llm.constants import SAFE_HOLD_CLASSIFICATION

# Strict-mode asyncio: explicit marker required (repo convention, see
# test_executor_acks.py).
pytestmark = [pytest.mark.ingest, pytest.mark.asyncio]


# --------------------------------------------------------------- questions → QUERY fast path


@pytest.mark.parametrize(
    "message",
    [
        "What's on my calendar today?",
        "Do I have anything on Tuesday?",
        "Am I busy tomorrow?",
        "What meetings this week?",
        "Meetings today?",  # elliptical question — '?' alone is enough
    ],
)
async def test_gap_c_questions_take_prefilter_fast_path(message):
    result = await classify_intent(message, [])
    assert result["intent"] == "QUERY", f"{message!r} should be QUERY via pre-filter"
    assert result["reasoning"].startswith("Deterministic pre-filter"), (
        f"{message!r} should hit the deterministic shortcut, not fall to the LLM"
    )


# --------------------------------------------------------- statements → fall through to LLM


@pytest.mark.parametrize(
    "message",
    [
        # The original incident messages — statements containing schedule nouns.
        "We have a meeting today at 8:30 PM",
        "We have scheduled a meeting on Tuesday",
        # Keyword-only landmines behind the gate:
        "Add this to the agenda for Tuesday",
        "Block my calendar Tuesday 8:30",
        "Upcoming: PB meet-and-greet with the team",
    ],
)
async def test_gap_c_statements_do_not_take_prefilter(message, monkeypatch):
    """Statements must NOT be force-routed to QUERY by the deterministic filter.

    They fall through toward the LLM path. We stub the LLM call so this stays
    hermetic and fast (no provider retries) — what matters is that the
    pre-filter did not claim the message first.
    """

    async def _fake_llm(**kwargs):
        return SAFE_HOLD_CLASSIFICATION

    import core.webhook.classify as classify_mod
    monkeypatch.setattr(
        classify_mod, "generate_content_with_fallback", _fake_llm
    )
    result = await classify_intent(message, [])
    assert not (
        result.get("intent") == "QUERY"
        and str(result.get("reasoning", "")).startswith("Deterministic pre-filter")
    ), f"{message!r} must not be force-routed to QUERY by the pre-filter"


# --------------------------------------- schedule-meeting pre-filter → TASK deterministically


@pytest.mark.parametrize(
    "message",
    [
        "Schedule meeting with Havnelight team on Thursday at 11 AM",
        "Schedule a call with David about the project",
        "Arrange a meeting with the client next week",
        "Book a demo with Quantum Analytics",
        "Set up a sync with Elena Vasquez",
        "Create a meeting with Marcus Webster tomorrow",
        "Add a meeting with the team on Friday",
        "Plan a catch-up with the Havnelight team",
        "Organize a review session with Cobalt and Finch",
        "Fix a chat with David at Google",
    ],
)
async def test_schedule_meeting_prefilter_forces_task(message):
    result = await classify_intent(message, [])
    assert result["intent"] == "TASK", f"{message!r} should be TASK via schedule-meeting pre-filter"
    assert result["reasoning"].startswith("Deterministic pre-filter"), (
        f"{message!r} should hit the deterministic shortcut, not fall to the LLM"
    )
