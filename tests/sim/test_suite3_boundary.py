from datetime import datetime, timezone
from core.services.db import get_supabase
from core.lib.audit_logger import set_trace_id

supabase = get_supabase()


def test_t1_noop_no_old_tasks():
    set_trace_id("sim-t1-noop")
    existing = supabase.table('tasks') \
        .select('id, priority') \
        .eq('is_current', True) \
        .eq('status', 'todo') \
        .eq('priority', 'important') \
        .execute()

    if existing.data:
        none_old = True
        for t in existing.data:
            ca = t.get('created_at', '')
            if not ca:
                continue
            td = datetime.fromisoformat(str(ca).replace('Z', '+00:00'))
            if (datetime.now(timezone.utc) - td).days >= 7:
                none_old = False
                break
        assert none_old, "No important tasks older than 7 days should exist"
    else:
        pass


def test_s5_noop_no_stale_waiting():
    set_trace_id("sim-s5-noop")
    stale = supabase.table('tasks') \
        .select('id') \
        .eq('is_current', True) \
        .eq('status', 'todo') \
        .eq('direction', 'waiting_on') \
        .execute()

    none_old = True
    for t in (stale.data or []):
        touch = t.get('reminder_at') or t.get('created_at', '')
        if not touch:
            continue
        td = datetime.fromisoformat(str(touch).replace('Z', '+00:00'))
        if td.tzinfo is None:
            td = td.replace(tzinfo=timezone.utc)
        if (datetime.now(timezone.utc) - td).days >= 14:
            none_old = False
            break
    assert none_old, "No stale waiting_on tasks should exist"


def test_m5_noop_no_expired_memories():
    set_trace_id("sim-m5-noop")
    expired = supabase.table('memories') \
        .select('id') \
        .lt('expires_at', datetime.now(timezone.utc).isoformat()) \
        .ilike('content', '[SIM_TEST]%') \
        .execute()
    assert not expired.data, "No expired memories should exist in test namespace"


def test_t4_noop_no_orphan_calendar():
    set_trace_id("sim-t4-noop")
    orphans = supabase.table('tasks') \
        .select('id') \
        .eq('is_current', True) \
        .eq('status', 'cancelled') \
        .not_.is_('google_event_id', 'null') \
        .execute()
    assert not orphans.data, "No cancelled tasks with google_event_id should exist"
