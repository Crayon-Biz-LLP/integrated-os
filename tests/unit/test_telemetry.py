"""
Unit tests for Tier 5 Meta-Cognitive Learning Layer telemetry module.

Tests:
T1 — hash_features is deterministic and unique per subsystem
T2 — emit_observation writes to subsystem_telemetry
T3 — emit_observation fail-open returns False on error
T4 — compute_pattern_confidence returns 'review' for <3 observations
T5 — compute_pattern_confidence returns correct values for known pattern
T6 — get_pattern_summary returns sorted results
"""

import pytest
from unittest.mock import patch, MagicMock
from core.lib.telemetry import (
    emit_observation,
    hash_features,
    get_pattern_summary,
    compute_pattern_confidence,
    weekly_synthesis,
)


# ── T1: hash_features is deterministic and unique per subsystem ──────────────

def test_t1_hash_deterministic():
    """Same features + subsystem always produce same hash."""
    features = {"source": "telegram", "node_type": "person", "has_context": True}
    h1 = hash_features(features, "entity_extraction")
    h2 = hash_features(features, "entity_extraction")
    assert h1 == h2
    assert len(h1) == 16


def test_t1_hash_different_subsystems():
    """Different subsystems with same features produce different hashes."""
    f = {"source": "email"}
    h1 = hash_features(f, "entity_extraction")
    h2 = hash_features(f, "classification")
    assert h1 != h2


def test_t1_hash_null_values_filtered():
    """Null values are excluded from the hash — None and {} treated same."""
    h1 = hash_features({"source": "email", "node_type": None}, "test")
    h2 = hash_features({"source": "email"}, "test")
    assert h1 == h2


# ── T2: emit_observation writes to subsystem_telemetry ──────────────────────

@pytest.mark.asyncio
async def test_t2_emit_inserts_row():
    """emit_observation calls supabase.table('subsystem_telemetry').insert()."""
    with patch("core.lib.telemetry.get_supabase") as mock_get_db:
        mock_supabase = MagicMock()
        mock_table = MagicMock()
        mock_insert = MagicMock()
        mock_supabase.table.return_value = mock_table
        mock_table.insert.return_value = mock_insert
        mock_get_db.return_value = mock_supabase

        # Also mock the pattern count update path
        def table_side_effect(name):
            if name == "subsystem_patterns":
                mock_patterns = MagicMock()
                mock_select = MagicMock()
                mock_select.maybe_single.return_value = MagicMock(data=None)
                mock_patterns.select.return_value = mock_select
                return mock_patterns
            return mock_table
        mock_supabase.table.side_effect = table_side_effect

        result = await emit_observation(
            subsystem="classification",
            event_type="correction",
            features={"source": "telegram", "word_count": 5},
            predicted="NOTE",
            actual="TASK",
            outcome="corrected",
            confidence=0.6,
            source="test",
        )

        assert result is True
        # Verify subsystem_telemetry insert was called
        insert_call_args = mock_table.insert.call_args[0][0]
        assert insert_call_args["subsystem"] == "classification"
        assert insert_call_args["event_type"] == "correction"
        assert insert_call_args["outcome"] == "corrected"


# ── T3: emit_observation fail-open ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_t3_emit_fail_open():
    """emit_observation failure returns False, doesn't crash."""
    with patch("core.lib.telemetry.get_supabase") as mock_get_db:
        mock_get_db.side_effect = Exception("DB down")
        result = await emit_observation(
            subsystem="classification",
            event_type="correction",
            features={"source": "test"},
            outcome="corrected",
        )
        assert result is False  # fail-open returns False, doesn't raise


# ── T4: compute_pattern_confidence with <3 observations returns 'review' ────

@pytest.mark.asyncio
async def test_t4_compute_confidence_insufficient():
    """With <3 observations in DB, returns recommendation='review'."""
    with patch("core.lib.telemetry.get_supabase") as mock_get_db:
        mock_supabase = MagicMock()
        mock_table = MagicMock()
        mock_supabase.table.return_value = mock_table
        mock_select = MagicMock()
        mock_select.maybe_single.return_value = MagicMock(data=None)
        mock_table.select.return_value = mock_select
        mock_get_db.return_value = mock_supabase

        result = await compute_pattern_confidence(
            {"source": "email"}, "entity_extraction"
        )

        assert result["recommendation"] == "review"
        assert result["confidence"] == 0.0
        assert result["total_observations"] == 0


# ── T5: compute_pattern_confidence with known pattern ──────────────────────

@pytest.mark.asyncio
async def test_t5_compute_confidence_known():
    """With 42 approve + 0 reject, returns approve recommendation."""
    with patch("core.lib.telemetry.get_supabase") as mock_get_db:
        mock_supabase = MagicMock()
        mock_table = MagicMock()
        mock_supabase.table.return_value = mock_table

        # Method chaining: .select().eq().eq().limit().maybe_single().execute()
        # (maybe_single_safe adds .limit(1) before .maybe_single())
        mock_select = MagicMock()
        mock_select.eq.return_value = mock_select  # chain eq() calls
        mock_select.limit.return_value = mock_select  # chain limit() from maybe_single_safe
        mock_maybe = MagicMock()
        mock_execute = MagicMock()
        mock_execute.data = {
            "total_count": 42,
            "correct_count": 42,
            "corrected_count": 0,
            "soft_accepted_count": 0,
            "feature_json": {"source": "email", "node_type": "person"},
        }
        mock_maybe.execute.return_value = mock_execute
        mock_select.maybe_single.return_value = mock_maybe
        mock_table.select.return_value = mock_select
        mock_get_db.return_value = mock_supabase

        result = await compute_pattern_confidence(
            {"source": "email", "node_type": "person"}, "entity_extraction"
        )

        assert result["confidence"] == 1.0
        assert result["total_observations"] == 42
        assert result["recommendation"] == "approve"
        assert "42/42" in result["rule"]


# ── T6: get_pattern_summary returns sorted results ─────────────────────────

@pytest.mark.asyncio
async def test_t6_get_pattern_summary_returns_sorted():
    """get_pattern_summary returns patterns sorted by confidence descending."""
    mock_rows = [
        {
            "feature_json": {"source": "email", "node_type": "person"},
            "total_count": 20,
            "correct_count": 20,
            "corrected_count": 0,
            "confidence": 1.0,
        },
        {
            "feature_json": {"source": "telegram", "node_type": "concept"},
            "total_count": 15,
            "correct_count": 13,
            "corrected_count": 2,
            "confidence": 0.87,
        },
        {
            "feature_json": {"source": "backfill"},
            "total_count": 10,
            "correct_count": 5,
            "corrected_count": 5,
            "confidence": 0.5,
        },
    ]

    with patch("core.lib.telemetry.get_supabase") as mock_get_db:
        mock_supabase = MagicMock()
        mock_table = MagicMock()
        mock_supabase.table.return_value = mock_table

        # Method chaining: .select().eq().gte().gte().order().limit().execute()
        mock_select = MagicMock()
        mock_select.eq.return_value = mock_select
        mock_select.gte.return_value = mock_select
        mock_select.order.return_value = mock_select
        mock_select.limit.return_value = mock_select
        mock_execute = MagicMock()
        mock_execute.data = mock_rows
        mock_select.execute.return_value = mock_execute
        mock_table.select.return_value = mock_select
        mock_get_db.return_value = mock_supabase

        result = await get_pattern_summary("entity_extraction", min_observations=3)

        assert len(result) == 3
        # Should be sorted by confidence descending
        assert result[0]["confidence"] >= result[1]["confidence"]
        assert result[1]["confidence"] >= result[2]["confidence"]
        # First should be auto_approve (100%), second suggest (87%)
        assert result[0]["recommendation"] == "auto_approve"


# ── T7: weekly_synthesis returns structured output ──────────────────────────

@pytest.mark.asyncio
async def test_t7_weekly_synthesis_structured():
    """weekly_synthesis returns dict with patterns, drift, recommendations."""
    with patch("core.lib.telemetry.get_supabase") as mock_get_db:
        mock_supabase = MagicMock()
        mock_table = MagicMock()
        mock_supabase.table.return_value = mock_table

        # Make all pattern queries return empty (no patterns yet)
        mock_execute = MagicMock()
        mock_execute.data = []

        mock_limit = MagicMock()
        mock_limit.execute.return_value = mock_execute

        mock_order = MagicMock()
        mock_order.limit.return_value = mock_limit

        mock_gte2 = MagicMock()
        mock_gte2.order.return_value = mock_order

        mock_gte1 = MagicMock()
        mock_gte1.gte.return_value = mock_gte2

        mock_eq = MagicMock()
        mock_eq.gte.return_value = mock_gte1

        mock_table.select.return_value = mock_eq
        mock_get_db.return_value = mock_supabase

        result = await weekly_synthesis()

        assert isinstance(result, dict)
        assert "patterns" in result
        assert "drift" in result
        assert "recommendations" in result
        assert isinstance(result["patterns"], list)
        assert isinstance(result["drift"], list)
        assert isinstance(result["recommendations"], list)
