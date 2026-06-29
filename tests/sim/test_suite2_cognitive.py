import pytest
import uuid
from unittest.mock import patch
from core.webhook.classify import classify_intent, SAFE_HOLD_CLASSIFICATION
from core.lib.conversation import resolve_thread
from core.services.db import get_supabase
from core.lib.audit_logger import set_trace_id

supabase = get_supabase()


@pytest.mark.asyncio
async def test_c3_safe_hold_on_llm_failure():
    set_trace_id("sim-c3")

    with patch('core.webhook.classify.generate_content_with_fallback',
               side_effect=Exception("LLM down")):
        result = await classify_intent(
            text="[SIM_TEST] Test message for safe hold",
            context=[],
            ist_hour=14
        )

    assert result == SAFE_HOLD_CLASSIFICATION, \
        f"Expected SAFE_HOLD_CLASSIFICATION, got {result}"
    assert result['intent'] == 'NOTE'
    assert result['confidence'] == 1.0
    assert result['entity'] == 'INBOX'
    assert result['reasoning'] == 'safe_hold'
    assert 'Message vaulted safely' in result['receipt']


@pytest.mark.asyncio
async def test_c3_safe_hold_on_rate_limit():
    set_trace_id("sim-c3-ratelimit")

    with patch('core.webhook.classify._classify_limiter') as mock_limiter:
        mock_limiter._get_wait_secs.return_value = 5.0
        result = await classify_intent(
            text="[SIM_TEST] Rate limited message",
            context=[],
            ist_hour=14
        )

    assert result == SAFE_HOLD_CLASSIFICATION
    assert result['intent'] == 'NOTE'


@pytest.mark.asyncio
async def test_k2_routing_workflow_priority():
    set_trace_id("sim-k2-workflow")
    chat_id = 9000001
    thread_id = str(uuid.uuid4())

    supabase.table('conversation_threads').insert({
        'id': thread_id, 'chat_id': chat_id, 'thread_type': 'general'
    }).execute()

    supabase.table('conversation_workflows').insert({
        'chat_id': chat_id, 'thread_id': thread_id,
        'workflow_type': 'calendar_event', 'status': 'active',
        'awaiting_user_input': True
    }).execute()

    routed_id, anchor = resolve_thread(chat_id, "yes go ahead")

    assert routed_id == thread_id, \
        f"Expected workflow thread {thread_id}, got {routed_id}"

    supabase.table('conversation_threads').delete().eq('id', thread_id).execute()
    supabase.table('conversation_workflows').delete().eq('chat_id', chat_id).execute()


@pytest.mark.asyncio
async def test_k2_routing_entity_match():
    set_trace_id("sim-k2-entity")
    chat_id = 9000002
    
    # Create an organization node to resolve against (has UUID id)
    org = supabase.table('organizations').insert({
        'name': 'SIM_TEST Entity Org'
    }).execute()
    entity_id = org.data[0]['id']

    thread = supabase.table('conversation_threads').insert({
        'chat_id': chat_id, 'thread_type': 'entity',
        'entity_type': 'organization', 'entity_id': entity_id
    }).execute()
    thread_id = thread.data[0]['id']

    routed_id, anchor = resolve_thread(chat_id, "status on SIM_TEST Entity Org")

    assert routed_id == thread_id, \
        f"Expected entity thread {thread_id}, got {routed_id}"

    supabase.table('conversation_threads').delete().eq('id', thread_id).execute()
    supabase.table('organizations').delete().eq('id', entity_id).execute()


@pytest.mark.asyncio
async def test_k2_outer_catch_silent_session():
    set_trace_id("sim-k2-outer")
    chat_id = 9000003

    with patch('core.lib.conversation.get_supabase',
               side_effect=Exception("DB down")):
        routed_id, anchor = resolve_thread(chat_id, "hello")

    assert isinstance(routed_id, str), "Should return a string UUID"
    assert len(routed_id) > 20, "Should be a UUID-like string"
    assert anchor is None, "Anchor should be None on fallback"

    check = supabase.table('conversations') \
        .select('id') \
        .eq('session_id', routed_id) \
        .execute()
    assert not check.data, \
        "Fallback session should NOT create any conversation_history rows"

    supabase.table('conversation_threads').delete().eq('chat_id', chat_id).execute()


@pytest.mark.asyncio
async def test_x3_context_provider_returns_dict():
    set_trace_id("sim-x3")
    from core.pulse.context import context_provider
    c = await context_provider.hydrate_tasks_context(
        query_text="SIM_TEST",
        max_chars=4000
    )
    assert isinstance(c, tuple)
    assert len(c) == 2
    assert isinstance(c[0], str)
    assert isinstance(c[1], str)
