"""Phase 4 stateful-clarification tests (no DB / LLM required).

Covers `core/webhook/workflows.py` action_clarification machinery:
parking the pending action (invariant #5), decline aborts, unrelated replies
falling through to normal routing, and the resume path re-planning the
original text with the user's answer.

Run: python -m pytest tests/unit/test_workflow_clarification.py -v
"""



from unittest.mock import MagicMock

import pytest

import core.webhook.workflows as wf
pytestmark = pytest.mark.decision



# ── _looks_like_time_reply ──


def test_looks_like_time_reply_true():
    assert wf._looks_like_time_reply("to the 19th") is True
    assert wf._looks_like_time_reply("next friday") is True
    assert wf._looks_like_time_reply("in 7 days") is True
    assert wf._looks_like_time_reply("tomorrow 3pm") is True


def test_looks_like_time_reply_false():
    assert wf._looks_like_time_reply("what's the weather") is False
    assert wf._looks_like_time_reply("no") is False


# ── park_action_clarification ──


@pytest.mark.asyncio
async def test_park_action_clarification_inserts_workflow(monkeypatch):
    fake_client = MagicMock()
    monkeypatch.setattr(wf, "tenant_aware_client", lambda: fake_client)

    await wf.park_action_clarification(
        chat_id=12345,
        thread_id="thread-1",
        original_text="defer the ashraya purchase by 7 days",
        intent="COMPLETION",
        title="Purchase Ashraya domain",
        entity="ASHRAYA",
        operation="reschedule",
        target_id="2466",
        missing_fields=["new_reminder_at"],
    )

    # Supersedes prior active workflows for the thread
    fake_client.table("conversation_workflows").update.assert_called_once()
    # Inserts the pending action
    insert_args = fake_client.table("conversation_workflows").insert.call_args
    row = insert_args[0][0]
    assert row["workflow_type"] == "action_clarification"
    assert row["status"] == "active"
    assert row["awaiting_user_input"] is True
    assert row["chat_id"] == 12345
    assert row["thread_id"] == "thread-1"
    assert row["payload"]["operation"] == "reschedule"
    assert row["payload"]["target_id"] == "2466"
    assert row["payload"]["missing_fields"] == ["new_reminder_at"]
    assert row["payload"]["original_text"] == "defer the ashraya purchase by 7 days"
    assert "expires_at" in row


# ── _resume_action_clarification ──


def _make_workflow(**overrides):
    w = {
        "id": "wf-1",
        "workflow_type": "action_clarification",
        "payload": {
            "original_text": "defer the ashraya purchase by 7 days",
            "intent": "COMPLETION",
            "title": "Purchase Ashraya domain",
            "entity": "ASHRAYA",
            "operation": "reschedule",
            "target_id": "2466",
            "missing_fields": ["new_reminder_at"],
        },
    }
    w.update(overrides)
    return w


@pytest.mark.asyncio
async def test_resume_decline_cancels_and_acks(monkeypatch):
    fake_client = MagicMock()
    monkeypatch.setattr(wf, "tenant_aware_client", lambda: fake_client)
    sent = []

    async def _fake_send(cid, msg):
        sent.append((cid, msg))

    observations = []

    async def _fake_emit(**kwargs):
        observations.append(kwargs)

    monkeypatch.setattr(wf, "send_telegram", _fake_send)
    monkeypatch.setattr(wf, "log_exchange", lambda *a, **k: None)
    monkeypatch.setattr("core.lib.telemetry.emit_observation", _fake_emit)

    handled, ancillary = await wf._resume_action_clarification(12345, "never mind", "thread-1", _make_workflow())

    assert handled is True
    assert ancillary is None
    assert "won't change that" in sent[0][1]
    # Workflow cancelled
    update_args = fake_client.table("conversation_workflows").update.call_args
    assert update_args[0][0]["status"] == "cancelled"
    assert update_args[0][0]["resolved_at"]
    # Learning loop: the rejection is persisted
    assert observations and observations[0]["outcome"] == "rejected"
    assert observations[0]["features"]["operation"] == "reschedule"
    assert observations[0]["subsystem"] == "action_planner"


@pytest.mark.asyncio
async def test_resume_unrelated_falls_through(monkeypatch):
    monkeypatch.setattr(wf, "tenant_aware_client", lambda: MagicMock())

    async def _fake_send(*a, **k):
        pass

    monkeypatch.setattr(wf, "send_telegram", _fake_send)
    monkeypatch.setattr(wf, "log_exchange", lambda *a, **k: None)

    handled, ancillary = await wf._resume_action_clarification(12345, "what's the weather", "thread-1", _make_workflow())

    # Not consumed — the workflow stays active for a later answer
    assert handled is False
    assert ancillary is None


@pytest.mark.asyncio
async def test_resume_entityless_time_answer_still_resumes(monkeypatch):
    """A bare date reply (no entities) is the answer, not an unrelated msg."""
    fake_client = MagicMock()
    monkeypatch.setattr(wf, "tenant_aware_client", lambda: fake_client)
    sent = []

    async def _fake_send(cid, msg):
        sent.append((cid, msg))

    observations = []

    async def _fake_emit(**kwargs):
        observations.append(kwargs)

    monkeypatch.setattr(wf, "send_telegram", _fake_send)
    monkeypatch.setattr(wf, "log_exchange", lambda *a, **k: None)
    monkeypatch.setattr("core.lib.telemetry.emit_observation", _fake_emit)

    executed = []

    async def _fake_plan(text, title="", entity="", active_anchor=None, intent=None):
        return [MagicMock()], None

    async def _fake_execute(actions, chat_id, **kwargs):
        executed.append((actions, chat_id, kwargs))

    monkeypatch.setattr("core.lib.suggestion_extractor.extract_suggestions", _fake_plan)
    monkeypatch.setattr("core.actions.executor.execute_planned_actions", _fake_execute)

    handled, _ = await wf._resume_action_clarification(12345, "friday", "thread-1", _make_workflow())

    assert handled is True
    assert executed
    assert observations[0]["outcome"] == "confirmed"


@pytest.mark.asyncio
async def test_resume_replans_with_answer_and_resolves(monkeypatch):
    fake_client = MagicMock()
    monkeypatch.setattr(wf, "tenant_aware_client", lambda: fake_client)
    sent = []

    async def _fake_send(cid, msg):
        sent.append((cid, msg))

    observations = []

    async def _fake_emit(**kwargs):
        observations.append(kwargs)

    monkeypatch.setattr(wf, "send_telegram", _fake_send)
    monkeypatch.setattr(wf, "log_exchange", lambda *a, **k: None)
    monkeypatch.setattr("core.lib.telemetry.emit_observation", _fake_emit)

    planned = {"seen_text": None}
    executed = []

    async def _fake_plan(text, title="", entity="", active_anchor=None, intent=None):
        planned["seen_text"] = text
        return [MagicMock()], None

    async def _fake_execute(actions, chat_id, **kwargs):
        executed.append((actions, chat_id, kwargs))

    monkeypatch.setattr("core.lib.suggestion_extractor.extract_suggestions", _fake_plan)
    monkeypatch.setattr("core.actions.executor.execute_planned_actions", _fake_execute)

    handled, ancillary = await wf._resume_action_clarification(
        12345, "to the 19th", "thread-1", _make_workflow()
    )

    assert handled is True
    assert ancillary is None
    # The answer completes the original request
    assert "[User clarification:] to the 19th" in planned["seen_text"]
    assert executed and executed[0][1] == 12345
    # Workflow resolved
    update_args = fake_client.table("conversation_workflows").update.call_args
    assert update_args[0][0]["status"] == "resolved"
    # Learning loop: the resolution is persisted as confirmed
    assert observations and observations[0]["outcome"] == "confirmed"
    assert observations[0]["features"]["missing_fields"] == ["new_reminder_at"]


@pytest.mark.asyncio
async def test_resume_no_actions_closes_loop_honestly(monkeypatch):
    fake_client = MagicMock()
    monkeypatch.setattr(wf, "tenant_aware_client", lambda: fake_client)
    sent = []

    async def _fake_send(cid, msg):
        sent.append((cid, msg))

    observations = []

    async def _fake_emit(**kwargs):
        observations.append(kwargs)

    monkeypatch.setattr(wf, "send_telegram", _fake_send)
    monkeypatch.setattr(wf, "log_exchange", lambda *a, **k: None)
    monkeypatch.setattr("core.lib.telemetry.emit_observation", _fake_emit)

    async def _fake_plan(text, title="", entity="", active_anchor=None, intent=None):
        return [], None  # nothing resolvable

    monkeypatch.setattr("core.lib.suggestion_extractor.extract_suggestions", _fake_plan)

    handled, _ = await wf._resume_action_clarification(12345, "next week sometime", "thread-1", _make_workflow())

    assert handled is True
    assert "couldn't work that out" in sent[0][1]
    update_args = fake_client.table("conversation_workflows").update.call_args
    assert update_args[0][0]["status"] == "cancelled"
    # Learning loop: the failed resolution is persisted
    assert observations and observations[0]["outcome"] == "failed"
