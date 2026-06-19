import pytest
from unittest.mock import patch, MagicMock, AsyncMock
from core.pulse.memory import write_outcome_memory

class TestMemoryWiring:
    @pytest.mark.asyncio
    @patch("core.pulse.memory.get_embedding", new_callable=AsyncMock)
    @patch("core.pulse.memory.supabase")
    @patch("core.retrieval.pipeline.schedule_index_memory")
    async def test_write_outcome_memory_enqueues_indexing(
        self, mock_schedule, mock_supabase, mock_get_embedding
    ):
        # 1. Setup mocks
        mock_get_embedding.return_value.vector = [0.1, 0.2, 0.3]
        
        # Mock Supabase insert result
        mock_result = MagicMock()
        mock_result.data = [{"id": 123}]
        mock_supabase.table.return_value.insert.return_value.execute.return_value = mock_result

        # 2. Execute
        await write_outcome_memory(task_title="Test Task", project_name="Test Project")

        # 3. Verify
        # Ensure insert was called
        mock_supabase.table.assert_called_with('memories')
        
        # Ensure schedule_index_memory was called with the correct ID and metadata
        mock_schedule.assert_called_once_with(
            123, 
            "Completed: Test Task on Test Project", 
            "outcome", 
            "pulse_outcome"
        )
