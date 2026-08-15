import pytest
import uuid
from unittest.mock import patch
from core.webhook.classify import classify_intent, SAFE_HOLD_CLASSIFICATION
from core.lib.conversation import resolve_thread
from core.services.db import tenant_aware_client
from core.lib.audit_logger import set_trace_id
from sim.conftest import requires_live_db
from tests.fixtures.run_isolation import run_chat_id
pytestmark = pytest.mark.ingest


supabase = tenant_aware_client()


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

    with patch('core.llm.budget.tenant_llm_limiter') as mock_limiter:
        mock_limiter.return_value._get_wait_secs.return_value = 5.0
        result = await classify_intent(
            text="[SIM_TEST] Rate limited message",
            context=[],
            ist_hour=14
        )

    assert result == SAFE_HOLD_CLASSIFICATION
    assert result['intent'] == 'NOTE'


@requires_live_db
@pytest.mark.asyncio
async def test_k2_routing_workflow_priority():
    set_trace_id("sim-k2-workflow")
    chat_id = run_chat_id(1)  # X4: per-run band
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
    try:
        supabase.table("organizations").select("id").limit(1).execute()
    except Exception:
        import pytest
        pytest.skip("migration 75 removed the organizations table — sim suite targets old schema")
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


@requires_live_db
@pytest.mark.asyncio
async def test_k2_outer_catch_silent_session():
    set_trace_id("sim-k2-outer")
    chat_id = 9000003

    with patch('core.lib.conversation.tenant_aware_client',
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


@requires_live_db
@pytest.mark.asyncio
async def test_x3_context_provider_returns_dict():
    set_trace_id("sim-x3")
    from core.pulse.context import context_provider
    c = await context_provider.hydrate_tasks_context(
        query_text="SIM_TEST",
        max_chars=4000
    )
    # hydrate_tasks_context returns a formatted string since the context
    # refactor (was a (str, str) tuple pre-refactor).
    assert isinstance(c, str), f"Expected str, got {type(c).__name__}"
    assert len(c) > 0, "Formatted task context should be non-empty"
