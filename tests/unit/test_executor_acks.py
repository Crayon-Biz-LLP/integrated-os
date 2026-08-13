"""Per-operation Telegram acks in the executor (no DB / LLM required).

Regression for the "✅ Closed" mislabel: every successful mutation used to be
acknowledged as a closure, so a reschedule read "✅ Closed: <task>" while the
task stayed open. A reschedule must read "✅ Rescheduled … → <date>", closures
must keep "✅ Closed", and mixed batches must produce one line per operation.

Run: python -m pytest tests/unit/test_executor_acks.py -v
"""

from contextlib import ExitStack
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.actions.models import Action

RESCHEDULE_AT = "2026-08-20T12:40:28+05:30"


class _FakeResp:
    def __init__(self, data):
        self.data = data


class _FakeSupabase:
    """Minimal chainable fake for the executor's task-read / ack paths."""

    def __init__(self, task_row=None):
        self._task_row = task_row
        self._name = None

    def table(self, name):
        self._name = name
        return self

    def select(self, cols):
        return self

    def update(self, data):
        return self

    def eq(self, col, val):
        return self

    def limit(self, n):
        return self

    def in_(self, col, vals):
        return self

    def execute(self):
        if self._name == "tasks" and self._task_row is not None:
            return _FakeResp([self._task_row])
        return _FakeResp([])


def _task_row(task_id=2466, title="Purchase the Ashraya domain", status="todo"):
    return {
        "id": task_id,
        "title": title,
        "status": status,
        "google_task_id": None,
        "google_event_id": None,
        "duration_mins": 15,
        "reminder_at": "2026-08-01T10:00:00+05:30",
    }


def _reschedule(task_id=2466, label="Purchase the Ashraya domain"):
    return Action(
        operation="reschedule",
        target_id=task_id,
        human_label=label,
        params={"new_reminder_at": RESCHEDULE_AT},
    )


def _close(task_id=2467, label="Reply to the client"):
    return Action(operation="close_task", target_id=task_id, human_label=label)


def _update_metadata(task_id=2468, label="Renew the lease"):
    return Action(
        operation="update_metadata",
        target_id=task_id,
        human_label=label,
        params={"new_priority": "urgent"},
    )


def _modify_recurring(task_id=2469, label="Weekly sync"):
    return Action(
        operation="modify_recurring",
        target_id=task_id,
        human_label=label,
        params={"new_rrule": "FREQ=WEEKLY;BYDAY=MO"},
    )


async def _run(actions, task_row, extra_patches=()):
    """Execute planned actions with all external side effects mocked.

    Returns the AsyncMock `send_telegram` so the test can assert on acks.
    `text=""` skips the Stage-1 memory dump; `intent=None` skips NOTE
    interception and Guard B — the mutation path stays fully exercised.
    """
    send = AsyncMock()
    supabase = _FakeSupabase(task_row)
    patchers = [
        patch("core.actions.executor.send_telegram", send),
        patch("core.actions.executor.tenant_aware_client", return_value=supabase),
        patch("core.actions.executor.audit_log_sync", MagicMock()),
        patch("core.services.google_service.sync_to_calendar", return_value=None),
        patch("core.services.google_service.sync_to_google", return_value=None),
    ]
    patchers.extend(extra_patches)
    with ExitStack() as stack:
        for p in patchers:
            stack.enter_context(p)
        from core.actions.executor import execute_planned_actions

        await execute_planned_actions(
            actions, chat_id=999, text="", source="telegram", session_id=None
        )
        return send


def _last_message(send: AsyncMock) -> str:
    assert send.await_args is not None, "send_telegram was never called"
    return send.await_args.args[1]


def _last_kwargs(send: AsyncMock) -> dict:
    assert send.await_args is not None, "send_telegram was never called"
    return dict(send.await_args.kwargs or {})


# ── reschedule: voice, honest, never Closed ────────────────────────────────


@pytest.mark.asyncio
async def test_reschedule_ack_voice_with_date():
    send = await _run([_reschedule()], _task_row())
    msg = _last_message(send)
    assert msg == "Moved Purchase the Ashraya domain to Aug 20, 2026."
    assert "Closed" not in msg


@pytest.mark.asyncio
async def test_reschedule_ack_carries_structured_meta():
    send = await _run([_reschedule()], _task_row())
    kw = _last_kwargs(send)
    assert kw["intent"] == "TASK_RESCHEDULED"
    assert kw["ack_title"] == "Purchase the Ashraya domain"


@pytest.mark.asyncio
async def test_reschedule_without_label_uses_db_title():
    send = await _run([_reschedule(label="")], _task_row())
    assert "Purchase the Ashraya domain" in _last_message(send)


# ── closures: keep the existing ack ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_close_ack_voice_done():
    send = await _run(
        [_close()],
        _task_row(task_id=2467, title="Reply to the client"),
        extra_patches=[
            patch(
                "core.pulse.tools.update_task_status",
                return_value="Task closed",
            ),
        ],
    )
    assert _last_message(send) == "Done — Reply to the client is off your plate."
    assert _last_kwargs(send)["intent"] == "TASK_CLOSED"


# ── other mutations ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_update_metadata_ack():
    send = await _run([_update_metadata()], _task_row(task_id=2468))
    assert _last_message(send) == "Updated Renew the lease's priority."


@pytest.mark.asyncio
async def test_modify_recurring_ack():
    send = await _run([_modify_recurring()], _task_row(task_id=2469))
    assert _last_message(send) == "Updated Weekly sync's schedule."


@pytest.mark.asyncio
async def test_mixed_batch_emits_one_line_per_operation():
    # A close + a reschedule in one message must produce two honest lines,
    # not a single "✅ Closed: A, B".
    send = await _run(
        [_close(), _reschedule()],
        _task_row(task_id=2467, title="Reply to the client"),
        extra_patches=[
            patch(
                "core.pulse.tools.update_task_status",
                return_value="Task closed",
            ),
        ],
    )
    msg = _last_message(send)
    assert "Moved Purchase the Ashraya domain to Aug 20, 2026." in msg
    assert "Done — Reply to the client is off your plate." in msg
    assert msg.index("Moved") < msg.index("Done")


# ── creations: per-op acks (a task is not "Logged", an event is not "Logged") ──


def _create_task(label="File the Q3 report"):
    return Action(
        operation="create_task",
        target_id=None,
        human_label=label,
        params={"title": label, "reminder_at": RESCHEDULE_AT},
    )


def _create_note(label="Meeting notes from today"):
    return Action(operation="create_note", target_id=None, human_label=label, params={})


def _create_event(label="Team standup"):
    return Action(
        operation="create_event",
        target_id=None,
        human_label=label,
        params={"title": label, "time": RESCHEDULE_AT},
    )


@pytest.mark.asyncio
async def test_create_task_ack_is_on_your_list_not_logged():
    send = await _run(
        [_create_task()],
        _task_row(),
        extra_patches=[
            patch(
                "core.pulse.tools.create_task_direct",
                return_value={"action": "created", "task_id": 5001},
            ),
        ],
    )
    msg = _last_message(send)
    assert msg == "Got it — File the Q3 report is on your list for Aug 20, 2026."
    assert "Logged" not in msg
    assert _last_kwargs(send)["intent"] == "TASK_CREATED"


@pytest.mark.asyncio
async def test_create_note_ack_is_logged():
    send = await _run(
        [_create_note()],
        _task_row(),
        extra_patches=[
            patch(
                "core.pulse.tools.create_note_direct",
                return_value={"action": "filed", "memory_id": 9001},
            ),
        ],
    )
    assert _last_message(send) == "Meeting notes from today — logged."
    assert _last_kwargs(send)["intent"] == "NOTE_LOGGED"


@pytest.mark.asyncio
async def test_create_event_ack_is_scheduled():
    send = await _run(
        [_create_event()],
        _task_row(),
        extra_patches=[
            patch(
                "core.pulse.tools.create_task_direct",
                return_value={"action": "created", "task_id": 5002},
            ),
        ],
    )
    msg = _last_message(send)
    assert msg == "Added Team standup to your calendar for Aug 20, 2026."
    assert "Logged" not in msg
    assert _last_kwargs(send)["intent"] == "EVENT_SCHEDULED"


# ── render_acks verb table (pure, no executor) ─────────────────────────────


def _result(op, status="committed", title="X", target_id=1, **values):
    from core.lib.rhodey_voice import ExecutionResult
    return ExecutionResult(
        operation=op, status=status, target_id=target_id, title=title, values=values
    )


def test_render_acks_skips_failed_and_rolled_back():
    from core.lib.rhodey_voice import render_acks
    lines = render_acks([
        _result("reschedule", status="failed", title="Doomed", target_id=1),
        _result("reschedule", status="rolled_back", title="Reverted", target_id=2),
    ])
    assert lines == []


def test_render_acks_groups_closures():
    from core.lib.rhodey_voice import render_acks
    lines = render_acks([
        _result("close_task", title="A", target_id=1),
        _result("cancel_recurring", title="B", target_id=2),
    ])
    assert lines == [
        "Cancelled — B won't repeat anymore.",
        "Done — A is off your plate.",
    ]


def test_render_acks_orders_creations_mutations_closures():
    from core.lib.rhodey_voice import render_acks
    lines = render_acks([
        _result("close_task", title="C", target_id=3),
        _result("reschedule", title="R", target_id=4, new_reminder_at=RESCHEDULE_AT),
        _result("create_note", title="N", target_id=5),
    ])
    assert lines == [
        "N — logged.",
        "Moved R to Aug 20, 2026.",
        "Done — C is off your plate.",
    ]
