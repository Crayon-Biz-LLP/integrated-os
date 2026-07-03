import pytest
import os
import uuid
from unittest.mock import patch, AsyncMock
from core.services.db import get_supabase
from core.webhook.handler import process_webhook

skip_unless_live_db = pytest.mark.skipif(
    os.getenv("LIVE_DB") != "true",
    reason="Requires LIVE_DB=true (real Supabase)"
)

# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def spy_classifier():
    """Spy on classify_intent to capture conversation_history and result.
    
    Returns a dictionary with 'args' and 'result' populated after classification.
    """
    spy_data = {'args': None, 'result': None}
    
    import core.webhook.classify
    real_classify = core.webhook.classify.classify_intent
    
    async def _spy(text, context, ist_hour=None, core_json="[]", conversation_history=""):
        spy_data['args'] = {
            'text': text,
            'context': context,
            'ist_hour': ist_hour,
            'core_json': core_json,
            'conversation_history': conversation_history
        }
        result = await real_classify(text, context, ist_hour, core_json, conversation_history)
        spy_data['result'] = result
        return result
        
    with patch('core.webhook.handler.classify_intent', side_effect=_spy):
        yield spy_data

@pytest.fixture
def mock_webhook_side_effects():
    """Mock side effects in process_webhook to prevent actual state changes."""
    with patch('core.webhook.handler.send_telegram', new_callable=AsyncMock) as m_send, \
         patch('core.webhook.handler.route_by_intent', new_callable=AsyncMock) as m_route, \
         patch('core.webhook.handler.get_recent_context', new_callable=AsyncMock) as m_recent, \
         patch('core.webhook.handler.check_and_resume_workflow', new_callable=AsyncMock) as m_wf, \
         patch('core.webhook.handler.get_or_create_session') as m_session, \
         patch('core.webhook.handler.get_thread_summary') as m_summary:
         
        m_recent.return_value = []
        m_wf.return_value = False
        m_summary.return_value = ""
        
        yield {'send': m_send, 'route': m_route, 'recent': m_recent, 'workflow': m_wf, 'session': m_session, 'summary': m_summary}

@pytest.fixture
def seeded_thread():
    """Create a seeded conversation thread and cleanup after test."""
    supabase = get_supabase()
    created_threads = []
    
    def _seed(chat_id: int, pairs: list, summary: str = None, active_anchor: dict = None):
        thread_id = str(uuid.uuid4())
        created_threads.append(thread_id)
        
        supabase.table('conversation_threads').insert({
            'id': thread_id,
            'chat_id': chat_id,
            'thread_type': 'general',
            'summary': summary,
            'active_anchor': active_anchor
        }).execute()
        
        for pair in pairs:
            if pair.get('user'):
                supabase.table('conversations').insert({
                    'session_id': thread_id,
                    'thread_id': thread_id,
                    'chat_id': chat_id,
                    'role': 'user',
                    'intent': 'NOTE',
                    'content': pair['user']['content'],
                    'token_count': len(pair['user']['content']) // 4
                }).execute()
            if pair.get('bot'):
                supabase.table('conversations').insert({
                    'session_id': thread_id,
                    'thread_id': thread_id,
                    'chat_id': chat_id,
                    'role': 'bot',
                    'intent': 'NOTE',
                    'content': pair['bot']['content'],
                    'token_count': len(pair['bot']['content']) // 4
                }).execute()
                
        return thread_id
        
    yield _seed
    
    for t_id in created_threads:
        try:
            supabase.table('conversations').delete().eq('thread_id', t_id).execute()
            supabase.table('conversation_threads').delete().eq('id', t_id).execute()
        except Exception:
            pass

@pytest.fixture
def mock_side_effects_only():
    """Mock only external side effects, leaving the DB and thread resolution real."""
    with patch('core.webhook.handler.send_telegram', new_callable=AsyncMock) as m_send, \
         patch('core.webhook.handler.route_by_intent', new_callable=AsyncMock) as m_route, \
         patch('core.webhook.handler.get_recent_context', new_callable=AsyncMock) as m_recent, \
         patch('core.webhook.handler.check_and_resume_workflow', new_callable=AsyncMock) as m_wf:
         
        m_recent.return_value = []
        m_wf.return_value = False
        yield {'send': m_send, 'route': m_route, 'recent': m_recent, 'workflow': m_wf}

# ── Tests ─────────────────────────────────────────────────────────────────────

@skip_unless_live_db
@pytest.mark.asyncio
async def test_s1_url_then_person_query_no_summary(spy_classifier, mock_webhook_side_effects):
    chat_id = int(os.environ.get("TELEGRAM_CHAT_ID", "111111111"))
    pairs = [{"user": {"content": "https://github.com/solvstrat/pricing"}, "bot": {"content": "Repository link logged for the project vault. Now go be a dad."}}]
    mock_webhook_side_effects['session'].return_value = ("fake_session_1", pairs, None)
    mock_webhook_side_effects['summary'].return_value = ""
    update = {"update_id": int(uuid.uuid4().int % 1000000000), "message": {"chat": {"id": chat_id}, "text": "Who is Binu?"}}
    res = await process_webhook(update)
    assert res.get("success") is True, f"Failed: {res}"
    history = spy_classifier['args']['conversation_history']
    assert 'Repository link logged' not in history
    assert 'go be a dad' not in history
    assert 'PRECEDING TURN' in history
    assert 'github.com' in history
    assert spy_classifier['result']['intent'] in ['QUERY', 'CLARIFICATION_NEEDED']

@skip_unless_live_db
@pytest.mark.asyncio
async def test_s2_url_then_person_query_with_summary(spy_classifier, mock_webhook_side_effects):
    chat_id = int(os.environ.get("TELEGRAM_CHAT_ID", "111111111"))
    pairs = [{"user": {"content": "https://github.com/solvstrat/pricing"}, "bot": {"content": "Repository link logged for the project vault. Now go be a dad."}}]
    summary = "User shared a repository link which was archived."
    mock_webhook_side_effects['session'].return_value = ("fake_session_2", pairs, None)
    mock_webhook_side_effects['summary'].return_value = summary
    update = {"update_id": int(uuid.uuid4().int % 1000000000), "message": {"chat": {"id": chat_id}, "text": "Who is Binu?"}}
    await process_webhook(update)
    history = spy_classifier['args']['conversation_history']
    assert 'THREAD SUMMARY' in history
    assert 'User shared a repository link' in history
    assert 'go be a dad' not in history
    assert spy_classifier['result']['intent'] in ['QUERY', 'CLARIFICATION_NEEDED']

@skip_unless_live_db
@pytest.mark.asyncio
async def test_s3_empty_history_no_crash(spy_classifier, mock_webhook_side_effects):
    chat_id = int(os.environ.get("TELEGRAM_CHAT_ID", "111111111"))
    mock_webhook_side_effects['session'].return_value = ("fake_session_3", [], None)
    mock_webhook_side_effects['summary'].return_value = ""
    update = {"update_id": int(uuid.uuid4().int % 1000000000), "message": {"chat": {"id": chat_id}, "text": "Who is Binu?"}}
    await process_webhook(update)
    history = spy_classifier['args']['conversation_history']
    assert history == ""
    assert spy_classifier['result']['intent'] in ['QUERY', 'CLARIFICATION_NEEDED']

@skip_unless_live_db
@pytest.mark.asyncio
async def test_s4_entity_anchor_in_context(spy_classifier, mock_webhook_side_effects):
    chat_id = int(os.environ.get("TELEGRAM_CHAT_ID", "111111111"))
    pairs = [{"user": {"content": "Update Binu to Pastor"}, "bot": {"content": "Role update logged for Binu."}}]
    anchor = {"name": "Binu", "type": "person"}
    mock_webhook_side_effects['session'].return_value = ("fake_session_4", pairs, anchor)
    mock_webhook_side_effects['summary'].return_value = ""
    update = {"update_id": int(uuid.uuid4().int % 1000000000), "message": {"chat": {"id": chat_id}, "text": "What about his email?"}}
    await process_webhook(update)
    history = spy_classifier['args']['conversation_history']
    assert 'ACTIVE ENTITY: Binu (person)' in history

@skip_unless_live_db
@pytest.mark.asyncio
async def test_s5_continuation_preserves_previous_turn(spy_classifier, mock_webhook_side_effects):
    chat_id = int(os.environ.get("TELEGRAM_CHAT_ID", "111111111"))
    pairs = [{"user": {"content": "Add Equisoft meeting Mon"}, "bot": {"content": "Meeting added for Equisoft on Monday."}}]
    mock_webhook_side_effects['session'].return_value = ("fake_session_5", pairs, None)
    mock_webhook_side_effects['summary'].return_value = ""
    update = {"update_id": int(uuid.uuid4().int % 1000000000), "message": {"chat": {"id": chat_id}, "text": "What about that meeting?"}}
    await process_webhook(update)
    history = spy_classifier['args']['conversation_history']
    assert 'PRECEDING TURN' in history
    assert 'Add Equisoft meeting Mon' in history

@skip_unless_live_db
@pytest.mark.asyncio
async def test_s6_bot_receipts_stripped_from_context(spy_classifier, mock_webhook_side_effects):
    chat_id = int(os.environ.get("TELEGRAM_CHAT_ID", "111111111"))
    pairs = [
        {"user": {"content": "Record meeting notes"}, "bot": {"content": "Meeting notes logged. Now go be a dad."}},
        {"user": {"content": "Check Qhord timeline"}, "bot": {"content": "Timeline check logged. Now go be a dad."}}
    ]
    mock_webhook_side_effects['session'].return_value = ("fake_session_6", pairs, None)
    mock_webhook_side_effects['summary'].return_value = ""
    update = {"update_id": int(uuid.uuid4().int % 1000000000), "message": {"chat": {"id": chat_id}, "text": "Who is Vasanth?"}}
    await process_webhook(update)
    history = spy_classifier['args']['conversation_history']
    assert 'Rhodey:' not in history
    assert 'go be a dad' not in history
    assert 'logged' not in history
    assert 'Check Qhord timeline' in history
    assert spy_classifier['result']['intent'] in ['QUERY', 'CLARIFICATION_NEEDED']

@skip_unless_live_db
@pytest.mark.asyncio
async def test_s7_resolve_thread_integration(seeded_thread, spy_classifier, mock_side_effects_only):
    chat_id = int(uuid.uuid4().int % 100000)
    pairs = [
        {"user": {"content": "[SIM_TEST] Record meeting notes"}},
        {"bot": {"content": "Meeting notes logged. Now go be a dad."}},
    ]
    summary = "User recorded meeting notes."
    anchor = {"name": "Integration Test Project", "type": "project"}
    
    seeded_thread(chat_id, pairs, summary=summary, active_anchor=anchor)
    update = {"update_id": int(uuid.uuid4().int % 1000000000), "message": {"chat": {"id": chat_id}, "text": "What about the integration?"}}
    
    with patch.dict(os.environ, {"TELEGRAM_CHAT_ID": str(chat_id)}):
        await process_webhook(update)
    
    history = spy_classifier['args']['conversation_history']
    assert 'ACTIVE ENTITY: Integration Test Project (project)' in history
    assert 'THREAD SUMMARY: User recorded meeting notes.' in history
    assert 'PRECEDING TURN' in history
    assert 'Record meeting notes' in history
    assert 'go be a dad' not in history
