"""
Unit tests for the /why handler — formatting, reason code display, empty-chain edge cases.

These tests mock DB calls and exercise format_decision_chain() directly.
No LIVE_DB required.
"""
import json
import pytest
from unittest.mock import patch, MagicMock, AsyncMock
from core.webhook.why_handler import format_decision_chain, handle_why, _resolve_chain_id, _fetch_decision_records


# ── W1: Empty chain returns helpful message ─────────────────────────────────

def test_w1_empty_records_message():
    result = format_decision_chain([])
    assert "No decision records found" in result


# ── W2: Classification-only chain renders summary ───────────────────────────

def test_w2_classification_stage_renders():
    records = [
        {
            '_meta': {
                'stage': 'classification',
                'query_text': 'what about Shifrah?',
                'resolved_entities': ['Shifrah'],
                'included_items': [],
                'excluded_items': [],
                'reason_codes': [],
                'summary': 'Classified as QUERY (92%)',
            }
        }
    ]
    result = format_decision_chain(records)
    assert 'Classification' in result
    assert 'QUERY' in result
    assert '92%' in result


# ── W3: Context registry stage shows kept/excluded with reason labels ────────

def test_w3_context_registry_kept_and_excluded():
    records = [
        {
            '_meta': {
                'stage': 'context_registry',
                'query_text': 'Armour project update',
                'resolved_entities': ['Armour Cyber'],
                'included_items': [
                    {'id': 'memory_1', 'content': 'Armour deal signed', 'score': 0.87, 'source': 'memories'},
                ],
                'excluded_items': [
                    {'id': 'memory_2', 'content': 'Dog walk scheduled', 'score': 0.35,
                     'source': 'memories', 'reason': 'no_entity_overlap'},
                ],
                'reason_codes': ['no_entity_overlap'],
                'summary': 'Context for PRE_FLIGHT: candidates=5 final=1',
            }
        }
    ]
    result = format_decision_chain(records)
    assert 'Context Filter' in result
    assert 'Armour deal signed' in result
    assert 'Dog walk scheduled' in result
    assert 'no entity overlap' in result


# ── W4: Top-k truncation shows correct reason label ─────────────────────────

def test_w4_top_k_truncated_reason():
    records = [
        {
            '_meta': {
                'stage': 'context_registry',
                'query_text': 'tasks',
                'resolved_entities': [],
                'included_items': [
                    {'id': 'task_1', 'content': 'Draft proposal', 'score': 1.0, 'source': 'tasks'},
                ],
                'excluded_items': [
                    {'id': 'task_2', 'content': 'Review slides', 'score': 0.9,
                     'source': 'tasks', 'reason': 'top_k_truncated'},
                ],
                'reason_codes': ['top_k_truncated'],
                'summary': 'Context for HYDRATE_TASKS: candidates=3 final=1',
            }
        }
    ]
    result = format_decision_chain(records)
    assert 'cut by top-k limit' in result


# ── W5: Multi-stage chain renders all stages in order ───────────────────────

def test_w5_multi_stage_ordering():
    records = [
        {
            '_meta': {
                'stage': 'classification',
                'query_text': 'what meetings today?',
                'resolved_entities': [],
                'included_items': [], 'excluded_items': [], 'reason_codes': [],
                'summary': 'Classified as QUERY (88%)',
            }
        },
        {
            '_meta': {
                'stage': 'routing',
                'query_text': 'what meetings today?',
                'resolved_entities': [],
                'included_items': [], 'excluded_items': [], 'reason_codes': [],
                'summary': 'Routing QUERY (88%) → interrogate_brain',
            }
        },
        {
            '_meta': {
                'stage': 'retrieval',
                'query_text': 'what meetings today?',
                'resolved_entities': [],
                'included_items': [
                    {'id': 'calendar events', 'content': 'calendar events', 'score': 1.0, 'source': 'interrogate_brain'},
                ],
                'excluded_items': [], 'reason_codes': [],
                'summary': 'interrogate_brain: 1 sources consulted — calendar events',
            }
        },
    ]
    result = format_decision_chain(records)
    # All three stage headers should appear
    assert 'Classification' in result
    assert 'Routing' in result
    assert 'Retrieval' in result
    # Classification should appear before Routing in the text
    assert result.index('Classification') < result.index('Routing')


# ── W6: handle_why returns early with no-chain message when chain is absent ──

@pytest.mark.asyncio
async def test_w6_handle_why_no_chain():
    with patch('core.webhook.why_handler.supabase') as mock_sb, \
         patch('core.webhook.why_handler.send_telegram', new_callable=AsyncMock) as mock_send:

        # Thread exists but has no last_decision_chain_id
        mock_thread = MagicMock()
        mock_thread.data = {'last_decision_chain_id': None}
        mock_sb.table.return_value.select.return_value.eq.return_value.maybe_single.return_value.execute.return_value = mock_thread

        # Fallback also returns nothing
        mock_fallback = MagicMock()
        mock_fallback.data = []
        mock_sb.table.return_value.select.return_value.eq.return_value.is_.return_value.not_.return_value.is_.return_value.order.return_value.limit.return_value.execute.return_value = mock_fallback

        await handle_why(chat_id=12345, session_id="abc")
        call_text = mock_send.call_args[0][1]
        assert "No decision records" in call_text


# ── W7: _resolve_chain_id prefers session_id thread over fallback ────────────

def test_w7_resolve_prefers_session_chain():
    with patch('core.webhook.why_handler.supabase') as mock_sb:
        mock_res = MagicMock()
        mock_res.data = {'last_decision_chain_id': 'chain-abc123'}
        mock_sb.table.return_value.select.return_value.eq.return_value.maybe_single.return_value.execute.return_value = mock_res

        result = _resolve_chain_id(chat_id=99, session_id='session-xyz')
        assert result == 'chain-abc123'


# ── W8: _fetch_decision_records filters by decision_chain_id in metadata ─────

def test_w8_fetch_filters_by_chain_id():
    target_chain = 'chain-target'
    other_chain = 'chain-other'

    rows = [
        {'id': 1, 'message': 'hit', 'metadata': json.dumps({'decision_chain_id': target_chain, 'stage': 'routing'}), 'created_at': '2026-01-01T00:00:00'},
        {'id': 2, 'message': 'miss', 'metadata': json.dumps({'decision_chain_id': other_chain, 'stage': 'routing'}), 'created_at': '2026-01-01T00:00:01'},
    ]

    with patch('core.webhook.why_handler.supabase') as mock_sb:
        mock_res = MagicMock()
        mock_res.data = rows
        mock_sb.table.return_value.select.return_value.eq.return_value.order.return_value.execute.return_value = mock_res

        result = _fetch_decision_records(target_chain)
        assert len(result) == 1
        assert result[0]['id'] == 1
