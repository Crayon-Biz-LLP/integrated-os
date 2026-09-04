"""Executor system notes route through the canonical note writer (Step 3).

Pins the invariant that the executor's three system notes — Guard 3 fallback,
closure history, and Guard B TASK-context — are written via create_note_direct
with the message's pre-computed EntityContext:

  - no raw `memories.insert` from the executor,
  - no second entity-extraction scan (extraction is read-only; a system note
    can never create pending nodes on its own),
  - provenance labels (intent/entity) preserved via extra_metadata.

Marker: decision (executor/actions pipeline).
"""

from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.actions.models import Action

pytestmark = pytest.mark.decision


class _FakeResp:
    def __init__(self, data):
        self.data = data


class _FakeSupabase:
    """Minimal chainable fake for the executor's task-read path."""

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
        "reminder_at": None,
    }


def _patch_note(mock_note):
    return patch("core.pulse.tools.create_note_direct", mock_note)


# ── Guard 3 fallback note ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_fallback_note_routes_through_create_note_direct():
    from core.actions.executor import _save_fallback_note

    note = AsyncMock(return_value={"action": "filed", "memory_id": 7001})
    with ExitStack() as stack:
        stack.enter_context(patch("core.actions.executor.tenant_aware_client", MagicMock()))
        stack.enter_context(patch("core.actions.executor.audit_log_sync", MagicMock()))
        stack.enter_context(_patch_note(note))
        ok = await _save_fallback_note("Unprocessable text here", 999, entity="Inbox", source="web")

    assert ok is True
    kwargs = note.await_args.kwargs
    assert kwargs["source"] == "web"
    assert kwargs["extra_metadata"] == {"intent": "NOTE", "entity": "Inbox"}


@pytest.mark.asyncio
async def test_fallback_note_passes_entity_context_not_second_scan():
    """The fallback note reuses the message's card-mode context — extraction is
    never re-run by the executor (no timing= async/sync call, no pending id)."""
    from core.actions.executor import _save_fallback_note
    from core.lib.entity_context import EntityContext

    ctx = EntityContext(source_text="x")
    note = AsyncMock(return_value={"action": "filed", "memory_id": 7002})
    extract = AsyncMock()
    with ExitStack() as stack:
        stack.enter_context(patch("core.actions.executor.tenant_aware_client", MagicMock()))
        stack.enter_context(patch("core.actions.executor.audit_log_sync", MagicMock()))
        stack.enter_context(_patch_note(note))
        # The underlying extraction module is patched as a tripwire: if the
        # executor re-scans, the mocked module would be imported and called.
        stack.enter_context(patch("core.lib.entity_context.extract_context_from_source", extract))
        await _save_fallback_note("Some text", 999, source="web", entity_context=ctx)

    assert note.await_args.kwargs["entity_context"] is ctx
    extract.assert_not_awaited()


@pytest.mark.asyncio
async def test_empty_actions_guard_passes_context_to_fallback():
    """Zero-action terminal wires entity_context through to the fallback note."""
    from core.actions.executor import execute_planned_actions

    fallback = AsyncMock(return_value=True)
    send = AsyncMock()
    with ExitStack() as stack:
        stack.enter_context(patch("core.actions.executor._save_fallback_note", fallback))
        stack.enter_context(patch("core.actions.executor.send_telegram", send))
        await execute_planned_actions([], chat_id=999, text="hi", source="web", intent="NOTE")

    assert fallback.await_args.kwargs["entity_context"] is None  # None when caller passes none
    assert fallback.await_args.args[3] == "web"


# ── Closure history note ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_closure_history_uses_create_note_direct_with_completion_label():
    from core.actions.executor import execute_planned_actions

    note = AsyncMock(return_value={"action": "filed", "memory_id": 8001})
    send = AsyncMock()
    actions = [Action(operation="close_task", target_id=2466, human_label="Purchase the Ashraya domain")]
    with ExitStack() as stack:
        stack.enter_context(patch("core.actions.executor.tenant_aware_client", return_value=_FakeSupabase(_task_row())))
        stack.enter_context(patch("core.actions.executor.audit_log_sync", MagicMock()))
        stack.enter_context(_patch_note(note))
        stack.enter_context(patch("core.actions.executor.send_telegram", send))
        stack.enter_context(patch("core.pulse.tools.update_task_status", return_value="OK: Task 2466 updated successfully."))
        stack.enter_context(patch("core.services.google_service.sync_to_calendar", return_value=None))
        stack.enter_context(patch("core.services.google_service.sync_to_google", return_value=None))
        await execute_planned_actions(
            actions, chat_id=999, text="Close the Ashraya domain purchase — done",
            source="web", intent="COMPLETION",
        )

    assert note.await_args is not None
    kwargs = note.await_args.kwargs
    assert kwargs["source"] == "webhook_completion"
    assert kwargs["extra_metadata"] == {"intent": "COMPLETION", "entity": None}


# ── Guard B TASK-context note ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_guard_b_uses_create_note_direct_with_task_context_label():
    from core.actions.executor import execute_planned_actions

    note = AsyncMock(return_value={"action": "filed", "memory_id": 9001})
    send = AsyncMock()
    fake_entities = [
        SimpleNamespace(type="person", label="Marcus"),
        SimpleNamespace(type="organization", label="Cobalt"),
    ]
    actions = [
        Action(operation="create_task", target_id=None, human_label="Follow up with Marcus",
               params={"title": "Follow up with Marcus", "reminder_at": None}),
    ]
    with ExitStack() as stack:
        stack.enter_context(patch("core.actions.executor.tenant_aware_client", return_value=_FakeSupabase(_task_row())))
        stack.enter_context(patch("core.actions.executor.audit_log_sync", MagicMock()))
        stack.enter_context(_patch_note(note))
        stack.enter_context(patch("core.actions.executor.send_telegram", send))
        stack.enter_context(patch("core.lib.entity_detector.detect_entities", return_value=fake_entities))
        stack.enter_context(patch("core.pulse.tools.create_task_direct", return_value={"action": "created", "task_id": 5001}))
        await execute_planned_actions(
            actions, chat_id=999, text="Follow up with Marcus on the Cobalt contract",
            source="web", intent="TASK",
        )

    assert note.await_args is not None
    kwargs = note.await_args.kwargs
    assert kwargs["extra_metadata"]["intent"] == "TASK_CONTEXT"
    assert kwargs["source"] == "web"