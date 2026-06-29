import pytest
from unittest.mock import patch, MagicMock
from core.lib.audit_logger import set_trace_id
from core.services.db import get_supabase


def _cleanup_sim_test_rows():
    supabase = get_supabase()
    tables = ['tasks', 'memories', 'raw_dumps', 'graph_nodes', 'graph_edges',
              'conversation_threads', 'conversation_workflows', 'conversations',
              'audit_logs', 'retrieval_index_runs']
    for tbl in tables:
        try:
            supabase.table(tbl).delete().ilike('title' if tbl == 'tasks' else 'content',
                                               '[SIM_TEST]%').execute()
        except Exception:
            try:
                supabase.table(tbl).delete().ilike('message', '[SIM_TEST]%').execute()
            except Exception:
                pass


def _cleanup_orphan_retrieval():
    supabase = get_supabase()
    try:
        passages = supabase.table('retrieval_passages') \
            .select('id, memory_id') \
            .not_.is_('memory_id', 'null') \
            .execute()
        if passages.data:
            mem_ids = list(set(p['memory_id'] for p in passages.data if p.get('memory_id')))
            if mem_ids:
                existing = supabase.table('memories') \
                    .select('id') \
                    .in_('id', mem_ids) \
                    .execute()
                existing_ids = {e['id'] for e in (existing.data or [])}
                for p in passages.data:
                    if p['memory_id'] and p['memory_id'] not in existing_ids:
                        from core.retrieval.cleanup import cleanup_memory_retrieval_index
                        cleanup_memory_retrieval_index(p['memory_id'])
    except Exception:
        pass


@pytest.fixture(autouse=True)
def sim_cleanup():
    yield
    _cleanup_sim_test_rows()
    _cleanup_orphan_retrieval()


@pytest.fixture
def mock_llm():
    with patch('core.llm.fallback.generate_content_with_fallback') as mock_gen, \
         patch('core.llm.compat.call_llm_with_fallback_sync') as mock_sync:
        mock_response = MagicMock()
        mock_response.text = "mock response"
        mock_response.parse_json.return_value = {"intent": "NOTE", "confidence": 1.0}
        mock_gen.return_value = mock_response
        mock_sync.return_value = mock_response
        yield {'generate': mock_gen, 'sync': mock_sync}


@pytest.fixture
def mock_telegram():
    with patch('core.webhook.telegram.send_telegram') as mock_send:
        mock_send.return_value = None
        yield mock_send


@pytest.fixture
def mock_google():
    with patch('core.pulse.tools.sync_to_calendar') as mock_cal, \
         patch('core.pulse.tools.delete_calendar_event') as mock_del_cal, \
         patch('core.services.google_service.get_cached_service') as mock_gs:
        mock_cal.return_value = "mock_event_id"
        mock_del_cal.return_value = None
        mock_service = MagicMock()
        mock_events = MagicMock()
        mock_service.events.return_value = mock_events
        mock_gs.return_value = mock_service
        yield {
            'sync_to_calendar': mock_cal,
            'delete_calendar_event': mock_del_cal,
            'service': mock_service,
            'events': mock_events
        }


@pytest.fixture
def trace_id():
    tid = set_trace_id("sim-test-trace")
    yield tid
    set_trace_id(None)
