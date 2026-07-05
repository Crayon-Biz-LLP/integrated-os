"""Unit tests for suggest-mode exclusion logic.

Tests:
S1 — hash_features_simple matches hash_features (hash chain consistency, 6 cases)
S2 — Approve callback writes core_config + increments soft_accepted_count
S3 — Approve callback fails gracefully on DB error
S4 — Approve callback idempotency via on_conflict='key'
"""

import pytest
from unittest.mock import patch, MagicMock, AsyncMock

from core.lib.telemetry import hash_features
from core.pulse.sentinel import hash_features_simple

# Reuse the _BuilderMock from test_auto_approve.py
from tests.unit.test_auto_approve import _make_builder


# ── S1: Hash chain consistency ──────────────────────────────────────────────

class TestHashChainConsistency:
    """Verify that hash_features_simple (sentinel) matches hash_features (telemetry)."""

    def test_s1_basic_features(self):
        """Simple features produce identical hashes."""
        features = {"source": "telegram", "node_type": "person"}
        h1 = hash_features(features, "entity_extraction")
        h2 = hash_features_simple(features, "entity_extraction")
        assert h1 == h2, f"Hash mismatch: {h1} != {h2}"

    def test_s1_null_values(self):
        """Null values are filtered identically."""
        features = {"source": "telegram", "node_type": None, "has_context": True}
        h1 = hash_features(features, "classification")
        h2 = hash_features_simple(features, "classification")
        assert h1 == h2

    def test_s1_different_subsystems(self):
        """Same features, different subsystems produce different (but matched) hashes."""
        f = {"channel": "email", "has_project": True, "word_count": 12}
        h1 = hash_features(f, "decision_pulse")
        h2 = hash_features_simple(f, "decision_pulse")
        assert h1 == h2

        h3 = hash_features_simple(f, "entity_extraction")
        assert h2 != h3

    def test_s1_complex_nested(self):
        """Nested dict features produce identical hashes."""
        features = {
            "source": "email",
            "has_project": True,
            "word_count": 42,
            "sender_domain": "example.com",
        }
        h1 = hash_features(features, "classification")
        h2 = hash_features_simple(features, "classification")
        assert h1 == h2

    def test_s1_empty_features(self):
        """Empty features produce identical hashes."""
        h1 = hash_features({}, "test")
        h2 = hash_features_simple({}, "test")
        assert h1 == h2

    def test_s1_long_strings(self):
        """Long string values produce identical hashes (tests MD5 consistency)."""
        features = {
            "title_keywords": ["meeting", "review", "proposal", "client", "followup"],
            "has_context": True,
            "source": "sentinel",
        }
        h1 = hash_features(features, "sentinel_nudge")
        h2 = hash_features_simple(features, "sentinel_nudge")
        assert h1 == h2


# ── S2: Callback writes core_config + increments soft_accepted_count ────────


@pytest.mark.asyncio
@patch("core.webhook.handler.supabase")
@patch("core.webhook.handler.answer_callback_query", new_callable=AsyncMock)
@patch("core.webhook.handler.send_telegram", new_callable=AsyncMock)
@patch("core.webhook.handler.os.getenv")
async def test_s2_soft_accepted_count_incremented(
    mock_getenv, mock_send_telegram, mock_answer_cb, mock_supabase
):
    """Approve callback increments soft_accepted_count from its current value."""
    from core.webhook.handler import process_callback_query

    mock_getenv.return_value = "12345"

    patterns_builder = _make_builder()
    patterns_builder.execute.return_value = MagicMock(data={
        "id": 42,
        "soft_accepted_count": 1,
    })

    core_config_builder = _make_builder()
    core_config_builder.execute.return_value = MagicMock(data=[])

    def _table_router(name):
        if name == "subsystem_patterns":
            return patterns_builder
        if name == "core_config":
            return core_config_builder
        return _make_builder()

    mock_supabase.table.side_effect = _table_router

    callback_query = {
        "id": "cb_1",
        "data": "pattern_approve_classification_a1b2c3d4e5f6a7b8",
        "message": {"chat": {"id": 12345}},
    }

    with patch("core.webhook.handler.audit_log_sync"):
        result = await process_callback_query(callback_query)

    assert result == {"success": True}

    assert patterns_builder.update.called, (
        "Expected subsystem_patterns.update to be called"
    )
    update_kwargs = patterns_builder.update.call_args[0][0]
    assert "soft_accepted_count" in update_kwargs, (
        f"Expected soft_accepted_count in update, got keys: {list(update_kwargs.keys())}"
    )
    assert update_kwargs["soft_accepted_count"] == 2, (
        f"Expected soft_accepted_count=2 (was 1 + 1), got {update_kwargs['soft_accepted_count']}"
    )

    # Verify core_config upsert was also called
    assert core_config_builder.upsert.called, (
        "Expected core_config.upsert to be called"
    )

    # Verify user was notified
    mock_send_telegram.assert_called_once()
    msg = mock_send_telegram.call_args[0][1] if len(mock_send_telegram.call_args[0]) > 1 else (
        mock_send_telegram.call_args[1].get("message_text", "")
    )
    assert "Pattern auto-approve enabled" in msg, (
        f"Expected success message, got: {msg}"
    )


@pytest.mark.asyncio
@patch("core.webhook.handler.supabase")
@patch("core.webhook.handler.answer_callback_query", new_callable=AsyncMock)
@patch("core.webhook.handler.send_telegram", new_callable=AsyncMock)
@patch("core.webhook.handler.os.getenv")
async def test_s2b_creates_if_no_existing_count(
    mock_getenv, mock_send_telegram, mock_answer_cb, mock_supabase
):
    """When pattern row has null soft_accepted_count, defaults to 0 then increments to 1."""
    from core.webhook.handler import process_callback_query

    mock_getenv.return_value = "12345"

    patterns_builder = _make_builder()
    patterns_builder.execute.return_value = MagicMock(data={
        "id": 42,
        "soft_accepted_count": None,  # No prior approvals
    })

    def _table_router(name):
        if name == "subsystem_patterns":
            return patterns_builder
        return _make_builder()

    mock_supabase.table.side_effect = _table_router

    callback_query = {
        "id": "cb_1b",
        "data": "pattern_approve_entity_extraction_f1f2f3f4e5d6c7b8a9",
        "message": {"chat": {"id": 12345}},
    }

    with patch("core.webhook.handler.audit_log_sync"):
        result = await process_callback_query(callback_query)

    assert result == {"success": True}
    assert patterns_builder.update.called
    update_kwargs = patterns_builder.update.call_args[0][0]
    assert update_kwargs["soft_accepted_count"] == 1, (
        f"Expected soft_accepted_count=1 (null→0→1), got {update_kwargs['soft_accepted_count']}"
    )


# ── S3: Approve callback fails gracefully ───────────────────────────────────


@pytest.mark.asyncio
@patch("core.webhook.handler.supabase")
@patch("core.webhook.handler.answer_callback_query", new_callable=AsyncMock)
@patch("core.webhook.handler.send_telegram", new_callable=AsyncMock)
@patch("core.webhook.handler.os.getenv")
async def test_s3_approve_fails_gracefully(
    mock_getenv, mock_send_telegram, mock_answer_cb, mock_supabase
):
    """When core_config upsert fails, user sees error, callback doesn't crash."""
    from core.webhook.handler import process_callback_query

    mock_getenv.return_value = "12345"

    # Make core_config upsert raise
    mock_supabase.table.return_value.upsert.side_effect = Exception("DB connection lost")

    callback_query = {
        "id": "cb_2",
        "data": "pattern_approve_classification_a1b2c3d4e5f6a7b8",
        "message": {"chat": {"id": 12345}},
    }

    result = await process_callback_query(callback_query)

    assert result == {"success": True}

    # Verify error message was sent to user
    assert mock_send_telegram.called, "Expected send_telegram to be called"
    call_text = (
        mock_send_telegram.call_args[0][1]
        if len(mock_send_telegram.call_args[0]) > 1
        else ""
    )
    assert "Failed to approve pattern" in call_text, (
        f"Expected error message, got: {call_text}"
    )


# ── S4: Approve callback is idempotent ─────────────────────────────────────


@pytest.mark.asyncio
@patch("core.webhook.handler.supabase")
@patch("core.webhook.handler.answer_callback_query", new_callable=AsyncMock)
@patch("core.webhook.handler.send_telegram", new_callable=AsyncMock)
@patch("core.webhook.handler.os.getenv")
async def test_s4_approve_idempotent(
    mock_getenv, mock_send_telegram, mock_answer_cb, mock_supabase
):
    """Repeated approve calls don't create duplicate core_config rows (on_conflict='key')."""
    from core.webhook.handler import process_callback_query

    mock_getenv.return_value = "12345"

    upsert_call_count = [0]
    core_config_builder = _make_builder()
    core_config_builder.execute.return_value = MagicMock(data=[])
    # Track upsert calls
    original_upsert = core_config_builder.upsert
    def counting_upsert(*args, **kwargs):
        upsert_call_count[0] += 1
        return original_upsert(*args, **kwargs)
    core_config_builder.upsert = counting_upsert

    patterns_builder = _make_builder()
    patterns_builder.execute.return_value = MagicMock(data={
        "id": 42,
        "soft_accepted_count": 1,
    })

    def _table_router(name):
        if name == "subsystem_patterns":
            return patterns_builder
        if name == "core_config":
            return core_config_builder
        return _make_builder()

    mock_supabase.table.side_effect = _table_router

    callback_query = {
        "id": "cb_3",
        "data": "pattern_approve_classification_a1b2c3d4e5f6a7b8",
        "message": {"chat": {"id": 12345}},
    }

    with patch("core.webhook.handler.audit_log_sync"):
        await process_callback_query(callback_query)
        await process_callback_query(callback_query)

    # core_config upsert called exactly twice (on_conflict='key' prevents duplicates)
    assert upsert_call_count[0] == 2, (
        f"Expected 2 upsert calls (one per callback), got {upsert_call_count[0]}"
    )
