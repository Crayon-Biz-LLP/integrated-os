import pytest
from unittest.mock import MagicMock, patch
from core.pulse.calendar import sync_completed_tasks_from_google
from datetime import datetime, timezone, timedelta

pytestmark = pytest.mark.sync

def mock_supabase_builder(data):
    mock = MagicMock()
    # Ensure any attribute access returns the same mock until execute() is called
    def return_self(*args, **kwargs):
        return mock
    
    mock.select = return_self
    mock.eq = return_self
    mock.not_ = mock
    mock.is_ = return_self
    mock.execute.return_value = MagicMock(data=data)
    
    return mock

@patch('core.pulse.calendar.context_provider')
def test_sync_completed_tasks_from_google_skips_recently_reopened(mock_ctx):
    mock_supabase = MagicMock()
    mock_tasks_service = MagicMock()

    task_updated_at = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    google_completed_at = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat().replace('+00:00', 'Z')
    
    data = [
        {
            'id': 123,
            'title': 'Test Task',
            'google_task_id': 'g123',
            'status': 'todo',
            'updated_at': task_updated_at
        }
    ]
    
    mock_supabase.table.return_value = mock_supabase_builder(data)

    mock_tasks_service.tasks.return_value.get.return_value.execute.return_value = {
        'status': 'completed',
        'completed': google_completed_at
    }

    completed = sync_completed_tasks_from_google(mock_supabase, mock_tasks_service)
    assert completed == []

@patch('core.pulse.calendar.context_provider')
def test_sync_completed_tasks_from_google_closes_if_google_is_newer(mock_ctx):
    mock_supabase = MagicMock()
    mock_tasks_service = MagicMock()

    task_updated_at = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    google_completed_at = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat().replace('+00:00', 'Z')
    
    data = [
        {
            'id': 123,
            'title': 'Test Task',
            'google_task_id': 'g123',
            'status': 'todo',
            'updated_at': task_updated_at
        }
    ]
    
    mock_supabase.table.return_value = mock_supabase_builder(data)

    mock_tasks_service.tasks.return_value.get.return_value.execute.return_value = {
        'status': 'completed',
        'completed': google_completed_at
    }

    completed = sync_completed_tasks_from_google(mock_supabase, mock_tasks_service)
    assert completed == [('Test Task', None)]

