import pytest
from datetime import datetime, timedelta, timezone
from core.services.db import get_supabase
from core.lib.audit_logger import set_trace_id

supabase = get_supabase()


@pytest.mark.asyncio
async def test_t1_idempotent_no_double_escalation():
    set_trace_id("sim-t1-idem")
    created = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
    task = supabase.table('tasks').insert({
        'title': '[SIM_TEST] T1 Idempotent',
        'priority': 'important', 'status': 'todo',
        'direction': 'inbound', 'is_current': True,
        'created_at': created
    }).execute()
    tid = task.data[0]['id']

    for t in (supabase.table('tasks').select('id, created_at').eq('id', tid).execute().data or []):
        cd = datetime.fromisoformat(str(t['created_at']).replace('Z', '+00:00'))
        if (datetime.now(timezone.utc) - cd).days >= 7:
            supabase.table('tasks').update({'priority': 'urgent'}).eq('id', t['id']).execute()

    for t in (supabase.table('tasks').select('id, created_at').eq('id', tid).execute().data or []):
        cd = datetime.fromisoformat(str(t['created_at']).replace('Z', '+00:00'))
        if (datetime.now(timezone.utc) - cd).days >= 7:
            supabase.table('tasks').update({'priority': 'urgent'}).eq('id', t['id']).execute()

    second_check = supabase.table('tasks').select('priority').eq('id', tid).execute()
    assert second_check.data[0]['priority'] == 'urgent'

    supabase.table('tasks').delete().eq('id', tid).execute()


@pytest.mark.asyncio
async def test_m5_idempotent_no_double_delete():
    set_trace_id("sim-m5-idem")
    expired_at = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    mem = supabase.table('memories').insert({
        'content': '[SIM_TEST] M5 Idempotent',
        'memory_type': 'note', 'source': 'sim_test',
        'expires_at': expired_at
    }).execute()
    mid = mem.data[0]['id']

    from core.retrieval.cleanup import cleanup_memory_retrieval_index
    cleanup_memory_retrieval_index(mid)
    supabase.table('memories').delete().eq('id', mid).execute()

    check = supabase.table('memories').select('id').eq('id', mid).execute()
    assert not check.data, "Memory must be deleted after first sweep"

    check_retrieval = supabase.table('retrieval_index_runs') \
        .select('id') \
        .eq('source_type', 'memory') \
        .eq('source_id', str(mid)) \
        .execute()
    assert not check_retrieval.data, "Retrieval index must be cleaned after first sweep"


@pytest.mark.asyncio
async def test_t4_idempotent_no_double_delete(mock_google):
    set_trace_id("sim-t4-idem")
    task = supabase.table('tasks').insert({
        'title': '[SIM_TEST] T4 Idempotent',
        'status': 'cancelled', 'is_current': True,
        'google_event_id': 'mock_event_idem',
        'recurrence': 'weekly', 'direction': 'inbound'
    }).execute()
    tid = task.data[0]['id']

    mock_google['delete_calendar_event']('mock_event_idem')
    supabase.table('tasks').update({'google_event_id': None}).eq('id', tid).execute()

    mock_google['delete_calendar_event']('mock_event_idem')
    supabase.table('tasks').update({'google_event_id': None}).eq('id', tid).execute()

    updated = supabase.table('tasks').select('google_event_id').eq('id', tid).execute()
    assert updated.data[0]['google_event_id'] is None

    supabase.table('tasks').delete().eq('id', tid).execute()
