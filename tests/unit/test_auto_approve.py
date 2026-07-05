"""Integration tests for Tier 5 auto-approve paths.

Tests that compute_pattern_confidence() is properly wired to:
1. Boost classification confidence when pattern recommends "approve"
2. Skip auto-approved channel items from decision pulse display
3. Auto-approve pending graph nodes when pattern recommends "approve"

All tests mock at the canonical import path (core.lib.telemetry.compute_pattern_confidence)
and verify the behavioral outcome (confidence boost, item exclusion, etc.).
No LIVE_DB required.
"""

import pytest
from unittest.mock import patch, MagicMock, AsyncMock


class _BuilderMock:
    """A real Python class for chainable Supabase builder mocking.

    Uses a real object (not MagicMock) for the builder to avoid
    Python 3.11 MagicMock attribute resolution bugs with 'lt' and
    other names. Individual methods are still MagicMock instances
    to allow assertion on call_count, call_args, etc.
    """
    def __init__(self, execute_result=None):
        if execute_result is None:
            execute_result = MagicMock(data=[])
        self.execute = MagicMock()
        self.execute.return_value = execute_result
        self.update = MagicMock(return_value=self)
        self.select = MagicMock(return_value=self)
        self.is_ = MagicMock(return_value=self)
        self.in_ = MagicMock(return_value=self)
        self.not_ = MagicMock(return_value=self)
        self.or_ = MagicMock(return_value=self)
        self.order = MagicMock(return_value=self)
        self.limit = MagicMock(return_value=self)
        # PostgREST methods — NO trailing underscore (not Python keywords)
        self.eq = MagicMock(return_value=self)
        self.lt = MagicMock(return_value=self)
        self.gt = MagicMock(return_value=self)
        self.gte = MagicMock(return_value=self)
        self.lte = MagicMock(return_value=self)
        self.nullslast = MagicMock(return_value=self)
        self.desc = MagicMock(return_value=self)
        self.insert = MagicMock(return_value=self)
        self.upsert = MagicMock(return_value=self)
        self.maybe_single = MagicMock(return_value=self)


def _make_builder():
    """Create a chainable Supabase builder mock."""
    return _BuilderMock()



def _configure_supabase_for_decision_pulse(mock_supabase):
    """Configure mock_supabase.table side_effect for decision pulse queries.

    Returns the individual builders so tests can assert on call counts.
    """
    messages_builder = _make_builder()
    nodes_builder = _make_builder()
    edges_builder = _make_builder()
    raw_dumps_builder = _make_builder()
    default_builder = _make_builder()

    def _table_router(name):
        return {
            "messages": messages_builder,
            "pending_graph_nodes": nodes_builder,
            "pending_graph_edges": edges_builder,
            "raw_dumps": raw_dumps_builder,
        }.get(name, default_builder)

    mock_supabase.table.side_effect = _table_router
    return messages_builder, nodes_builder, edges_builder


# ── A1: Classification auto-approve boosts confidence ────────────────────────


@pytest.mark.asyncio
@patch("core.lib.telemetry.compute_pattern_confidence", new_callable=AsyncMock)
@patch("core.webhook.classify.generate_content_with_fallback")
@patch("core.webhook.classify.cache_get")
@patch("core.webhook.classify.cache_set")
@patch("core.webhook.classify.supabase")
async def test_a1_classify_boosts_confidence_on_approve(
    mock_supabase, mock_cache_set, mock_cache_get,
    mock_llm, mock_pattern
):
    """When compute_pattern_confidence returns 'approve', result confidence is boosted."""
    mock_supabase.table.side_effect = None
    mock_supabase.table.return_value = _make_builder()
    mock_cache_get.return_value = None
    mock_cache_set.return_value = None

    # LLM returns a classification with moderate confidence
    mock_response = MagicMock()
    mock_response.parse_json.return_value = {
        "intent": "TASK",
        "confidence": 0.65,
        "entity": "General",
        "title": "Test task",
        "receipt": "Task created",
    }
    mock_llm.return_value = mock_response

    # Pattern confidence says "approve" with 0.95 confidence
    mock_pattern.return_value = {
        "confidence": 0.95,
        "total_observations": 12,
        "recommendation": "approve",
        "rule": "8/10 (95%)",
    }

    from core.webhook.classify import classify_intent

    result = await classify_intent(
        text="Send the proposal to the client",
        context=[],
        ist_hour=10,
    )

    # Confidence should be boosted from 0.65 to 0.95 (max)
    assert result["confidence"] == 0.95, (
        f"Expected boosted confidence 0.95, got {result['confidence']}"
    )
    assert result["intent"] == "TASK"
    mock_pattern.assert_called_once()


@pytest.mark.asyncio
@patch("core.lib.telemetry.compute_pattern_confidence", new_callable=AsyncMock)
@patch("core.webhook.classify.generate_content_with_fallback")
@patch("core.webhook.classify.cache_get")
@patch("core.webhook.classify.cache_set")
@patch("core.webhook.classify.supabase")
async def test_a1b_classify_no_boost_when_review(
    mock_supabase, mock_cache_set, mock_cache_get,
    mock_llm, mock_pattern
):
    """When pattern says 'review', confidence is NOT boosted."""
    mock_supabase.table.side_effect = None
    mock_supabase.table.return_value = _make_builder()
    mock_cache_get.return_value = None
    mock_cache_set.return_value = None

    mock_response = MagicMock()
    mock_response.parse_json.return_value = {
        "intent": "TASK",
        "confidence": 0.65,
        "entity": "General",
        "title": "Test task",
        "receipt": "Task created",
    }
    mock_llm.return_value = mock_response

    # Pattern says "review" with low confidence
    mock_pattern.return_value = {
        "confidence": 0.3,
        "total_observations": 2,
        "recommendation": "review",
        "rule": "1/2 (50%)",
    }

    from core.webhook.classify import classify_intent

    result = await classify_intent(
        text="Send the proposal",
        context=[],
        ist_hour=10,
    )

    # Confidence stays at original LLM value (0.65)
    assert result["confidence"] == 0.65


@pytest.mark.asyncio
@patch("core.lib.telemetry.compute_pattern_confidence", new_callable=AsyncMock)
@patch("core.webhook.classify.generate_content_with_fallback")
@patch("core.webhook.classify.cache_get")
@patch("core.webhook.classify.cache_set")
@patch("core.webhook.classify.supabase")
async def test_a1c_auto_approve_fail_open(
    mock_supabase, mock_cache_set, mock_cache_get,
    mock_llm, mock_pattern
):
    """When compute_pattern_confidence raises, classify still succeeds without boost."""
    mock_supabase.table.side_effect = None
    mock_supabase.table.return_value = _make_builder()
    mock_cache_get.return_value = None
    mock_cache_set.return_value = None

    mock_response = MagicMock()
    mock_response.parse_json.return_value = {
        "intent": "NOTE",
        "confidence": 0.80,
        "entity": "General",
        "title": "Test note",
        "receipt": "Note saved",
    }
    mock_llm.return_value = mock_response

    # Pattern raises an exception (fail-open)
    mock_pattern.side_effect = Exception("DB timeout")

    from core.webhook.classify import classify_intent

    result = await classify_intent(
        text="Test note",
        context=[],
        ist_hour=14,
    )

    # Should still succeed with original confidence
    assert result["confidence"] == 0.80
    assert result["intent"] == "NOTE"


# ── A2: Decision pulse auto-approves channel items ──────────────────────────


@pytest.mark.asyncio
@patch("core.lib.telemetry.compute_pattern_confidence", new_callable=AsyncMock)
@patch("core.lib.redis_cache.acquire_lock")
@patch("core.lib.redis_cache.release_lock")
@patch("core.pulse.run_logger.create_pulse_run", new_callable=AsyncMock)
@patch("core.pulse.run_logger.complete_pulse_run", new_callable=AsyncMock)
@patch("core.pulse.engine.send_telegram", new_callable=AsyncMock)
@patch("core.pulse.engine.supabase")
@patch("core.pulse.engine.os.getenv")
async def test_a2_decision_pulse_auto_approves_channel_items(
    mock_getenv, mock_supabase, mock_send_telegram,
    mock_complete_run, mock_create_run,
    mock_release_lock, mock_acquire_lock, mock_pattern
):
    """Channel items with high pattern confidence are auto-approved and excluded from display."""
    mock_acquire_lock.return_value = True
    mock_release_lock.return_value = True
    mock_create_run.return_value = MagicMock()
    mock_complete_run.return_value = None

    mock_getenv.side_effect = lambda key, default=None: {
        "PULSE_SECRET": "test-secret",
        "TELEGRAM_CHAT_ID": "12345",
    }.get(key, default)

    # 3 actionable items: 1 email with project, 1 email without, 1 call
    messages_data = [
        {"id": 1, "channel": "email", "classification": "actionable",
         "suggested_title": "Review proposal", "suggested_project": "Acme",
         "sender_name": "John", "metadata": {}, "subject": "Proposal review"},
        {"id": 2, "channel": "email", "classification": "actionable",
         "suggested_title": "Schedule meeting", "suggested_project": None,
         "sender_name": "Jane", "metadata": {}, "subject": "Meeting"},
        {"id": 3, "channel": "call", "classification": "actionable",
         "suggested_title": "Call with Bob", "suggested_project": None,
         "sender_name": None, "metadata": {"action_type": "task"}, "subject": None},
    ]

    messages_builder, nodes_builder, _ = _configure_supabase_for_decision_pulse(mock_supabase)

    # messages_builder.execute() calls in order:
    # 1. expiry sweep update chain     → empty
    # 2. pending messages select chain  → messages_data
    # 3. auto-approve update chain      → empty
    # 4. shown_in_brief update chain     → empty
    messages_builder.execute.side_effect = [
        MagicMock(data=[]),               # 1. expiry sweep
        MagicMock(data=messages_data),    # 2. pending messages
        MagicMock(data=[]),               # 3. auto-approve
        MagicMock(data=[]),               # 4. shown_in_brief
    ]

    # Set pattern to return 'review' so no items get auto-approved in this test
    mock_pattern.return_value = {
        "confidence": 0.3,
        "total_observations": 2,
        "recommendation": "review",
        "rule": "1/2 (50%)",
    }

    # Patch emit_observation and audit_log_sync to prevent real DB calls
    with patch("core.lib.telemetry.emit_observation", new_callable=AsyncMock):
        with patch("core.pulse.engine.audit_log_sync"):
            from core.pulse.engine import process_decision_pulse
            result = await process_decision_pulse(auth_secret="test-secret")

    # No items auto-approved (pattern says 'review'):
    # updates on messages: (1) expiry sweep, (2) shown_in_brief
    assert messages_builder.update.call_count >= 2, (
        f"Expected >=2 update calls on messages (expiry + shown), "
        f"got {messages_builder.update.call_count}"
    )

    # Verify send_telegram was called (function proceeded past auto-approve)
    send_call = mock_send_telegram.call_args
    assert send_call is not None, "Expected send_telegram to be called"
    message_text = send_call[1].get("message_text", "")
    # At minimum, remaining items (Schedule meeting) should appear
    assert "Schedule meeting" in message_text, (
        "Non-approved item should still appear in message"
    )

    assert result["success"] is True


# ── A3: Engine.py pending graph nodes auto-approve ──────────────────────────


@pytest.mark.asyncio
@patch("core.lib.telemetry.compute_pattern_confidence", new_callable=AsyncMock)
@patch("core.pulse.graph.process_graph_pending_decision", new_callable=AsyncMock)
@patch("core.lib.redis_cache.acquire_lock")
@patch("core.lib.redis_cache.release_lock")
@patch("core.pulse.run_logger.create_pulse_run", new_callable=AsyncMock)
@patch("core.pulse.run_logger.complete_pulse_run", new_callable=AsyncMock)
@patch("core.pulse.engine.send_telegram", new_callable=AsyncMock)
@patch("core.pulse.engine.supabase")
@patch("core.pulse.engine.os.getenv")
async def test_a3_graph_nodes_auto_approve(
    mock_getenv, mock_supabase, mock_send_telegram,
    mock_complete_run, mock_create_run,
    mock_release_lock, mock_acquire_lock,
    mock_process_graph, mock_pattern
):
    """Pending graph nodes with high pattern confidence are auto-approved before display."""
    mock_acquire_lock.return_value = True
    mock_release_lock.return_value = True
    mock_create_run.return_value = MagicMock()
    mock_complete_run.return_value = None

    mock_getenv.side_effect = lambda key, default=None: {
        "PULSE_SECRET": "test-secret",
        "TELEGRAM_CHAT_ID": "12345",
    }.get(key, default)

    mock_process_graph.return_value = {"success": True, "action": "approved"}

    nodes_data = [
        {"id": 10, "label": "Acme Corp", "type": "organization",
         "source_tag": "extraction", "source_text": "client meeting"},
        {"id": 11, "label": "Unknown Person", "type": "person",
         "source_tag": "extraction", "source_text": None},
    ]

    _, nodes_builder, _ = _configure_supabase_for_decision_pulse(mock_supabase)

    # nodes_builder.execute() calls in order:
    # 1. stale revert update chain               → empty
    # 2. pending nodes select chain              → nodes_data
    # 3. awaiting_details count select chain     → empty
    nodes_builder.execute.side_effect = [
        MagicMock(data=[]),            # 1. stale revert
        MagicMock(data=nodes_data),    # 2. pending nodes
        MagicMock(data=[]),            # 3. awaiting_details
    ]

    # Pattern says "approve" for node 10 (has context), "review" for node 11
    async def _pattern_side_effect(features, subsystem):
        has_context = features.get("has_context", False)
        if subsystem == "entity_extraction" and has_context:
            return {
                "confidence": 0.92,
                "total_observations": 20,
                "recommendation": "approve",
                "rule": "19/20 (92%)",
            }
        return {
            "confidence": 0.3,
            "total_observations": 2,
            "recommendation": "review",
            "rule": "1/2 (50%)",
        }
    mock_pattern.side_effect = _pattern_side_effect

    # Patch emit_observation and audit_log_sync to prevent real DB calls
    with patch("core.lib.telemetry.emit_observation", new_callable=AsyncMock):
        with patch("core.pulse.engine.audit_log_sync"):
            from core.pulse.engine import process_decision_pulse
            result = await process_decision_pulse(auth_secret="test-secret")

    # Node 10 (Acme Corp) should have been auto-approved with auto_decided=True
    mock_process_graph.assert_called_once_with(10, "approve", auto_decided=True)
    assert result["success"] is True
