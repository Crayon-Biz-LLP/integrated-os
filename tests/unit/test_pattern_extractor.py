"""Unit tests for core.lib.pattern_extractor.

Note: detect_drift() uses lazy imports (from core.services.db import get_supabase,
from core.lib.telemetry import hash_features) inside the function body,
so mocks must target the canonical import paths, not pattern_extractor module attrs.
"""

import pytest
from unittest.mock import AsyncMock, patch, MagicMock
import json

from core.lib.pattern_extractor import (
    extract_patterns,
    detect_drift,
    build_transparency_report,
)


@pytest.mark.asyncio
@patch("core.lib.pattern_extractor.get_pattern_summary", new_callable=AsyncMock)
async def test_extract_patterns_returns_list(mock_get_pattern_summary):
    """Returns a list of patterns sorted by confidence descending."""
    mock_get_pattern_summary.return_value = [
        {"subsystem": "classification", "features": {"source": "telegram", "has_url": True},
         "total_count": 10, "correct_count": 9, "confidence": 0.9,
         "recommendation": "auto_approve", "rule": "source=telegram, has_url=True: 9/10 (90%)"},
        {"subsystem": "classification", "features": {"source": "email"},
         "total_count": 5, "correct_count": 2, "confidence": 0.4,
         "recommendation": "review", "rule": "source=email: 2/5 (40%)"},
    ]

    result = await extract_patterns("classification", min_observations=3)

    assert isinstance(result, list)
    assert len(result) == 2
    assert result[0]["confidence"] >= result[1]["confidence"]
    mock_get_pattern_summary.assert_called_once_with(
        subsystem="classification", min_observations=3, max_patterns=5
    )


@pytest.mark.asyncio
@patch("core.lib.pattern_extractor.get_pattern_summary", new_callable=AsyncMock)
async def test_extract_patterns_empty_when_no_data(mock_get_pattern_summary):
    """Returns empty list when no patterns meet the threshold."""
    mock_get_pattern_summary.return_value = []

    result = await extract_patterns("classification")

    assert result == []


@pytest.mark.asyncio
@patch("core.lib.pattern_extractor.get_pattern_summary", new_callable=AsyncMock)
async def test_extract_patterns_passes_max_patterns(mock_get_pattern_summary):
    """Passes max_patterns through to get_pattern_summary."""
    mock_get_pattern_summary.return_value = [{"k": "v"}] * 10

    result = await extract_patterns("x", max_patterns=3)

    mock_get_pattern_summary.assert_called_once_with(
        subsystem="x", min_observations=3, max_patterns=3
    )
    assert len(result) == 10


@pytest.mark.asyncio
@patch("core.lib.pattern_extractor.get_pattern_summary", new_callable=AsyncMock)
@patch("core.services.db.get_supabase")
async def test_detect_drift_returns_empty_when_no_baseline(mock_get_supabase, mock_get_pattern_summary):
    """detect_drift returns empty list when no baseline exists."""
    mock_get_pattern_summary.return_value = [
        {"subsystem": "classification", "features": {"source": "telegram"},
         "total_count": 10, "correct_count": 9, "confidence": 0.9,
         "recommendation": "auto_approve", "rule": "source=telegram: 9/10 (90%)"},
    ]

    mock_supabase = MagicMock()
    mock_supabase.table.return_value.select.return_value.eq.return_value.maybe_single.return_value.execute.return_value = \
        MagicMock(data=None)
    mock_get_supabase.return_value = mock_supabase

    result = await detect_drift("classification")

    assert result == []


@pytest.mark.asyncio
@patch("core.lib.pattern_extractor.get_pattern_summary", new_callable=AsyncMock)
@patch("core.services.db.get_supabase")
@patch("core.lib.telemetry.hash_features")
async def test_detect_drift_returns_signals_when_delta_above_threshold(
    mock_hash_features, mock_get_supabase, mock_get_pattern_summary
):
    """Returns drift signals when confidence changed by more than 20%."""
    mock_get_pattern_summary.return_value = [
        {"subsystem": "classification", "features": {"source": "telegram", "has_url": True},
         "total_count": 10, "correct_count": 9, "confidence": 0.9,
         "recommendation": "auto_approve", "rule": "source=telegram, has_url=True: 9/10 (90%)"},
    ]

    mock_hash_features.return_value = "abc123"

    mock_supabase = MagicMock()
    mock_get_supabase.return_value = mock_supabase
    mock_supabase.table.return_value.select.return_value.eq.return_value.maybe_single.return_value.execute.return_value = \
        MagicMock(data={"content": json.dumps({"abc123": {"confidence": 0.5, "total_count": 5}})})

    result = await detect_drift("classification")

    assert len(result) == 1
    assert result[0]["direction"] == "up"
    assert result[0]["delta"] == 0.4
    assert result[0]["was"] == 0.5
    assert result[0]["now"] == 0.9


@pytest.mark.asyncio
@patch("core.lib.pattern_extractor.get_pattern_summary", new_callable=AsyncMock)
@patch("core.services.db.get_supabase")
@patch("core.lib.telemetry.hash_features")
async def test_detect_drift_ignores_small_deltas(
    mock_hash_features, mock_get_supabase, mock_get_pattern_summary
):
    """Does not emit drift signal for changes below 20% threshold."""
    mock_get_pattern_summary.return_value = [
        {"subsystem": "classification", "features": {"source": "telegram"},
         "total_count": 10, "correct_count": 6, "confidence": 0.6,
         "recommendation": "suggest", "rule": "source=telegram: 6/10 (60%)"},
    ]

    mock_hash_features.return_value = "def456"

    mock_supabase = MagicMock()
    mock_get_supabase.return_value = mock_supabase
    mock_supabase.table.return_value.select.return_value.eq.return_value.maybe_single.return_value.execute.return_value = \
        MagicMock(data={"content": json.dumps({"def456": {"confidence": 0.5, "total_count": 5}})})

    result = await detect_drift("classification")

    assert len(result) == 0


@pytest.mark.asyncio
@patch("core.lib.pattern_extractor.get_pattern_summary", new_callable=AsyncMock)
@patch("core.services.db.get_supabase")
@patch("core.lib.telemetry.hash_features")
async def test_detect_drift_detects_downward_drift(
    mock_hash_features, mock_get_supabase, mock_get_pattern_summary
):
    """Correctly detects when confidence has decreased."""
    mock_get_pattern_summary.return_value = [
        {"subsystem": "classification", "features": {"source": "email"},
         "total_count": 10, "correct_count": 4, "confidence": 0.4,
         "recommendation": "review", "rule": "source=email: 4/10 (40%)"},
    ]

    mock_hash_features.return_value = "ghi789"

    mock_supabase = MagicMock()
    mock_get_supabase.return_value = mock_supabase
    mock_supabase.table.return_value.select.return_value.eq.return_value.maybe_single.return_value.execute.return_value = \
        MagicMock(data={"content": json.dumps({"ghi789": {"confidence": 0.8, "total_count": 5}})})

    result = await detect_drift("classification")

    assert len(result) == 1
    assert result[0]["direction"] == "down"
    assert result[0]["delta"] == -0.4


@pytest.mark.asyncio
@patch("core.lib.pattern_extractor.weekly_synthesis", new_callable=AsyncMock)
async def test_build_transparency_report_empty_when_no_patterns(mock_weekly_synthesis):
    """Returns empty string when there are no patterns."""
    mock_weekly_synthesis.return_value = {"patterns": [], "drift": [], "recommendations": []}

    result = await build_transparency_report()

    assert result == ""


@pytest.mark.asyncio
@patch("core.lib.pattern_extractor.weekly_synthesis", new_callable=AsyncMock)
async def test_build_transparency_report_includes_patterns(mock_weekly_synthesis):
    """Includes pattern rules in the report."""
    mock_weekly_synthesis.return_value = {
        "patterns": [
            {"subsystem": "classification", "features": {"source": "telegram"},
             "confidence": 0.9, "recommendation": "auto_approve",
             "rule": "source=telegram: 9/10 (90%)"},
        ],
        "drift": [],
        "recommendations": [],
    }

    result = await build_transparency_report()

    assert "What I Learned This Week" in result
    assert "source=telegram" in result
    assert "Classification" in result


@pytest.mark.asyncio
@patch("core.lib.pattern_extractor.weekly_synthesis", new_callable=AsyncMock)
async def test_build_transparency_report_includes_drift(mock_weekly_synthesis):
    """Includes drift signals when present."""
    mock_weekly_synthesis.return_value = {
        "patterns": [
            {"subsystem": "classification", "features": {"source": "email"},
             "confidence": 0.4, "recommendation": "review",
             "rule": "source=email: 4/10 (40%)"},
        ],
        "drift": [
            {"subsystem": "classification", "delta": -0.4,
             "signal": "source=email dropped from 80% to 40%"},
        ],
        "recommendations": [],
    }

    result = await build_transparency_report()

    assert "Pattern Changes" in result
    assert "dropped" in result


@pytest.mark.asyncio
@patch("core.lib.pattern_extractor.weekly_synthesis", new_callable=AsyncMock)
async def test_build_transparency_report_includes_recommendations(mock_weekly_synthesis):
    """Includes AI recommendations when present."""
    mock_weekly_synthesis.return_value = {
        "patterns": [
            {"subsystem": "classification", "features": {"source": "telegram"},
             "confidence": 0.9, "recommendation": "auto_approve",
             "rule": "source=telegram: 9/10 (90%)"},
        ],
        "drift": [],
        "recommendations": [
            "Auto-approve: source=telegram: 9/10 (90%)",
        ],
    }

    result = await build_transparency_report()

    assert "Recommendations" in result
    assert "Auto-approve" in result


@pytest.mark.asyncio
@patch("core.lib.pattern_extractor.weekly_synthesis", new_callable=AsyncMock)
async def test_build_transparency_report_groups_by_subsystem(mock_weekly_synthesis):
    """Groups patterns by subsystem with emoji labels."""
    mock_weekly_synthesis.return_value = {
        "patterns": [
            {"subsystem": "classification", "features": {"source": "telegram"},
             "confidence": 0.9, "recommendation": "auto_approve",
             "rule": "classification rule"},
            {"subsystem": "email_pipeline", "features": {"sender_domain": "acme.com"},
             "confidence": 0.85, "recommendation": "auto_approve",
             "rule": "email rule"},
        ],
        "drift": [],
        "recommendations": [],
    }

    result = await build_transparency_report()

    assert "Classification" in result
    assert "Email Pipeline" in result or "Email" in result
