import pytest
from unittest.mock import patch
from core.webhook.workflows import check_and_resume_workflow
from core.lib.conversation import resolve_thread
from core.services.db import get_supabase
import uuid

@pytest.mark.asyncio
async def test_workflow_yes_reply():
    supabase = get_supabase()
    chat_id = 9999999
    thread_id = str(uuid.uuid4())
    
    # Setup thread
    supabase.table('conversation_threads').insert({
        'id': thread_id,
        'chat_id': chat_id,
        'thread_type': 'workflow'
    }).execute()
    
    # Setup workflow
    w_res = supabase.table('conversation_workflows').insert({
        'chat_id': chat_id,
        'thread_id': thread_id,
        'workflow_type': 'calendar_event',
        'status': 'active',
        'awaiting_user_input': True,
        'payload': {'title': 'Test Event'}
    }).execute()
    
    w_id = w_res.data[0]['id']
    
    try:
        # Test "yes" reply
        handled = await check_and_resume_workflow(chat_id, "Yes, go ahead", thread_id)
        assert handled
        
        # Verify workflow resolved
        check = supabase.table('conversation_workflows').select('status').eq('id', w_id).execute()
        assert check.data[0]['status'] == 'resolved'
        
    finally:
        # Cleanup
        supabase.table('conversation_workflows').delete().eq('id', w_id).execute()
        supabase.table('conversation_threads').delete().eq('id', thread_id).execute()
        supabase.table('tasks').delete().eq('title', 'Test Event').execute()
    supabase = get_supabase()
    chat_id = 9999998
    thread_id = str(uuid.uuid4())
    
    # Setup thread
    supabase.table('conversation_threads').insert({
        'id': thread_id,
        'chat_id': chat_id,
        'thread_type': 'workflow'
    }).execute()
    
    # Setup workflow
    w_res = supabase.table('conversation_workflows').insert({
        'chat_id': chat_id,
        'thread_id': thread_id,
        'workflow_type': 'calendar_event',
        'status': 'active',
        'awaiting_user_input': True,
        'payload': {'title': 'Test Event'}
    }).execute()
    
    w_id = w_res.data[0]['id']
    
    try:
        # Test raw note reply (should bypass workflow, stay active, return False to fall open)
        handled = await check_and_resume_workflow(chat_id, "By the way, remind me to buy milk", thread_id)
        assert not handled
        
        # Verify workflow STILL ACTIVE (not cancelled) — unrelated replies bypass without destroying state
        check = supabase.table('conversation_workflows').select('status').eq('id', w_id).execute()
        assert check.data[0]['status'] == 'active'
        
    finally:
        # Cleanup
        supabase.table('conversation_workflows').delete().eq('id', w_id).execute()
        supabase.table('conversation_threads').delete().eq('id', thread_id).execute()

@pytest.mark.asyncio
async def test_multiple_workflows_fall_open():
    supabase = get_supabase()
    chat_id = 9999997
    thread_id_1 = str(uuid.uuid4())
    thread_id_2 = str(uuid.uuid4())
    
    supabase.table('conversation_threads').insert([
        {'id': thread_id_1, 'chat_id': chat_id, 'thread_type': 'workflow'},
        {'id': thread_id_2, 'chat_id': chat_id, 'thread_type': 'workflow'}
    ]).execute()
    
    w_res = supabase.table('conversation_workflows').insert([
        {'chat_id': chat_id, 'thread_id': thread_id_1, 'workflow_type': 'calendar_event', 'status': 'active', 'awaiting_user_input': True},
        {'chat_id': chat_id, 'thread_id': thread_id_2, 'workflow_type': 'task_creation', 'status': 'active', 'awaiting_user_input': True}
    ]).execute()
    
    try:
        # Should detect multiple active workflows and fail open safely
        handled = await check_and_resume_workflow(chat_id, "Yes", thread_id_1)
        assert not handled
    finally:
        supabase.table('conversation_workflows').delete().in_('id', [w['id'] for w in w_res.data]).execute()
        supabase.table('conversation_threads').delete().in_('id', [thread_id_1, thread_id_2]).execute()


def test_resolve_thread_unrelated_entity_falls_through():
    """Active workflow + unrelated named entity → fallback thread, not workflow thread."""
    supabase = get_supabase()
    chat_id = 9999996
    thread_id = str(uuid.uuid4())
    
    supabase.table('conversation_threads').insert({
        'id': thread_id, 'chat_id': chat_id, 'thread_type': 'general'
    }).execute()
    
    w_res = supabase.table('conversation_workflows').insert({
        'chat_id': chat_id, 'thread_id': thread_id,
        'workflow_type': 'task_creation', 'status': 'active',
        'awaiting_user_input': True,
        'payload': {'title': 'Amico contract review'}
    }).execute()
    w_id = w_res.data[0]['id']
    
    try:
        routed_id, _ = resolve_thread(chat_id, "Equisoft gave the go ahead")
        assert routed_id != thread_id, \
            f"Expected different thread, got workflow thread {routed_id}"
    finally:
        supabase.table('conversation_workflows').delete().eq('id', w_id).execute()
        supabase.table('conversation_threads').delete().eq('id', thread_id).execute()


def test_resolve_thread_filler_yes_resumes_workflow():
    """Active workflow + filler 'yes' → workflow thread."""
    supabase = get_supabase()
    chat_id = 9999995
    thread_id = str(uuid.uuid4())
    
    supabase.table('conversation_threads').insert({
        'id': thread_id, 'chat_id': chat_id, 'thread_type': 'general'
    }).execute()
    
    w_res = supabase.table('conversation_workflows').insert({
        'chat_id': chat_id, 'thread_id': thread_id,
        'workflow_type': 'task_creation', 'status': 'active',
        'awaiting_user_input': True,
        'payload': {'title': 'Amico contract review'}
    }).execute()
    w_id = w_res.data[0]['id']
    
    try:
        routed_id, _ = resolve_thread(chat_id, "yes")
        assert routed_id == thread_id, \
            f"Expected workflow thread {thread_id}, got {routed_id}"
    finally:
        supabase.table('conversation_workflows').delete().eq('id', w_id).execute()
        supabase.table('conversation_threads').delete().eq('id', thread_id).execute()


@patch('core.webhook.workflows.generate_content_with_fallback')
@pytest.mark.asyncio
async def test_check_resume_skips_llm_on_topic_mismatch(mock_llm):
    """check_and_resume_workflow with unrelated entity skips LLM, returns False."""
    supabase = get_supabase()
    chat_id = 9999994
    thread_id = str(uuid.uuid4())
    
    supabase.table('conversation_threads').insert({
        'id': thread_id, 'chat_id': chat_id, 'thread_type': 'general'
    }).execute()
    
    w_res = supabase.table('conversation_workflows').insert({
        'chat_id': chat_id, 'thread_id': thread_id,
        'workflow_type': 'task_creation', 'status': 'active',
        'awaiting_user_input': True,
        'payload': {'title': 'Amico contract review'}
    }).execute()
    w_id = w_res.data[0]['id']
    
    try:
        handled = await check_and_resume_workflow(chat_id, "Equisoft team approved", thread_id)
        assert not handled, "Should return False (not handled) for unrelated entity"
        mock_llm.assert_not_called(), "LLM should not be called for topic mismatch"
        
        check = supabase.table('conversation_workflows').select('status').eq('id', w_id).execute()
        assert check.data[0]['status'] == 'active', "Workflow should remain active"
    finally:
        supabase.table('conversation_workflows').delete().eq('id', w_id).execute()
        supabase.table('conversation_threads').delete().eq('id', thread_id).execute()
