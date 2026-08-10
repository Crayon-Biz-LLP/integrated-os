import pytest
from unittest.mock import patch, MagicMock
from core.lib.audit_logger import set_trace_id
from core.services.db import tenant_scope
from tests.fixtures.test_tenant import fresh_supabase, resolve_test_tenant_uid
from core.llm.compat import get_embedding_sync
from core.llm.constants import EMBEDDING_DIMENSION
from core.lib.graph_rules import normalize_label


# ── Live-Supabase integration guard ───────────────────────────────────────
# tests/sim is a DB-backed behavioral suite: it inserts [SIM_TEST] rows into
# the Supabase project named by SUPABASE_URL and runs real pipeline code
# against it. When that project is unreachable (CI sandbox, no network), the
# DB-backed tests are skipped — they are integration tests, not unit tests.
def _live_db_reachable() -> bool:
    """Faithful probe: can the supabase-py client actually talk to this host?

    This runs the REAL client through the REAL path the tests use — a
    read-only select against the users table — and requires a REAL
    PostgREST response shape. Two failure modes are covered:

    1. Unreachable host → httpx raises → False.
    2. Mocked client (some test suites replace get_supabase() with a
       MagicMock): a mock chain never raises, so a bare try/except would
       wrongly report "reachable". We therefore require the response's
       .data to be a real list (or None) — a MagicMock's .data is itself a
       MagicMock, which fails the isinstance check → False.
    """
    try:
        res = fresh_supabase().table("users").select("id").limit(1).execute()
        # Real PostgREST responses: .data is a list of rows, or None on
        # zero rows. Any other type (MagicMock, etc.) is not a live DB.
        return isinstance(res.data, (list, type(None)))
    except Exception:
        return False
# once at module load; when the DB is reachable but no test tenant exists,
# the suite skips rather than falling back to the channel tenant (Danny) —
# that fallback is exactly the cross-tenant leak M3 was built to prevent.
TEST_TENANT_UID = resolve_test_tenant_uid()

requires_live_db = pytest.mark.skipif(
    not (_live_db_reachable() and TEST_TENANT_UID),
    reason="live Supabase / test tenant unavailable — integration test",
)


@pytest.fixture(autouse=True)
def _test_tenant_scope():
    """Run every sim test inside the TEST TENANT's owner scope.

    The pipeline code under test binds tenant_aware_client(), which injects
    owner_id from the current tenant context. Without this scope it would
    resolve the channel tenant (oldest active user = Danny) and write sim
    rows into HIS tenant. When no test tenant is resolvable but the DB is
    reachable, skip instead of running unscoped.
    """
    uid = TEST_TENANT_UID
    if uid:
        with tenant_scope(uid):
            yield
    elif _live_db_reachable():
        pytest.skip("test tenant unresolvable — refusing unscoped run")
    else:
        yield  # DB unreachable: pure-logic tests may still run


# ── Module-level cleanup: sweep stale [SIM_TEST] rows before any test ──
# Must respect FK order: tasks/projects first → organizations/graph_nodes last.

_SWEEP_ORDER = [
    ('org_creation_signals', 'org_name', 'org_creation_signals'),
    ('tasks', 'title', 'tasks'),
    ('memories', 'content', 'memories'),
    ('projects', 'name', 'projects'),
    ('graph_nodes', 'label', 'graph_nodes'),
    ('resources', 'url', 'resources'),
    ('raw_dumps', 'content', 'raw_dumps'),
    ('audit_logs', 'message', 'audit_logs'),
    ('conversations', 'content', 'conversations'),
]

def _delete_ilike(table, col, pattern):
    """Delete pattern-matched rows owned by the TEST TENANT only.

    The owner_id filter is the leak guard: no matter what the ilike pattern
    matches, rows belonging to any other tenant are never touched.
    """
    supabase = fresh_supabase()
    try:
        supabase.table(table).delete().eq('owner_id', TEST_TENANT_UID).ilike(col, pattern).execute()
    except Exception:
        pass

def _delete_fk_orphans(table, fk_col, parent_table, parent_name_col, parent_pattern):
    """Delete child rows whose parent matches a NAME pattern (owner-scoped).

    Finds parent rows by name (e.g. organizations/projects named
    '[SIM_TEST] …'), then deletes test-tenant children referencing them via
    the FK column. The owner_id filter on the child delete is the leak guard.
    """
    supabase = fresh_supabase()
    try:
        parents = supabase.table(parent_table).select('id').ilike(parent_name_col, parent_pattern).execute()
        if parents.data:
            ids = [p['id'] for p in parents.data]
            supabase.table(table).delete().eq('owner_id', TEST_TENANT_UID).in_(fk_col, ids).execute()
    except Exception:
        pass

def _sweep_sim_test_rows():
    if not TEST_TENANT_UID:
        return  # nothing resolvable → nothing to sweep (suite will skip anyway)
    # FK orphans first (may not have [SIM_TEST] in their own title/content).
    # The organizations mirror was dropped by migration 75 — those calls
    # silently no-op (table missing), projects is the live parent.
    _delete_fk_orphans('tasks', 'organization_id', 'organizations', 'name', '[SIM_TEST]%')
    _delete_fk_orphans('tasks', 'project_id', 'projects', 'name', '[SIM_TEST]%')
    _delete_fk_orphans('projects', 'organization_id', 'organizations', 'name', '[SIM_TEST]%')
    # Then direct ilike sweep in FK-safe order
    for tbl, col, _ in _SWEEP_ORDER:
        _delete_ilike(tbl, col, '[SIM_TEST]%')

_sweep_sim_test_rows()

# ── Per-table cleanup predicates ──────────────────────────────────────────

_CLEANUP_PREDICATES = {
    'org_creation_signals': ('org_name', '[SIM_TEST]%'),
    'tasks':                   ('title', '[SIM_TEST]%'),
    'memories':                ('content', '[SIM_TEST]%'),
    'graph_nodes':             ('label', '[SIM_TEST]%'),
    'graph_edges':             None,  # deleted via node cascade — no direct clean
    'audit_logs':              ('message', '[SIM_TEST]%'),
    'conversations':           ('content', '[SIM_TEST]%'),
    'conversation_threads':    None,  # cleaned via id set
    'conversation_workflows':  None,  # cleaned via thread_id set
    'retrieval_index_runs':    ('error', '[SIM_TEST]%'),
    'retrieval_passages':      None,  # deleted via memory cascade
    'raw_dumps':               ('content', '[SIM_TEST]%'),
    'pending_retrieval_index_jobs': None,  # cleaned via per-test finally block
    'organizations':           ('name', '[SIM_TEST]%'),
    'projects':                ('name', '[SIM_TEST]%'),
    'resources':               ('url', '%[SIM_TEST]%'),
    'pending_graph_edges':     None,  # cleaned via node cascade
}


def _cleanup_sim_test_rows():
    # FK orphans first (tasks/projects with titles that don't start with [SIM_TEST])
    _delete_fk_orphans('tasks', 'organization_id', 'organizations', 'id', '[SIM_TEST]%')
    _delete_fk_orphans('tasks', 'project_id', 'projects', 'id', '[SIM_TEST]%')
    _delete_fk_orphans('projects', 'organization_id', 'organizations', 'id', '[SIM_TEST]%')
    # Then direct ilike sweep
    for tbl, pred in _CLEANUP_PREDICATES.items():
        if pred is None:
            continue
        col, pattern = pred
        _delete_ilike(tbl, col, pattern)


def _cleanup_by_ids(table: str, id_column: str, ids: list):
    """Delete rows by a list of IDs within the TEST TENANT. No-op if ids empty."""
    if not ids:
        return
    supabase = fresh_supabase()
    try:
        supabase.table(table).delete().eq('owner_id', TEST_TENANT_UID).in_(id_column, ids).execute()
    except Exception:
        pass


def _verify_cleanup(table: str, col: str, pattern: str, expected: int = 0):
    """Assert that no TEST-TENANT rows matching the pattern remain."""
    supabase = fresh_supabase()
    try:
        res = supabase.table(table).select('id', count='exact').eq('owner_id', TEST_TENANT_UID).ilike(col, pattern).execute()
        actual = res.count if hasattr(res, 'count') else len(res.data or [])
        assert actual == expected, f"Cleanup verification failed for {table}: expected {expected}, got {actual}"
    except Exception:
        # If table doesn't exist or query fails, skip verification
        pass


def _cleanup_orphan_retrieval():
    supabase = fresh_supabase()
    try:
        # Owner-scoped: only sweep the TEST TENANT's own passages. A global
        # sweep would delete another tenant's orphaned rows — the exact
        # cross-tenant write the M3 tenant wall exists to prevent.
        passages = supabase.table('retrieval_passages') \
            .select('id, memory_id') \
            .eq('owner_id', TEST_TENANT_UID) \
            .not_.is_('memory_id', 'null') \
            .execute()
        if passages.data:
            mem_ids = list(set(p['memory_id'] for p in passages.data if p.get('memory_id')))
            if mem_ids:
                existing = supabase.table('memories') \
                    .select('id') \
                    .eq('owner_id', TEST_TENANT_UID) \
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
    # Migration 75 removed the organizations mirror table — the org-routing
    # sim suite seeds against the old schema and is obsolete as-is.
    try:
        supabase = fresh_supabase()
        supabase.table("organizations").select("id").limit(1).execute()
    except Exception:
        import pytest
        pytest.skip("migration 75 removed the organizations table — sim suite targets old schema")
    seeded = {'graph_nodes': {}, 'memories': [], 'tasks': [], 'threads': [], 'workflows': []}
    supabase = fresh_supabase()

    # 1. Graph nodes — every row explicitly owned by the TEST TENANT so a
    # scope slip can never attribute sim data to another tenant.
    nodes = [
        {'label': '[SIM_TEST] Shifrah', 'type': 'person', 'normalized_label': normalize_label('[SIM_TEST] Shifrah'), 'owner_id': TEST_TENANT_UID},
        {'label': '[SIM_TEST] Vasanth', 'type': 'person', 'normalized_label': normalize_label('[SIM_TEST] Vasanth'), 'owner_id': TEST_TENANT_UID},
        {'label': '[SIM_TEST] Alpha', 'type': 'project', 'normalized_label': normalize_label('[SIM_TEST] Alpha'), 'owner_id': TEST_TENANT_UID},
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
            'owner_id': TEST_TENANT_UID,
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
        'owner_id': TEST_TENANT_UID,
    }).execute()
    if task_res.data:
        seeded['tasks'].append(task_res.data[0]['id'])

    # 4. Conversation thread (for session continuity tests)
    thread_res = supabase.table('conversation_threads').insert({
        'id': '00000000-0000-4000-8000-00000000aaaa',
        'chat_id': 999999999,
        'active_anchor': {"type": "person", "name": "Shifrah", "id": seeded['graph_nodes'].get('[SIM_TEST] Shifrah')},
        'owner_id': TEST_TENANT_UID,
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
        'owner_id': TEST_TENANT_UID,
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


# ── Full pipeline seed data ────────────────────────────────────────────

@pytest.fixture
def seed_full_test_data():
    """Seed richer test data for the full pipeline simulation test.

    Creates real DB rows with [SIM_TEST] prefix across organizations,
    projects, graph_nodes, tasks, and memories. Used by test_full_pipeline.py.
    After yield, cleans up by ID.
    """
    seeded = {
        'orgs': {}, 'projects': {}, 'graph_nodes': {},
        'tasks': [], 'memories': [],
        # Test-created IDs — appended by each test; cleaned up in teardown
        '_created_tasks': [],
        '_created_memories': [],
    }
    supabase = fresh_supabase()

    # Sweep any stale [SIM_TEST] rows before seeding to avoid duplicate key errors
    for tbl, col in [('projects', 'name'),
                     ('graph_nodes', 'label'), ('memories', 'content'),
                     ('raw_dumps', 'text'), ('tasks', 'title')]:
        try:
            supabase.table(tbl).delete().eq('owner_id', TEST_TENANT_UID).ilike(col, '[SIM_TEST]%').execute()
        except Exception:
            pass

    orgs_data = [
        '[SIM_TEST] Crayon Biz LLP',
        '[SIM_TEST] Equisoft',
    ]
    for name in orgs_data:
        res = supabase.table('graph_nodes').insert({
            'label': name,
            'type': 'organization',
            'normalized_label': normalize_label(name),
            'owner_id': TEST_TENANT_UID,
        }).execute()
        if res.data:
            seeded['orgs'][name] = res.data[0]['id']

    projects_data = [
        {'name': '[SIM_TEST] Qhord', 'context': '', 'organization_id': seeded['orgs'].get('[SIM_TEST] Crayon Biz LLP'), 'status': 'active', 'owner_id': TEST_TENANT_UID},
        {'name': '[SIM_TEST] Ashraya', 'context': '', 'status': 'active', 'owner_id': TEST_TENANT_UID},
        {'name': '[SIM_TEST] IAM Recertification', 'context': '', 'organization_id': seeded['orgs'].get('[SIM_TEST] Equisoft'), 'status': 'active', 'owner_id': TEST_TENANT_UID},
    ]
    for p in projects_data:
        res = supabase.table('projects').insert(p).execute()
        if res.data:
            seeded['projects'][p['name']] = res.data[0]['id']

    nodes = [
        {'label': '[SIM_TEST] Danny', 'type': 'person', 'normalized_label': normalize_label('[SIM_TEST] Danny'), 'owner_id': TEST_TENANT_UID},
        {'label': '[SIM_TEST] Shifrah', 'type': 'person', 'normalized_label': normalize_label('[SIM_TEST] Shifrah'), 'owner_id': TEST_TENANT_UID},
        {'label': '[SIM_TEST] Marcus', 'type': 'person', 'normalized_label': normalize_label('[SIM_TEST] Marcus'), 'owner_id': TEST_TENANT_UID},
        {'label': '[SIM_TEST] Qhord', 'type': 'project', 'normalized_label': normalize_label('[SIM_TEST] Qhord'), 'owner_id': TEST_TENANT_UID},
        {'label': '[SIM_TEST] Ashraya', 'type': 'project', 'normalized_label': normalize_label('[SIM_TEST] Ashraya'), 'owner_id': TEST_TENANT_UID},
    ]
    for n in nodes:
        res = supabase.table('graph_nodes').insert(n).execute()
        if res.data:
            seeded['graph_nodes'][n['label']] = res.data[0]['id']

    try:
        emb_vec = get_embedding_sync('[SIM_TEST] Discussed Qhord launch plan')
    except Exception:
        emb_vec = [0.01] * EMBEDDING_DIMENSION
    mem_res = supabase.table('memories').insert({
        'content': '[SIM_TEST] Discussed Qhord launch plan',
        'memory_type': 'note',
        'embedding': emb_vec,
        'owner_id': TEST_TENANT_UID,
    }).execute()
    if mem_res.data:
        seeded['memories'].append(mem_res.data[0]['id'])

    task_res = supabase.table('tasks').insert({
        'title': '[SIM_TEST] Buy groceries',
        'status': 'todo',
        'priority': 'normal',
        'is_current': True,
        'direction': 'inbound',
        'owner_id': TEST_TENANT_UID,
    }).execute()
    if task_res.data:
        seeded['tasks'].append(task_res.data[0]['id'])

    yield seeded

    _cleanup_by_ids('tasks', 'id', seeded['tasks'] + seeded['_created_tasks'])
    _cleanup_by_ids('memories', 'id', seeded['memories'] + seeded['_created_memories'])
    _cleanup_by_ids('graph_nodes', 'id', list(seeded['graph_nodes'].values()) + list(seeded['orgs'].values()))
    _cleanup_by_ids('projects', 'id', list(seeded['projects'].values()))

    _verify_cleanup('projects', 'name', '[SIM_TEST]%')
    _verify_cleanup('graph_nodes', 'label', '[SIM_TEST]%')
    _verify_cleanup('tasks', 'title', '[SIM_TEST]%')
    _verify_cleanup('memories', 'content', '[SIM_TEST]%')


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
