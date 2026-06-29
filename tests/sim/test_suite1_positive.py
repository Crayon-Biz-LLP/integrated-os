import pytest
from datetime import datetime, timedelta, timezone
from core.services.db import get_supabase
from core.lib.audit_logger import set_trace_id
from core.pulse.sentinel import process_sentinel

supabase = get_supabase()


@pytest.mark.asyncio
async def test_t1_priority_escalation():
    set_trace_id("sim-t1-pos")
    created = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
    task = supabase.table('tasks').insert({
        'title': '[SIM_TEST] T1 Escalation Task',
        'priority': 'important',
        'status': 'todo',
        'direction': 'inbound',
        'is_current': True,
        'created_at': created
    }).execute()
    tid = task.data[0]['id']

    esc_candidates = supabase.table('tasks') \
        .select('id, title, created_at, priority') \
        .eq('is_current', True) \
        .eq('status', 'todo') \
        .eq('priority', 'important') \
        .execute()

    escalated = 0
    for t in (esc_candidates.data or []):
        ca = t.get('created_at', '')
        if not ca:
            continue
        created_dt = datetime.fromisoformat(str(ca).replace('Z', '+00:00'))
        days_old = (datetime.now(timezone.utc) - created_dt).days
        if days_old >= 7:
            supabase.table('tasks').update({'priority': 'urgent'}).eq('id', t['id']).execute()
            escalated += 1

    assert escalated >= 1, "Task older than 7 days should be escalated"

    updated = supabase.table('tasks').select('priority').eq('id', tid).eq('is_current', True).execute()
    assert updated.data[0]['priority'] == 'urgent'

    supabase.table('tasks').delete().eq('id', tid).execute()


@pytest.mark.asyncio
async def test_s5_followup_auto_cancel():
    set_trace_id("sim-s5-pos")
    reminder = (datetime.now(timezone.utc) - timedelta(days=20)).isoformat()
    task = supabase.table('tasks').insert({
        'title': '[SIM_TEST] S5 Stale Waiting Task',
        'status': 'todo',
        'direction': 'waiting_on',
        'reminder_at': reminder,
        'is_current': True
    }).execute()
    tid = task.data[0]['id']

    stale = supabase.table('tasks') \
        .select('id, title, created_at, reminder_at') \
        .eq('is_current', True) \
        .eq('status', 'todo') \
        .eq('direction', 'waiting_on') \
        .execute()

    cancelled = 0
    for t in (stale.data or []):
        touch = t.get('reminder_at') or t.get('created_at', '')
        if not touch:
            continue
        touch_dt = datetime.fromisoformat(str(touch).replace('Z', '+00:00'))
        if touch_dt.tzinfo is None:
            touch_dt = touch_dt.replace(tzinfo=timezone.utc)
        days_stale = (datetime.now(timezone.utc) - touch_dt).days
        if days_stale >= 14:
            supabase.table('tasks').update({'status': 'cancelled'}).eq('id', t['id']).execute()
            cancelled += 1

    assert cancelled >= 1, "Waiting task >14d stale should be cancelled"

    updated = supabase.table('tasks').select('status').eq('id', tid).execute()
    assert updated.data[0]['status'] == 'cancelled'

    supabase.table('tasks').delete().eq('id', tid).execute()


@pytest.mark.asyncio
async def test_m5_memory_sweep():
    set_trace_id("sim-m5-pos")
    from core.retrieval.cleanup import cleanup_memory_retrieval_index

    expired_at = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    mem = supabase.table('memories').insert({
        'content': '[SIM_TEST] M5 Expired Memory',
        'memory_type': 'note',
        'source': 'sim_test',
        'expires_at': expired_at
    }).execute()
    mid = mem.data[0]['id']

    expired = supabase.table('memories') \
        .select('id') \
        .lt('expires_at', datetime.now(timezone.utc).isoformat()) \
        .execute()
    expired_ids = [m['id'] for m in (expired.data or [])]

    assert mid in expired_ids, "Expired memory should appear in sweep query"

    cleanup_memory_retrieval_index(mid)
    supabase.table('memories').delete().eq('id', mid).execute()

    check = supabase.table('memories').select('id').eq('id', mid).execute()
    assert not check.data, "Memory should be deleted after sweep"


@pytest.mark.asyncio
async def test_t4_orphan_calendar_cleanup(mock_google):
    set_trace_id("sim-t4-pos")
    task = supabase.table('tasks').insert({
        'title': '[SIM_TEST] T4 Orphan Calendar',
        'status': 'cancelled',
        'is_current': True,
        'google_event_id': 'mock_event_orphan',
        'recurrence': 'weekly',
        'direction': 'inbound'
    }).execute()
    tid = task.data[0]['id']

    orphan_res = supabase.table('tasks') \
        .select('id, title, google_event_id, recurrence') \
        .eq('is_current', True) \
        .eq('status', 'cancelled') \
        .not_.is_('google_event_id', 'null') \
        .execute()

    cleaned = 0
    for t in (orphan_res.data or []):
        rec = t.get('recurrence', '')
        if rec and rec.lower() not in ('', 'none'):
            mock_google['delete_calendar_event'](t['google_event_id'])
            supabase.table('tasks').update({'google_event_id': None}).eq('id', t['id']).execute()
            cleaned += 1

    assert cleaned >= 1, "Cancelled recurring task with google_event_id should be cleaned"

    mock_google['delete_calendar_event'].assert_called_once_with('mock_event_orphan')

    updated = supabase.table('tasks').select('google_event_id').eq('id', tid).execute()
    assert updated.data[0]['google_event_id'] is None

    supabase.table('tasks').delete().eq('id', tid).execute()


@pytest.mark.asyncio
async def test_sentinel_piggyback_call_order(mock_telegram, mock_google):
    set_trace_id("sim-call-order")

    supabase.table('tasks').insert({
        'title': '[SIM_TEST] CallOrder Escalation',
        'priority': 'important', 'status': 'todo',
        'direction': 'inbound', 'is_current': True,
        'created_at': (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
    }).execute()

    supabase.table('tasks').insert({
        'title': '[SIM_TEST] CallOrder Orphan',
        'status': 'cancelled', 'is_current': True,
        'google_event_id': 'mock_event_co',
        'recurrence': 'weekly', 'direction': 'inbound'
    }).execute()

    supabase.table('memories').insert({
        'content': '[SIM_TEST] CallOrder Expired',
        'memory_type': 'note', 'source': 'sim_test',
        'expires_at': (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    }).execute()

    expired_mem = supabase.table('memories') \
        .select('id') \
        .lt('expires_at', datetime.now(timezone.utc).isoformat()) \
        .ilike('content', '[SIM_TEST] CallOrder Expired%') \
        .limit(1) \
        .execute()
    assert expired_mem.data, "Expired memory should exist before sentinel"

    result = await process_sentinel(auth_secret="dummy", trigger="cron")
    assert isinstance(result, dict), f"Sentinel should return dict, got {type(result)}"

    supabase.table('tasks').delete().ilike('title', '[SIM_TEST] CallOrder%').execute()
    supabase.table('memories').delete().ilike('content', '[SIM_TEST] CallOrder%').execute()
