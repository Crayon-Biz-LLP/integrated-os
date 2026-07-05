"""
Pattern extraction and drift detection for Tier 5 Meta-Cognitive Layer.

Provides:
- extract_patterns() — per-subsystem pattern extraction
- detect_drift() — compare this week to last week
- build_transparency_report() — "What I Learned" section for briefings
"""

from core.lib.telemetry import get_pattern_summary, weekly_synthesis
from core.lib.audit_logger import audit_log_sync
from core.services.db import maybe_single_safe


async def extract_patterns(
    subsystem: str,
    min_observations: int = 3,
    max_patterns: int = 5,
) -> list[dict]:
    """
    Extract interpretable patterns from telemetry data.

    Wraps get_pattern_summary with additional metadata for the transparency report.

    Returns patterns sorted by confidence descending.
    """
    patterns = await get_pattern_summary(
        subsystem=subsystem,
        min_observations=min_observations,
        max_patterns=max_patterns,
    )
    return patterns


async def detect_drift(subsystem: str) -> list[dict]:
    """
    Compare this week's patterns to last week's stored baseline.

    Returns list of drift signals where confidence changed by >20%.
    Each signal includes the direction and magnitude of change.
    """
    try:
        from core.services.db import get_supabase
        import json

        supabase = get_supabase()

        # Get this week's patterns
        current = await extract_patterns(subsystem, min_observations=3)

        # Get last week's baseline from core_config
        baseline_key = f"pattern_baseline:{subsystem}"
        baseline_res = maybe_single_safe(
            supabase.table("core_config")
            .select("content")
            .eq("key", baseline_key)
        )

        if not baseline_res or not baseline_res.data:
            return []

        baseline = json.loads(baseline_res.data["content"])

        drift_signals = []
        for pattern in current:
            # Use feature_hash as the baseline key (set by weekly_synthesis)
            from core.lib.telemetry import hash_features

            match_key = hash_features(pattern["features"], subsystem)

            if match_key and match_key in baseline:
                prev = baseline[match_key]
                prev_conf = prev.get("confidence", 0.0)
                curr_conf = pattern["confidence"]
                delta = curr_conf - prev_conf

                if abs(delta) > 0.20:
                    drift_signals.append({
                        "subsystem": subsystem,
                        "pattern": pattern["rule"],
                        "was": prev_conf,
                        "now": curr_conf,
                        "delta": delta,
                        "direction": "up" if delta > 0 else "down",
                    })

        return drift_signals
    except Exception as e:
        audit_log_sync("pattern_extractor", "WARNING",
                       f"detect_drift failed for {subsystem}: {e}")
        return []


async def build_transparency_report() -> str:
    """
    Build the "What I Learned This Week" section for weekend briefings.

    Returns a formatted string suitable for Telegram.
    Empty string if no significant patterns found.
    """
    synthesis = await weekly_synthesis()

    if not synthesis["patterns"]:
        return ""

    emoji_map = {
        "classification": "\U0001f3f7\ufe0f",
        "entity_extraction": "\U0001f578\ufe0f",
        "decision_pulse": "\U0001f4cb",
        "task_routing": "\U0001f500",
        "email_pipeline": "\U0001f4e8",
        "practices": "\U0001f3c3",
        "context_retrieval": "\U0001f50d",
        "briefing_generation": "\U0001f9e0",
        "sentinel_nudge": "\U0001f6a8",
    }

    lines = ["\U0001f9e0 *What I Learned This Week*", ""]

    # Group patterns by subsystem
    by_subsystem = {}
    for p in synthesis["patterns"]:
        sub = p["subsystem"]
        if sub not in by_subsystem:
            by_subsystem[sub] = []
        by_subsystem[sub].append(p)

    for subsystem, patterns in by_subsystem.items():
        emoji = emoji_map.get(subsystem, "\U0001f4ca")
        label = subsystem.replace("_", " ").title()
        lines.append(f"{emoji} *{label}:*")

        for p in patterns[:3]:  # max 3 per subsystem
            icon = "\u2705" if p["recommendation"] == "auto_approve" else \
                   "\u274c" if p["recommendation"] == "auto_reject" else \
                   "\U0001f4a1"
            lines.append(f"  {icon} {p['rule']}")

        lines.append("")

    # Add drift signals
    if synthesis["drift"]:
        lines.append("\u26a0\ufe0f *Pattern Changes (Drift Detected):*")
        for d in synthesis["drift"][:3]:
            arrow = "\u2191" if d["delta"] > 0 else "\u2193"
            lines.append(
                f"  {arrow} {d['signal']}")
        lines.append("")

    # Add recommendations
    if synthesis["recommendations"]:
        lines.append("\U0001f916 *Recommendations:*")
        for r in synthesis["recommendations"][:3]:
            lines.append(f"  \u2022 {r}")
        lines.append("")

    return "\n".join(lines)
