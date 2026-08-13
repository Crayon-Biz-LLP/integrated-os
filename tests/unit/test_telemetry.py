"""
Unit tests for Tier 5 Meta-Cognitive Learning Layer telemetry module.

Tests:
T1 — hash_features is deterministic and unique per subsystem
T2 — emit_observation writes to subsystem_telemetry
T3 — emit_observation fail-open returns False on error
T4 — compute_pattern_confidence returns 'review' for <3 observations
T5 — compute_pattern_confidence returns correct values for known pattern
T6 — get_pattern_summary returns sorted results
T7 — weekly_synthesis returns structured output

Mocking note (M3 refactor): the module no longer calls `get_supabase()`
directly — every DB touch goes through `tenant_aware_client()` and
`maybe_single_safe()`. The tests therefore patch
`core.lib.telemetry.tenant_aware_client` with a self-chaining builder client
(any chain shape resolves; `.execute()` returns the configured data).
"""

import pytest
from unittest.mock import patch, MagicMock
from datetime import datetime, timezone, timedelta
from core.lib.telemetry import (
    emit_observation,
    hash_features,
    get_pattern_summary,
    compute_pattern_confidence,
    weekly_synthesis,
)


# Recent timestamps (relative to now) so temporal decay leaves confidence at
# 1.0× — hardcoded dates would drift into the decay window as time passes.
_RECENT = datetime.now(timezone.utc)
_T1_AGO = (_RECENT - timedelta(days=6)).isoformat()
_T2_AGO = (_RECENT - timedelta(days=5)).isoformat()


def _make_builder(data=None):
    """Self-chaining query builder mock: every chain verb returns self,
    `.execute()` returns a response whose `.data` is `data`."""
    m = MagicMock()
    for verb in (
        "select", "eq", "in_", "or_", "is_", "not_", "gte", "lt", "lte", "gt",
        "order", "limit", "range", "maybe_single", "ilike", "neq", "filter",
        "text_search", "insert", "update", "upsert", "delete",
    ):
        getattr(m, verb).return_value = m
    m.execute.return_value = MagicMock(data=data)
    return m


class _FakeClient:
    """Stand-in for tenant_aware_client(): per-table self-chaining builders."""

    def __init__(self, table_data=None):
        self._data = table_data or {}
        self.builders = {}

    def table(self, name):
        if name not in self.builders:
            self.builders[name] = _make_builder(self._data.get(name))
        return self.builders[name]


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
    """emit_observation inserts into subsystem_telemetry and returns True."""
    # subsystem_patterns returns no existing row → the counter takes the
    # insert branch (both are exercised through the self-chaining builder).
    client = _FakeClient({"subsystem_patterns": None})

    with patch("core.lib.telemetry.tenant_aware_client", return_value=client):
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
        telemetry_builder = client.builders["subsystem_telemetry"]
        assert telemetry_builder.insert.called
        insert_payload = telemetry_builder.insert.call_args[0][0]
        assert insert_payload["subsystem"] == "classification"
        assert insert_payload["event_type"] == "correction"
        assert insert_payload["outcome"] == "corrected"
        # The rolling pattern counter also wrote a row.
        assert client.builders["subsystem_patterns"].insert.called


# ── T3: emit_observation fail-open ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_t3_emit_fail_open():
    """emit_observation failure returns False, doesn't crash."""
    with patch(
        "core.lib.telemetry.tenant_aware_client", side_effect=Exception("DB down")
    ):
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
    client = _FakeClient({"subsystem_patterns": None})  # no existing pattern

    with patch("core.lib.telemetry.tenant_aware_client", return_value=client):
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
    client = _FakeClient({
        "subsystem_patterns": {
            "total_count": 42,
            "correct_count": 42,
            "corrected_count": 0,
            "soft_accepted_count": 0,
            "feature_json": {"source": "email", "node_type": "person"},
            "first_seen": _T1_AGO,
            "last_seen": _T2_AGO,
        }
    })

    with patch("core.lib.telemetry.tenant_aware_client", return_value=client):
        result = await compute_pattern_confidence(
            {"source": "email", "node_type": "person"}, "entity_extraction"
        )

        assert result["confidence"] == 1.0
        assert result["total_observations"] == 42
        assert result["recommendation"] == "approve"
        assert "42/42" in result["rule"]


# ── T5b: user-approved override (suggest-mode tap makes auto-approve real) ──

@pytest.mark.asyncio
async def test_t5b_user_approved_overrides_stats():
    """A suggest_approved key flips a low-confidence pattern to 'approve'.

    The handler's "will auto-approve from now on" acknowledgement must be
    true: the tap writes suggest_approved:{subsystem}:{hash}, and this
    override makes the next decision for that pattern auto-approve.
    """
    features = {"source": "email", "node_type": "person"}
    key_hash = hash_features(features, "entity_extraction")
    # Zero correct answers → normally "review"; the override must flip it.
    client = _FakeClient({
        "subsystem_patterns": {
            "total_count": 3,
            "correct_count": 0,
            "corrected_count": 0,
            "soft_accepted_count": 0,
            "feature_json": {"source": "email", "node_type": "person"},
            "first_seen": _T1_AGO,
            "last_seen": _T2_AGO,
        },
        "core_config": [{"key": f"suggest_approved:entity_extraction:{key_hash}"}],
    })

    with patch("core.lib.telemetry.tenant_aware_client", return_value=client):
        result = await compute_pattern_confidence(features, "entity_extraction")

    assert result["recommendation"] == "approve"
    assert "user-approved" in result["rule"]


@pytest.mark.asyncio
async def test_t5c_no_override_without_suggest_key():
    """Without the suggest_approved key, stats decide as before."""
    features = {"source": "email", "node_type": "person"}
    client = _FakeClient({
        "subsystem_patterns": {
            "total_count": 3,
            "correct_count": 0,
            "corrected_count": 0,
            "soft_accepted_count": 0,
            "feature_json": {"source": "email", "node_type": "person"},
            "first_seen": _T1_AGO,
            "last_seen": _T2_AGO,
        },
        "core_config": None,  # no suggest_approved key for this pattern
    })

    with patch("core.lib.telemetry.tenant_aware_client", return_value=client):
        result = await compute_pattern_confidence(features, "entity_extraction")

    assert result["recommendation"] != "approve"


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
            "first_seen": _T1_AGO,
            "last_seen": _T2_AGO,
        },
        {
            "feature_json": {"source": "telegram", "node_type": "concept"},
            "total_count": 15,
            "correct_count": 13,
            "corrected_count": 2,
            "confidence": 0.87,
            "first_seen": _T1_AGO,
            "last_seen": _T2_AGO,
        },
        {
            "feature_json": {"source": "backfill"},
            "total_count": 10,
            "correct_count": 5,
            "corrected_count": 5,
            "confidence": 0.5,
            "first_seen": _T1_AGO,
            "last_seen": _T2_AGO,
        },
    ]
    client = _FakeClient({"subsystem_patterns": mock_rows})

    with patch("core.lib.telemetry.tenant_aware_client", return_value=client):
        result = await get_pattern_summary("entity_extraction", min_observations=3)

        assert len(result) == 3
        # Should be sorted by confidence descending
        assert result[0]["confidence"] >= result[1]["confidence"]
        assert result[1]["confidence"] >= result[2]["confidence"]
        # All rows clear CONFIDENCE_AUTO_APPLY (0.5) → the top row is auto_approve
        assert result[0]["recommendation"] == "auto_approve"


# ── T7: weekly_synthesis returns structured output ──────────────────────────

@pytest.mark.asyncio
async def test_t7_weekly_synthesis_structured():
    """weekly_synthesis returns dict with patterns, drift, recommendations."""
    client = _FakeClient({})  # no patterns anywhere → empty synthesis

    with patch("core.lib.telemetry.tenant_aware_client", return_value=client):
        result = await weekly_synthesis()

        assert isinstance(result, dict)
        assert "patterns" in result
        assert "drift" in result
        assert "recommendations" in result
        assert isinstance(result["patterns"], list)
        assert isinstance(result["drift"], list)
        assert isinstance(result["recommendations"], list)
