"""Unit tests for the task sync decision matrix and undo cleanup.

Covers:
1. Gating logic in `create_task_direct`:
   - No date -> No Google Task, No Calendar
   - Deadline only -> Google Task, No Calendar
   - Reminder date only -> Google Task, No Calendar
   - Reminder with time -> Google Task, Calendar Event
   - Recurrence -> Google Task, Calendar Event
2. Undo cleanup in `compensate_action`:
   - Fetch IDs and call delete functions.
"""
import pytest
from unittest.mock import MagicMock, patch, AsyncMock

from core.actions.models import Action
from core.actions.executor import compensate_action
from core.pulse.tools import create_task_direct

pytestmark = pytest.mark.sync

@pytest.fixture
def mock_supabase():
    with patch("core.pulse.tools.supabase") as mock_supa:
        # For create_task_direct
        mock_supa.table().select().eq().eq().not_.in_().execute.return_value = MagicMock(data=[])
        mock_supa.table().insert().execute.return_value = MagicMock(data=[{"id": 999}])
        mock_supa.table().update().eq().execute.return_value = MagicMock(data=[])
        
        # For compensate_action (passed as param)
        mock_exec_supa = AsyncMock() if hasattr(AsyncMock, "table") else MagicMock()
        mock_exec_supa.table().select().eq().limit().execute.return_value = MagicMock(
            data=[{"google_task_id": "gt_123", "google_event_id": "ge_456"}]
        )
        mock_exec_supa.table().update().eq().execute.return_value = MagicMock(data=[])
        
        yield mock_supa, mock_exec_supa


@pytest.fixture
def mock_google_sync():
    with patch("core.pulse.tools.sync_to_google") as m_gt, \
         patch("core.pulse.tools.sync_to_calendar") as m_gc, \
         patch("core.pulse.tools.get_tasks_service") as m_tasks_service, \
         patch("core.services.google_service.check_conflict") as m_conflict, \
         patch("core.lib.enrichment_queue.enqueue_enrichment"):
        
        m_gt.return_value = "gt_123"
        m_gc.return_value = "ge_456"
        m_tasks_service.return_value = MagicMock()
        m_conflict.return_value = None
        
        yield m_gt, m_gc


@pytest.fixture
def mock_google_delete():
    with patch("core.services.google_service.delete_google_task") as m_del_gt, \
         patch("core.services.google_service.delete_calendar_event") as m_del_gc:
        yield m_del_gt, m_del_gc


# --- 1. Gating Logic in create_task_direct ---

@pytest.mark.asyncio
async def test_no_date_no_sync(mock_supabase, mock_google_sync):
    m_gt, m_gc = mock_google_sync
    await create_task_direct(title="No date task", deadline=None, reminder_at=None)
    m_gt.assert_not_called()
    m_gc.assert_not_called()

@pytest.mark.asyncio
async def test_deadline_only_syncs_google_task(mock_supabase, mock_google_sync):
    m_gt, m_gc = mock_google_sync
    await create_task_direct(title="Deadline task", deadline="2026-09-01", reminder_at=None)
    m_gt.assert_called_once()
    m_gc.assert_not_called()
    assert m_gt.call_args[1]["due_at"] == "2026-09-01"

@pytest.mark.asyncio
async def test_reminder_date_only_syncs_google_task(mock_supabase, mock_google_sync):
    m_gt, m_gc = mock_google_sync
    await create_task_direct(title="Reminder task", deadline=None, reminder_at="2026-09-01")
    m_gt.assert_called_once()
    m_gc.assert_not_called()
    assert m_gt.call_args[1]["due_at"] == "2026-09-01"

@pytest.mark.asyncio
async def test_reminder_with_time_syncs_both(mock_supabase, mock_google_sync):
    m_gt, m_gc = mock_google_sync
    await create_task_direct(title="Reminder time task", deadline=None, reminder_at="2026-09-01T15:00:00Z")
    m_gt.assert_called_once()
    m_gc.assert_called_once()
    assert m_gt.call_args[1]["due_at"] == "2026-09-01T15:00:00Z"
    assert m_gt.call_args[1]["explicit_time"] is True

@pytest.mark.asyncio
async def test_recurrence_syncs_both(mock_supabase, mock_google_sync):
    m_gt, m_gc = mock_google_sync
    # recurrence implies a calendar event even if explicit_time check is loose,
    # but let's test it with explicit time + recurrence
    await create_task_direct(title="Recurring task", deadline=None, reminder_at="2026-09-01T09:00:00Z", recurrence="FREQ=WEEKLY")
    m_gt.assert_called_once()
    m_gc.assert_called_once()

# --- 2. Undo Cleanup in compensate_action ---

@pytest.mark.asyncio
async def test_undo_cleans_up_google_and_calendar(mock_supabase, mock_google_delete):
    _, mock_exec_supa = mock_supabase
    m_del_gt, m_del_gc = mock_google_delete
    
    action = Action(operation="create_task", params={"_created_task_id": 999})
    await compensate_action(action, mock_exec_supa)
    
    m_del_gt.assert_called_once_with("gt_123")
    m_del_gc.assert_called_once_with("ge_456")
    # Soft delete
    mock_exec_supa.table().update().eq().execute.assert_called()

@pytest.mark.asyncio
async def test_undo_no_google_ids(mock_supabase, mock_google_delete):
    _, mock_exec_supa = mock_supabase
    m_del_gt, m_del_gc = mock_google_delete
    
    # Empty DB return
    mock_exec_supa.table().select().eq().limit().execute.return_value = MagicMock(
        data=[{"google_task_id": None, "google_event_id": None}]
    )
    
    action = Action(operation="create_task", params={"_created_task_id": 999})
    await compensate_action(action, mock_exec_supa)
    
    m_del_gt.assert_not_called()
    m_del_gc.assert_not_called()
    # Soft delete still happens
    mock_exec_supa.table().update().eq().execute.assert_called()
