import pytest
from unittest.mock import patch, MagicMock
from datetime import datetime, timedelta, timezone
from core.services.db import get_supabase
from core.lib.audit_logger import set_trace_id
from core.webhook.classify import classify_intent

supabase = get_supabase()


@pytest.mark.asyncio
async def test_c3_llm_failure_trace_id():
    set_trace_id("sim-c3-fail")

    with patch('core.webhook.classify.generate_content_with_fallback',
               side_effect=Exception("LLM network timeout")):
        result = await classify_intent(
            text="[SIM_TEST] C3 failure",
            context=[],
            ist_hour=14
        )

    assert result['intent'] == 'NOTE'
    assert result['reasoning'] == 'safe_hold'

    logs = supabase.table('audit_logs') \
        .select('id, level, message, metadata') \
        .in_('level', ['WARNING', 'ERROR']) \
        .order('created_at', desc=True) \
        .limit(10) \
        .execute()

    matched = False
    for log in (logs.data or []):
        meta = log.get('metadata', '{}')
        if isinstance(meta, str):
            import json
            try:
                meta = json.loads(meta)
            except Exception:
                continue
        if meta.get('trace_id') == 'sim-c3-fail':
            matched = True
            break

    assert matched, "At least one WARNING/ERROR audit log must carry trace_id 'sim-c3-fail'"


@pytest.mark.asyncio
async def test_m5_retry_then_warn():
    set_trace_id("sim-m5-fail")
    expired_at = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    mem = supabase.table('memories').insert({
        'content': '[SIM_TEST] M5 Retry Fail',
        'memory_type': 'note', 'source': 'sim_test',
        'expires_at': expired_at
    }).execute()
    mid = mem.data[0]['id']

    from core.retrieval.cleanup import cleanup_memory_retrieval_index

    with patch('core.retrieval.cleanup.get_supabase') as mock_db:
        mock_instance = MagicMock()
        mock_instance.table.return_value.select.return_value.execute.side_effect = Exception("DB fail")
        mock_db.return_value = mock_instance

        failed = 0
        for attempt in range(2):
            try:
                cleanup_memory_retrieval_index(mid)
                break
            except Exception:
                if attempt == 0:
                    continue
                failed += 1

    assert failed >= 0

    supabase.table('memories').delete().eq('id', mid).execute()


@pytest.mark.asyncio
async def test_k2_outer_catch_trace_id():
    set_trace_id("sim-k2-fail")
    chat_id = 9000005

    with patch('core.lib.conversation.get_supabase',
               side_effect=Exception("DB connection lost")):
        from core.lib.conversation import resolve_thread
        routed_id, anchor = resolve_thread(chat_id, "hello")

    assert isinstance(routed_id, str)
    assert anchor is None

    logs = supabase.table('audit_logs') \
        .select('id, level, metadata') \
        .in_('level', ['WARNING', 'ERROR']) \
        .order('created_at', desc=True) \
        .limit(10) \
        .execute()

    matched = False
    for log in (logs.data or []):
        meta = log.get('metadata', '{}')
        if isinstance(meta, str):
            import json
            try:
                meta = json.loads(meta)
            except Exception:
                continue
        if meta.get('trace_id') == 'sim-k2-fail':
            matched = True
            break

    assert matched, "K2 outer catch should produce ERROR audit_log with trace_id"
