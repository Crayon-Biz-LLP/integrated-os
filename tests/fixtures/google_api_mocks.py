import pytest
from unittest.mock import patch, MagicMock

@pytest.fixture
def mock_google_apis():
    with patch("core.pulse.tools.sync_to_calendar") as mock_cal, \
         patch("core.pulse.tools.sync_to_google") as mock_tasks, \
         patch("core.pulse.tools.delete_calendar_event") as mock_del_cal, \
         patch("core.services.google_service.get_cached_service") as mock_get_service:
        
        # Default successful behaviors
        mock_cal.return_value = "mock_google_event_id_123"
        mock_tasks.return_value = None
        mock_del_cal.return_value = None
        
        # Build a deep mock for Google API service
        mock_service = MagicMock()
        mock_events = MagicMock()
        mock_service.events.return_value = mock_events
        mock_get_service.return_value = mock_service
        
        yield {
            "sync_to_calendar": mock_cal,
            "sync_to_google": mock_tasks,
            "delete_calendar_event": mock_del_cal,
            "service": mock_service,
            "events": mock_events
        }
