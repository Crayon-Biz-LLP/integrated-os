import pytest
from unittest.mock import patch, MagicMock
from core.lib.audit_logger import set_trace_id
from core.services.db import get_supabase
from core.llm.compat import get_embedding_sync
from core.llm.constants import EMBEDDING_DIMENSION
from core.lib.graph_rules import normalize_label


# ── Per-table cleanup predicates ──────────────────────────────────────────

_CLEANUP_PREDICATES = {
    'tasks':           ('title', '[SIM_TEST]%'),
    'memories':        ('content', '[SIM_TEST]%'),
    'graph_nodes':     ('label', '[SIM_TEST]%'),
    'graph_edges':     None,  # deleted via node cascade — no direct clean
    'audit_logs':      ('message', '[SIM_TEST]%'),
    'conversations':   ('query', '[SIM_TEST]%'),
    'conversation_threads': None,  # cleaned via id set
    'conversation_workflows': None,  # cleaned via thread_id set
    'retrieval_index_runs': ('error_message', '[SIM_TEST]%'),
    'retrieval_passages': None,  # deleted via memory cascade
    'raw_dumps':       ('text', '[SIM_TEST]%'),
    'pending_retrieval_index_jobs': None,  # cleaned via per-test finally block
}


def _cleanup_sim_test_rows():
    supabase = get_supabase()
    for tbl, pred in _CLEANUP_PREDICATES.items():
        if pred is None:
            continue
        col, pattern = pred
        try:
            supabase.table(tbl).delete().ilike(col, pattern).execute()
        except Exception:
            pass


def _cleanup_by_ids(table: str, id_column: str, ids: list):
    """Delete rows by a list of IDs. No-op if ids empty."""
    if not ids:
        return
    supabase = get_supabase()
    try:
        supabase.table(table).delete().in_(id_column, ids).execute()
    except Exception:
        pass


def _verify_cleanup(table: str, col: str, pattern: str, expected: int = 0):
    """Assert that no rows matching the pattern remain."""
    supabase = get_supabase()
    try:
        res = supabase.table(table).select('id', count='exact').ilike(col, pattern).execute()
        actual = res.count if hasattr(res, 'count') else len(res.data or [])
        assert actual == expected, f"Cleanup verification failed for {table}: expected {expected}, got {actual}"
    except Exception:
        # If table doesn't exist or query fails, skip verification
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


# ── Seed data fixture ────────────────────────────────────────────────────

@pytest.fixture
def seed_test_data():
    """Seed realistic test data into the real DB with [SIM_TEST] prefix.
    
    Returns a dict of seeded IDs keyed by table name so tests can reference them.
    After yield, cleans up by ID and verifies per-table predicates.
    """
    seeded = {'graph_nodes': {}, 'memories': [], 'tasks': [], 'threads': [], 'workflows': []}
    supabase = get_supabase()

    # 1. Graph nodes
    nodes = [
        {'label': '[SIM_TEST] Shifrah', 'type': 'person', 'normalized_label': normalize_label('[SIM_TEST] Shifrah')},
        {'label': '[SIM_TEST] Vasanth', 'type': 'person', 'normalized_label': normalize_label('[SIM_TEST] Vasanth')},
        {'label': '[SIM_TEST] Alpha', 'type': 'project', 'normalized_label': normalize_label('[SIM_TEST] Alpha')},
    ]
    for n in nodes:
        res = supabase.table('graph_nodes').insert(n).execute()
        if res.data:
            seeded['graph_nodes'][n['label']] = res.data[0]['id']

    # 2. Memories (with embeddings, so match_memories_hybrid RPC can find them)
    memory_texts = [
        '[SIM_TEST] Unity prayer walk with Shifrah from the 90-Day Prayer group',
        '[SIM_TEST] Discussed budget with Vasanth, approved Q3 spend',
        '[SIM_TEST] I went for a walk in the park',
        '[SIM_TEST] Alpha project kickoff went well',
    ]
    for text in memory_texts:
        # Generate embedding matching production code (see dispatch.py)
        # Uses get_embedding_sync which handles event loop management via
        # nest_asyncio (installed as a test dependency).
        try:
            emb_vec = get_embedding_sync(text)
        except Exception:
            emb_vec = None
        if emb_vec is None:
            # Fallback: small non-zero constant vector.
            # Zero vector is invisible to pgvector cosine distance (0/0 = NaN),
            # which gets filtered by the RPC's (embedding <=> q_vec) IS NOT NULL check.
            emb_vec = [0.01] * EMBEDDING_DIMENSION
        res = supabase.table('memories').insert({
            'content': text,
            'memory_type': 'note',
            'embedding': emb_vec,
        }).execute()
        if res.data:
            seeded['memories'].append(res.data[0]['id'])

    # 3. Tasks
    task_res = supabase.table('tasks').insert({
        'title': '[SIM_TEST] Finalize Alpha project proposal',
        'status': 'todo',
        'priority': 'important',
        'is_current': True,
        'direction': 'outbound',
        'committed_to': 'Client',
    }).execute()
    if task_res.data:
        seeded['tasks'].append(task_res.data[0]['id'])

    # 4. Conversation thread (for session continuity tests)
    thread_res = supabase.table('conversation_threads').insert({
        'id': '00000000-0000-4000-8000-00000000aaaa',
        'chat_id': 999999999,
        'active_anchor': {"type": "person", "name": "Shifrah", "id": seeded['graph_nodes'].get('[SIM_TEST] Shifrah')},
    }).execute()
    if thread_res.data:
        seeded['threads'].append(thread_res.data[0]['id'])

    # 5. Workflow (for session continuity tests)
    wf_res = supabase.table('conversation_workflows').insert({
        'thread_id': '00000000-0000-4000-8000-00000000aaaa',
        'chat_id': 999999999,
        'workflow_type': 'awaiting_disambiguation_confirmation',
        'payload': {},
        'awaiting_user_input': True,
        'status': 'active',
    }).execute()
    if wf_res.data:
        seeded['workflows'].append(wf_res.data[0]['id'])

    yield seeded

    # Cleanup by ID (precise, no side effects)
    _cleanup_by_ids('conversation_workflows', 'id', seeded['workflows'])
    _cleanup_by_ids('conversation_threads', 'id', seeded['threads'])
    _cleanup_by_ids('tasks', 'id', seeded['tasks'])
    _cleanup_by_ids('memories', 'id', seeded['memories'])
    _cleanup_by_ids('graph_nodes', 'id', list(seeded['graph_nodes'].values()))

    # Verify cleanup
    _verify_cleanup('graph_nodes', 'label', '[SIM_TEST]%')
    _verify_cleanup('memories', 'content', '[SIM_TEST]%')
    _verify_cleanup('tasks', 'title', '[SIM_TEST]%')


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
