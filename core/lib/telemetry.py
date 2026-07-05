"""
Tier 5 Meta-Cognitive Layer — Telemetry module.

Provides:
- emit_observation() — record a structured observation from any subsystem
- hash_features() — deterministic feature hashing for pattern grouping
- get_pattern_summary() — fetch patterns for a subsystem
- compute_pattern_confidence() — inference: given features, what's the predicted outcome?
- weekly_synthesis() — cross-subsystem pattern extractor for weekend briefings
"""

import hashlib
import json
from typing import Any, Optional
from datetime import datetime, timezone, timedelta

from core.services.db import get_supabase, maybe_single_safe
from core.lib.audit_logger import audit_log_sync

# Minimum observations required before a pattern is considered meaningful
MIN_PATTERN_OBSERVATIONS = 3

# Minimum observations before auto-apply is allowed (grace period to prevent
# sparse early-edge-case steering). Patterns need 5 observations before
# Rhodey acts on them autonomously, regardless of confidence.
MIN_AUTO_APPROVE_OBSERVATIONS = 5

# Maximum error rate (corrected/total) before auto-approve is demoted to suggest.
# If >50% of observations were corrected, the pattern is not reliable enough
# for autonomous action.
MAX_ERROR_RATE = 0.50

# Confidence thresholds for recommendations
# Lowered from 0.90 to 0.70: Rhodey acts sooner on patterns and learns from corrections
CONFIDENCE_AUTO_APPLY = 0.70
CONFIDENCE_SUGGEST = 0.50
CONFIDENCE_REVIEW = 0.0

# All subsystems that can emit telemetry
SUBSYSTEMS = [
    "classification",
    "entity_extraction",
    "decision_pulse",
    "task_routing",
    "email_pipeline",
    "call_pipeline",
    "practices",
    "context_retrieval",
    "briefing_generation",
    "memory_indexing",
    "url_handling",
    "graph_edges",
    "completion_matching",
]

# Decay constants for temporal weighting
# Observations older than these thresholds contribute diminishing weight
DECAY_DAYS_FRESH = 7      # Full weight (1.0x)
DECAY_DAYS_NORMAL = 30     # Reduced weight (0.8x)
DECAY_DAYS_STALE = 60      # Low weight (0.5x)
DECAY_DAYS_ARCHIVE = 90    # Minimal weight (0.25x)

# Compress window: if all observations happened within this many days,
# the pattern hasn't been stress-tested across time — apply a penalty.
COMPRESS_WINDOW_DAYS = 1  # Observations all within 1 day → compress penalty
COMPRESS_PENALTY = 0.15    # Subtract this from confidence if compressed


# Identity pattern markers — patterns that express standing rules or identity
# statements rather than contextual preferences. These should NOT decay with
# time because they strengthen, not weaken, with absence.
# e.g., "I never want emails from unknown senders" is an identity statement.
_IDENTITY_FEATURE_MARKERS = {
    "rejection_reason": ("unknown_sender", "no_content", "not_actionable"),
}

# Fallback dimensions for feature space sparsity.
# When the full feature hash has < MIN_PATTERN_OBSERVATIONS observations,
# progressively strip lower-signal dimensions to find a generalizable pattern.
# Order: least important (stripped first) → most important (preserved last)
_FALLBACK_DIMENSIONS = [
    "time_bucket",       # High variance, low signal — strip first
    "weekday",           # High variance, low signal
    "mode",              # Context mode — useful but situational
    "project_lifecycle", # Differentiates active/winding_down — secondary signal
    "rejection_reason",  # Only meaningful for rejected items
    "action_type",       # Call-specific: task vs note vs decision
    "has_people",        # Conversation enrichment flag
    "has_summary",       # Enrichment flag
    "project_name",      # Entity string — high signal
    "sender_name",       # Entity string — high signal
]


def _is_identity_pattern(features: dict) -> bool:
    """
    Check if a feature set represents an identity-level pattern.
    
    Identity patterns express standing rules ("never approve unknown senders")
    rather than contextual preferences ("Equisoft rejected in afternoon").
    These should NOT decay with time because they are core to how decisions
    are made, not habits that weaken without reinforcement.
    
    Current markers:
    - rejection_reason=unknown_sender: "I don't make decisions for people I don't know"
    - rejection_reason=no_content: "Don't approve empty messages"
    - rejection_reason=not_actionable: "Don't act on informational items"
    """
    for key, values in _IDENTITY_FEATURE_MARKERS.items():
        val = features.get(key, "")
        if val in values:
            return True
    return False


def _temporal_decay_multiplier(last_seen: str, features: dict = None) -> float:
    """
    Compute a confidence multiplier based on how recently the pattern was seen.
    
    Patterns that haven't been reinforced recently are less trusted, UNLESS
    they are identity patterns (standing rules that strengthen with absence).
    
    Args:
        last_seen: ISO timestamp of when the pattern was last observed
        features: Optional feature dict, used to detect identity patterns
    """
    # Identity patterns are exempt from temporal decay
    if features and _is_identity_pattern(features):
        return 1.0
    if not last_seen:
        return 1.0
    try:
        now = datetime.now(timezone.utc)
        ls = datetime.fromisoformat(last_seen.replace('Z', '+00:00'))
        days_ago = (now - ls).days
        if days_ago <= DECAY_DAYS_FRESH:
            return 1.0
        elif days_ago <= DECAY_DAYS_NORMAL:
            return 0.8
        elif days_ago <= DECAY_DAYS_STALE:
            return 0.5
        elif days_ago <= DECAY_DAYS_ARCHIVE:
            return 0.25
        else:
            return 0.1
    except Exception:
        return 1.0


def hash_features(features: dict, subsystem: str) -> str:
    """
    Deterministic hash of key features for pattern grouping.

    Canonicalizes by sorting keys and only including non-null values.

    Args:
        features: Feature dict, e.g. {"source": "telegram", "node_type": "person"}
        subsystem: Subsystem name for namespacing

    Returns:
        First 16 chars of MD5 hexdigest
    """
    canonical = {k: v for k, v in sorted(features.items()) if v is not None}
    raw = json.dumps(canonical, sort_keys=True, default=str)
    return hashlib.md5(f"{subsystem}:{raw}".encode()).hexdigest()[:16]


async def emit_observation(
    subsystem: str,
    event_type: str,
    features: dict,
    predicted: Any = None,
    actual: Any = None,
    outcome: str = "correct",
    confidence: Optional[float] = None,
    latency_ms: Optional[int] = None,
    session_id: Optional[str] = None,
    source: str = "webhook",
) -> bool:
    """
    Record a structured observation from any subsystem.

    This is the primary telemetry ingestion point. Every subsystem calls this
    when Danny provides feedback (correction, approval, rejection, engagement).

    Args:
        subsystem: 'classification', 'entity_extraction', 'context_retrieval',
                   'task_routing', 'decision_pulse', 'email_pipeline', 'practices'
        event_type: 'correction', 'approval', 'rejection', 'engagement', 'failure'
        features: Dict of structured feature values for pattern extraction.
                 Must include enough context to distinguish different scenarios.
        predicted: What the system originally produced (intent string, action string, etc.)
        actual: What actually happened (corrected intent, actual action, etc.)
        outcome: 'correct' (system was right), 'corrected' (Danny changed it),
                 'confirmed' (Danny agreed), 'rejected' (Danny said no),
                 'ignored' (Danny didn't act), 'failed' (system error)
        confidence: System's confidence score (0.0-1.0) if applicable
        latency_ms: How long the operation took
        session_id: Links to conversation thread for context
        source: 'webhook', 'pulse', 'sentinel', etc.

    Returns:
        True if observation was recorded, False on failure (fail-open)
    """
    try:
        supabase = get_supabase()
        supabase.table("subsystem_telemetry").insert({
            "subsystem": subsystem,
            "event_type": event_type,
            "features": features,
            "predicted": json.dumps(predicted) if predicted is not None else None,
            "actual": json.dumps(actual) if actual is not None else None,
            "outcome": outcome,
            "confidence": confidence,
            "latency_ms": latency_ms,
            "session_id": session_id,
            "source": source,
        }).execute()

        # Also upsert the pattern counter
        await _update_pattern_count(subsystem, features, outcome)

        return True
    except Exception as e:
        # Fail-open: telemetry should never crash the calling subsystem
        audit_log_sync("telemetry", "WARNING", f"emit_observation failed: {e}")
        return False


async def _update_pattern_count(subsystem: str, features: dict, outcome: str) -> None:
    """
    Upsert the rolling pattern counter for this feature combination.

    This is the "local pattern learner" — it simply counts observations
    per feature hash. No ML, no model — just counting.
    """
    try:
        supabase = get_supabase()
        feature_hash = hash_features(features, subsystem)

        # Try to find existing pattern row
        existing = maybe_single_safe(
            supabase.table("subsystem_patterns")
            .select("id, total_count, correct_count, corrected_count")
            .eq("subsystem", subsystem)
            .eq("feature_hash", feature_hash)
        )

        now = datetime.now(timezone.utc).isoformat()
        is_correct = outcome in ("correct", "confirmed")
        is_corrected = outcome in ("corrected", "rejected")

        if existing and existing.data:
            row = existing.data
            new_total = row["total_count"] + 1
            new_correct = row["correct_count"] + (1 if is_correct else 0)
            new_corrected = row["corrected_count"] + (1 if is_corrected else 0)
            new_confidence = new_correct / new_total if new_total > 0 else 0.0

            supabase.table("subsystem_patterns").update({
                "total_count": new_total,
                "correct_count": new_correct,
                "corrected_count": new_corrected,
                "confidence": new_confidence,
                "last_seen": now,
            }).eq("id", row["id"]).execute()
        else:
            # First observation: set confidence based on actual outcome
            # For rejections/errors (is_corrected=True, is_correct=False),
            # confidence starts at 0.0 since it represents 0% approval rate.
            initial_confidence = 1.0 if is_correct else 0.0
            supabase.table("subsystem_patterns").insert({
                "subsystem": subsystem,
                "feature_hash": feature_hash,
                "feature_json": features,
                "total_count": 1,
                "correct_count": 1 if is_correct else 0,
                "corrected_count": 1 if is_corrected else 0,
                "confidence": initial_confidence,
                "first_seen": now,
                "last_seen": now,
            }).execute()
    except Exception as e:
        # Fail-open
        audit_log_sync("telemetry", "WARNING", f"_update_pattern_count failed: {e}")


async def get_pattern_summary(
    subsystem: str,
    min_observations: int = MIN_PATTERN_OBSERVATIONS,
    max_patterns: int = 10,
    days_back: int = 30,
) -> list[dict]:
    """
    Fetch top patterns for a subsystem, sorted by confidence descending.

    Returns list of dicts:
    [
        {
            "subsystem": str,
            "features": dict,
            "total_count": int,
            "correct_count": int,
            "confidence": float,
            "recommendation": "auto_approve" | "auto_reject" | "suggest" | "review",
            "rule": str
        },
        ...
    ]
    """
    try:
        supabase = get_supabase()
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days_back)).isoformat()

        rows = (
            supabase.table("subsystem_patterns")
            .select("*")
            .eq("subsystem", subsystem)
            .gte("total_count", min_observations)
            .gte("last_seen", cutoff)
            .order("confidence", desc=True)
            .limit(max_patterns)
            .execute()
        )

        result = []
        for row in rows.data or []:
            features = row.get("feature_json", {})
            total = row["total_count"]
            correct = row["correct_count"]
            soft_accepted = row.get("soft_accepted_count", 0) or 0
            soft_boost = min(soft_accepted * 0.05, 0.15)
            base = correct / total if total > 0 else 0.0
            confidence = min(base + soft_boost, 1.0)

            error_rate = (row["corrected_count"] / total) if total > 0 else 0.0
            is_demoted = error_rate > MAX_ERROR_RATE and total >= MIN_PATTERN_OBSERVATIONS

            # Temporal decay (identity patterns exempt)
            decay_mult = _temporal_decay_multiplier(row.get("last_seen", ""), features)

            # Compression penalty
            first_seen = row.get("first_seen", "")
            last_seen = row.get("last_seen", "")
            compress_penalty = 0.0
            if first_seen and last_seen:
                try:
                    fs = datetime.fromisoformat(first_seen.replace('Z', '+00:00'))
                    ls = datetime.fromisoformat(last_seen.replace('Z', '+00:00'))
                    spread_days = (ls - fs).days
                    if spread_days < COMPRESS_WINDOW_DAYS and total >= MIN_PATTERN_OBSERVATIONS:
                        compress_penalty = COMPRESS_PENALTY
                except Exception:
                    pass

            decayed_confidence = confidence * decay_mult
            final_confidence = max(decayed_confidence - compress_penalty, 0.0)

            recommendation = "review"
            if not is_demoted and final_confidence >= CONFIDENCE_AUTO_APPLY and total >= MIN_AUTO_APPROVE_OBSERVATIONS:
                recommendation = "auto_approve" if correct > total / 2 else "auto_reject"
            elif final_confidence >= CONFIDENCE_SUGGEST:
                recommendation = "suggest"

            # Build human-readable rule with decay/compress info
            feature_parts = [f"{k}={v}" for k, v in features.items() if v]
            rule_extra = []
            if decay_mult < 1.0:
                rule_extra.append(f"decay={decay_mult:.1f}x")
            if compress_penalty > 0:
                rule_extra.append("compress")
            rule_core = f"{correct}/{total} ({final_confidence:.0%})"
            if rule_extra:
                rule = f"{', '.join(feature_parts)}: {rule_core} [{', '.join(rule_extra)}]"
            else:
                rule = f"{', '.join(feature_parts)}: {rule_core}"

            result.append({
                "subsystem": subsystem,
                "features": features,
                "total_count": total,
                "correct_count": correct,
                "confidence": confidence,
                "recommendation": recommendation,
                "rule": rule,
            })

        return result
    except Exception as e:
        audit_log_sync("telemetry", "WARNING", f"get_pattern_summary failed: {e}")
        return []


async def compute_pattern_confidence(
    features: dict,
    subsystem: str,
) -> dict:
    """
    Given a feature set, look up the stored pattern and return confidence.

    Uses a fallback chain to handle feature space sparsity:
    1. Try exact feature hash match
    2. If none found, progressively strip lower-signal dimensions until a
       match with ≥ MIN_PATTERN_OBSERVATIONS is found
    3. If still none, return "review" with insufficient data

    This prevents the "14 dimensions → zero matches → always review" problem.

    Args:
        features: Current item's features
        subsystem: Subsystem name

    Returns:
        {
            "confidence": 0.0-1.0,        # predicted accuracy
            "total_observations": int,     # how many times we've seen this
            "recommendation": str,         # "approve" | "reject" | "suggest" | "review"
            "rule": str                    # human-readable summary
        }
    """
    try:
        supabase = get_supabase()

        # Build fallback feature sets by progressively stripping dimensions
        # Each iteration strips one more dimension from the previous set.
        fallback_sets = [features]
        current = dict(features)
        for dim in _FALLBACK_DIMENSIONS:
            if dim in current:
                current = {k: v for k, v in current.items() if k != dim}
                fallback_sets.append(dict(current))

        # Try each fallback set until a hit is found
        best_match = None
        best_match_key = "exact"
        for i, fb_features in enumerate(fallback_sets):
            feature_hash = hash_features(fb_features, subsystem)
            row = maybe_single_safe(
                supabase.table("subsystem_patterns")
                .select("total_count, correct_count, corrected_count, soft_accepted_count, feature_json, first_seen, last_seen")
                .eq("subsystem", subsystem)
                .eq("feature_hash", feature_hash)
            )
            if row.data and row.data["total_count"] >= MIN_PATTERN_OBSERVATIONS:
                best_match = row.data
                if i == 0:
                    best_match_key = "exact"
                else:
                    stripped_dims = [_FALLBACK_DIMENSIONS[j] for j in range(i)]
                    best_match_key = f"fallback({','.join(stripped_dims)})"
                break

        if not best_match:
            return {
                "confidence": 0.0,
                "total_observations": 0,
                "recommendation": "review",
                "rule": "Insufficient data",
            }

        total = best_match["total_count"]
        correct = best_match["correct_count"]
        soft_accepted = best_match.get("soft_accepted_count", 0) or 0
        stored_features = best_match.get("feature_json", {}) or {}
        base_confidence = correct / total if total > 0 else 0.0

        # soft_accepted_count provides a small confidence boost (capped at 15%)
        soft_boost = min(soft_accepted * 0.05, 0.15)
        confidence = min(base_confidence + soft_boost, 1.0)

        # Error-rate demotion
        error_rate = (best_match["corrected_count"] / total) if total > 0 else 0.0
        is_demoted = error_rate > MAX_ERROR_RATE and total >= MIN_PATTERN_OBSERVATIONS

        # Temporal decay (identity patterns exempt — uses stored features for classification)
        decay_mult = _temporal_decay_multiplier(best_match.get("last_seen", ""), stored_features)

        # Compression penalty
        first_seen = best_match.get("first_seen", "")
        last_seen = best_match.get("last_seen", "")
        compress_penalty = 0.0
        if first_seen and last_seen:
            try:
                fs = datetime.fromisoformat(first_seen.replace('Z', '+00:00'))
                ls = datetime.fromisoformat(last_seen.replace('Z', '+00:00'))
                spread_days = (ls - fs).days
                if spread_days < COMPRESS_WINDOW_DAYS and total >= MIN_PATTERN_OBSERVATIONS:
                    compress_penalty = COMPRESS_PENALTY
            except Exception:
                pass

        decayed_confidence = confidence * decay_mult
        final_confidence = max(decayed_confidence - compress_penalty, 0.0)

        recommendation = "review"
        if not is_demoted and final_confidence >= CONFIDENCE_AUTO_APPLY and total >= MIN_AUTO_APPROVE_OBSERVATIONS:
            recommendation = "approve" if correct > total / 2 else "reject"
        elif final_confidence >= CONFIDENCE_SUGGEST:
            recommendation = "suggest"

        # Build rule string with fallback info
        rule_parts = [f"{correct}/{total}"]
        if best_match_key != "exact":
            rule_parts.append(f"[{best_match_key}]")
        if decay_mult < 1.0:
            rule_parts.append(f"decay={decay_mult:.1f}x")
        if compress_penalty > 0:
            rule_parts.append("compress")
        rule = f"{' '.join(rule_parts)} ({final_confidence:.0%})"

        return {
            "confidence": final_confidence,
            "total_observations": total,
            "recommendation": recommendation,
            "rule": rule,
        }
    except Exception as e:
        audit_log_sync("telemetry", "WARNING", f"compute_pattern_confidence failed: {e}")
        return {
            "confidence": 0.0,
            "total_observations": 0,
            "recommendation": "review",
            "rule": "Error",
        }


# Old call_pipeline features used just {has_project, has_sender}.
# The new enriched features include action_type, has_people, has_summary.
# This set is used by _is_stale_feature_set to detect orphaned patterns.
_OLD_CALL_FEATURES = {"has_project", "has_sender"}


def _is_stale_feature_set(feature_json: dict) -> bool:
    """Check if a pattern's features are from the old, stale hash space.
    
    For call_pipeline patterns, the old features were just {has_project, has_sender}.
    The new enriched features include action_type, has_people, has_summary.
    Any call_pipeline pattern missing the new keys is from the old hash space.
    """
    feature_keys = set(feature_json.keys())
    # Old features are a subset of new features — if the pattern has ONLY old keys
    # and is missing ALL new keys, it's stale
    return bool(feature_keys and feature_keys == _OLD_CALL_FEATURES)


async def prune_orphaned_patterns(dry_run: bool = False) -> dict:
    """
    Find and remove subsystem_patterns rows that are orphaned by feature hash changes.
    
    When features are enriched (e.g., call_pipeline got action_type/has_people/has_summary),
    old patterns with the old hash space become unreachable. This function cleans them up
    and reports the count for audit logging.
    
    Args:
        dry_run: If True, only report what would be deleted without deleting.
        
    Returns:
        {
            "deleted": int,           # patterns removed
            "total_orphans": int,     # patterns that would be orphaned
            "subsystems_affected": [str],  # which subsystems had orphans
            "dry_run": bool
        }
    """
    try:
        supabase = get_supabase()
        seven_days_ago = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
        
        # Find all patterns across all subsystems with stale feature sets
        all_rows = supabase.table("subsystem_patterns").select("id, subsystem, feature_json, last_seen").execute()
        
        orphans = []
        subsystems_affected = set()
        
        for row in all_rows.data or []:
            subsystem = row.get("subsystem", "")
            feature_json = row.get("feature_json", {}) or {}
            
            # Only check call_pipeline (the only subsystem with enriched features)
            if subsystem != "call_pipeline":
                continue
            
            if _is_stale_feature_set(feature_json):
                # Only prune if pattern hasn't been seen in 7+ days
                last_seen = row.get("last_seen", "")
                if last_seen and last_seen < seven_days_ago:
                    orphans.append(row["id"])
                    subsystems_affected.add(subsystem)
        
        deleted = 0
        if orphans and not dry_run:
            supabase.table("subsystem_patterns").delete().in_("id", orphans).execute()
            deleted = len(orphans)
            audit_log_sync(
                "telemetry", "INFO",
                f"Pruned {deleted} orphaned call_pipeline patterns (stale feature hash). "
                f"Affected subsystems: {', '.join(sorted(subsystems_affected))}"
            )
        elif orphans and dry_run:
            audit_log_sync(
                "telemetry", "INFO",
                f"[DRY RUN] Would prune {len(orphans)} orphaned call_pipeline patterns. "
                f"Affected subsystems: {', '.join(sorted(subsystems_affected))}"
            )
        
        return {
            "deleted": deleted,
            "total_orphans": len(orphans),
            "subsystems_affected": sorted(subsystems_affected),
            "dry_run": dry_run,
        }
    except Exception as e:
        audit_log_sync("telemetry", "WARNING", f"prune_orphaned_patterns failed: {e}")
        return {"deleted": 0, "total_orphans": 0, "subsystems_affected": [], "dry_run": dry_run}


async def weekly_synthesis() -> dict:
    """
    Run across ALL subsystems. Produce a structured report for the weekend briefing.

    Returns:
        {
            "patterns": [...],  # top patterns per subsystem
            "drift": [...],     # significant changes from last week
            "recommendations": [...]  # suggested config changes
        }
    """
    all_patterns = []
    all_drift = []
    all_recommendations = []

    for subsystem in SUBSYSTEMS:
        patterns = await get_pattern_summary(subsystem, min_observations=3, max_patterns=5)

        if patterns:
            all_patterns.extend(patterns)

            # Check for drift: compare confidence to stored baseline
            try:
                supabase = get_supabase()
                baseline_key = f"pattern_baseline:{subsystem}"
                baseline_res = maybe_single_safe(
                    supabase.table("core_config")
                    .select("content")
                    .eq("key", baseline_key)
                )

                if baseline_res and baseline_res.data:
                    baseline = json.loads(baseline_res.data["content"])
                    for p in patterns:
                        # Use the deterministic feature_hash as baseline key
                        # to avoid collisions between different feature combinations
                        match_key = hash_features(p["features"], subsystem)

                        if match_key and match_key in baseline:
                            prev = baseline[match_key]
                            prev_conf = prev.get("confidence", 0.0)
                            curr_conf = p["confidence"]
                            delta = curr_conf - prev_conf

                            if abs(delta) > 0.20:
                                all_drift.append({
                                    "subsystem": subsystem,
                                    "signal": f"{p['rule']} (was {prev_conf:.0%} last week)",
                                    "delta": delta,
                                })

                # Store this week's patterns as new baseline using feature_hash keys
                baseline_data = {}
                for p in patterns:
                    fhash = hash_features(p["features"], subsystem)
                    baseline_data[fhash] = {
                        "confidence": p["confidence"],
                        "total_count": p["total_count"],
                    }

                supabase.table("core_config").upsert(
                    {
                        "key": baseline_key,
                        "content": json.dumps(baseline_data),
                    },
                    on_conflict="key",
                ).execute()
            except Exception:
                pass

            # Build recommendations
            for p in patterns:
                if p["recommendation"] == "auto_approve":
                    all_recommendations.append(
                        f"Auto-approve: {p['rule']}"
                    )
                elif p["recommendation"] == "auto_reject":
                    all_recommendations.append(
                        f"Auto-reject: {p['rule']}"
                    )

    # Sort patterns by confidence descending
    all_patterns.sort(key=lambda p: p["confidence"], reverse=True)

    return {
        "patterns": all_patterns[:10],  # top 10 across all subsystems
        "drift": all_drift[:5],
        "recommendations": all_recommendations[:5],
    }
