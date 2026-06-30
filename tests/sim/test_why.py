import pytest
import os
import json
from unittest.mock import patch, AsyncMock

from core.lib.decision_audit import set_decision_chain_id
from core.webhook.dispatch import _persist_chain_id
from core.webhook.why_handler import _resolve_chain_id, _fetch_decision_records, handle_why
from core.context import execute_context_strategy, PRE_FLIGHT_CONFIG
from core.services.db import get_supabase
from core.webhook.handler import process_webhook

skip_unless_live_db = pytest.mark.skipif(
    os.getenv("LIVE_DB") != "true",
    reason="Requires LIVE_DB=true (real Supabase)"
)


@skip_unless_live_db
@pytest.mark.asyncio
async def test_ws1_execute_context_strategy_writes_decision_audit(seed_test_data):
    """W-S1: Real DB write from execute_context_strategy for CONTEXT_REGISTRY."""
    supabase = get_supabase()
    target_chain = "sim-test-chain-001"
    set_decision_chain_id(target_chain)
    
    try:
        # Call the real context strategy
        await execute_context_strategy(
            "walk with Shifrah", 
            PRE_FLIGHT_CONFIG, 
            extracted_entities=["Shifrah"]
        )
        
        # Verify the DB write
        res = supabase.table('audit_logs') \
            .select('id, metadata') \
            .eq('service', 'decision_audit') \
            .execute()
            
        found = False
        for row in res.data or []:
            meta_str = row.get('metadata', '{}')
            meta = json.loads(meta_str) if isinstance(meta_str, str) else meta_str
            if meta.get('decision_chain_id') == target_chain:
                found = True
                assert meta.get('stage') == 'context_registry'
                assert 'included_items' in meta
                assert 'excluded_items' in meta
                break
                
        assert found, "Audit log row not created"
        
    finally:
        # Delete using python side filter for safe cleanup
        res = supabase.table('audit_logs').select('id, metadata').eq('service', 'decision_audit').execute()
        ids_to_delete = []
        for row in res.data or []:
            meta_str = row.get('metadata', '{}')
            meta = json.loads(meta_str) if isinstance(meta_str, str) else meta_str
            if meta.get('decision_chain_id') == target_chain:
                ids_to_delete.append(row['id'])
        if ids_to_delete:
            supabase.table('audit_logs').delete().in_('id', ids_to_delete).execute()


@skip_unless_live_db
def test_ws2_persist_chain_id():
    """W-S2: _persist_chain_id() updates conversation_threads.last_decision_chain_id."""
    supabase = get_supabase()
    thread_id = "00000000-0000-4000-8000-00000000bbbb"
    target_chain = "sim-test-chain-002"
    
    try:
        # Create thread
        supabase.table('conversation_threads').insert({
            'id': thread_id,
            'chat_id': 999999999
        }).execute()
        
        # Set chain ID and persist
        set_decision_chain_id(target_chain)
        _persist_chain_id(thread_id)
        
        # Verify DB
        res = supabase.table('conversation_threads').select('last_decision_chain_id').eq('id', thread_id).execute()
        assert res.data[0]['last_decision_chain_id'] == target_chain
    finally:
        supabase.table('conversation_threads').delete().eq('id', thread_id).execute()


@skip_unless_live_db
def test_ws3_resolve_chain_id_fallback():
    """W-S3: _resolve_chain_id() prefers thread/session and falls back correctly."""
    supabase = get_supabase()
    thread_id = "00000000-0000-4000-8000-00000000cccc"
    target_chain = "sim-test-chain-003"
    chat_id = 888888888
    
    try:
        supabase.table('conversation_threads').insert({
            'id': thread_id,
            'chat_id': chat_id,
            'last_decision_chain_id': target_chain
        }).execute()
        
        # 1. By session_id
        resolved = _resolve_chain_id(chat_id, thread_id)
        assert resolved == target_chain
        
        # 2. By chat_id fallback
        resolved_fallback = _resolve_chain_id(chat_id, None)
        assert resolved_fallback == target_chain
    finally:
        supabase.table('conversation_threads').delete().eq('id', thread_id).execute()


@skip_unless_live_db
def test_ws4_fetch_decision_records():
    """W-S4: _fetch_decision_records() filters correctly from real stored rows."""
    supabase = get_supabase()
    target_chain = "sim-test-chain-004"
    other_chain = "sim-test-chain-other"
    
    try:
        supabase.table('audit_logs').insert([
            {
                "service": "decision_audit",
                "level": "INFO",
                "message": "[SIM_TEST] routing",
                "metadata": json.dumps({"decision_chain_id": target_chain, "stage": "routing"})
            },
            {
                "service": "decision_audit",
                "level": "INFO",
                "message": "[SIM_TEST] other",
                "metadata": json.dumps({"decision_chain_id": other_chain, "stage": "routing"})
            }
        ]).execute()
        
        records = _fetch_decision_records(target_chain)
        assert len(records) == 1
        assert records[0]['_meta']['decision_chain_id'] == target_chain
    finally:
        res = supabase.table('audit_logs').select('id, metadata').eq('service', 'decision_audit').execute()
        ids_to_delete = []
        for row in res.data or []:
            meta_str = row.get('metadata', '{}')
            meta = json.loads(meta_str) if isinstance(meta_str, str) else meta_str
            if meta.get('decision_chain_id') and str(meta.get('decision_chain_id')).startswith('sim-test-chain-'):
                ids_to_delete.append(row['id'])
        if ids_to_delete:
            supabase.table('audit_logs').delete().in_('id', ids_to_delete).execute()


@skip_unless_live_db
@pytest.mark.asyncio
async def test_ws5_handle_why_end_to_end():
    """W-S5: handle_why() with real stored thread + decision rows produces coherent Telegram output."""
    supabase = get_supabase()
    thread_id = "00000000-0000-4000-8000-00000000dddd"
    target_chain = "sim-test-chain-005"
    chat_id = 777777777
    
    with patch('core.webhook.why_handler.send_telegram', new_callable=AsyncMock) as mock_send_telegram:
        try:
            # Create thread
            supabase.table('conversation_threads').insert({
                'id': thread_id,
                'chat_id': chat_id,
                'last_decision_chain_id': target_chain
            }).execute()
    
            # Create audit records
            supabase.table('audit_logs').insert([
                {
                    "service": "decision_audit",
                    "level": "INFO",
                    "message": "[SIM_TEST] classification",
                    "metadata": json.dumps({
                        "decision_chain_id": target_chain,
                        "stage": "classification",
                        "summary": "Classified as TASK (90%)"
                    })
                }
            ]).execute()
    
            # Call handle_why
            await handle_why(chat_id, thread_id)
    
            # Verify telegram output
            mock_send_telegram.assert_called_once()
            msg = mock_send_telegram.call_args[0][1]
            assert "Decision audit for my last response" in msg
            assert "Classification" in msg
            assert "Classified as TASK (90%)" in msg
            
        finally:
            supabase.table('conversation_threads').delete().eq('id', thread_id).execute()
            res = supabase.table('audit_logs').select('id, metadata').eq('service', 'decision_audit').execute()
            ids_to_delete = []
            for row in res.data or []:
                meta_str = row.get('metadata', '{}')
                meta = json.loads(meta_str) if isinstance(meta_str, str) else meta_str
                if meta.get('decision_chain_id') == target_chain:
                    ids_to_delete.append(row['id'])
            if ids_to_delete:
                supabase.table('audit_logs').delete().in_('id', ids_to_delete).execute()


@skip_unless_live_db
@pytest.mark.asyncio
async def test_ws6_routing_trigger_detection():
    """W-S6: Narrow routing sim test verifying that a 'why' phrase short-circuits to handle_why."""
    
    with patch('core.webhook.why_handler.handle_why', new_callable=AsyncMock) as mock_handle_why, \
         patch('core.webhook.handler.get_or_create_session') as mock_session, \
         patch('core.webhook.handler.check_and_resume_workflow', new_callable=AsyncMock) as mock_workflow, \
         patch('core.webhook.handler.classify_intent', new_callable=AsyncMock) as mock_classify:
         
        mock_session.return_value = ("sim-session-123", [], None)
        mock_workflow.return_value = False
        
        import random
        update = {
            "update_id": random.randint(1000000, 9999999),
            "message": {
                "chat": {"id": int(os.getenv("TELEGRAM_CHAT_ID", "111111111"))},
                "text": "Why did you do that?"
            }
        }
        
        res = await process_webhook(update)
        assert res.get("success") is True
        
        # handle_why should be called
        mock_handle_why.assert_called_once()
        
        # classify_intent should NOT be called (short-circuited)
        mock_classify.assert_not_called()
