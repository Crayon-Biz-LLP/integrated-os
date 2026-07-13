"""
Shared feature builder for pattern learning decisions.

Centralizes the feature construction logic used by:
- utils.py (channel decision emission)
- engine.py (auto-approve inference)

Provides:
- _infer_rejection_reason() — derive why an item was rejected
- _context_dimensions() — time-of-day, mode, weekday context
- build_decision_features() — unified feature dict for emit_observation
"""

from datetime import datetime, timezone, timedelta
from core.services.db import get_supabase


def _project_lifecycle(project_name: str) -> str:
    """
    Detect the engagement lifecycle phase for a project.
    
    Queries recent task activity and decision history to classify
    a project as one of:
    - active: tasks created in the last 30 days
    - winding_down: no new tasks in 30+ days, decision activity >30 days old
    - cold: no tasks or decisions ever recorded
    - unknown: project name not found in graph_nodes
    
    This lets the pattern learner distinguish "rejected Equisoft when active"
    from "rejected Equisoft when winding down" — same project, different
    lifecycle phase, different feature hash.
    """
    if not project_name or len(project_name) <= 1:
        return "unknown"
    
    try:
        supabase = get_supabase()
        ist_offset = timezone(timedelta(hours=5, minutes=30))
        now = datetime.now(ist_offset)
        
        # First, check if project exists in graph_nodes
        node = supabase.table('graph_nodes').select('id').eq('type', 'project').ilike('label', project_name).eq('is_current', True).limit(1).execute()
        if not node.data:
            return "unknown"
        
        # Check for tasks created in the last 30 days for this project
        thirty_days_ago = (now - timedelta(days=30)).isoformat()
        recent_tasks = supabase.table('tasks').select('id')\
            .ilike('title', f'%{project_name}%')\
            .eq('is_current', True)\
            .gte('created_at', thirty_days_ago)\
            .limit(1)\
            .execute()
        
        if recent_tasks.data:
            return "active"
        
        # No active tasks — check if there were ever any tasks for this project
        any_tasks = supabase.table('tasks').select('id')\
            .ilike('title', f'%{project_name}%')\
            .eq('is_current', True)\
            .limit(1)\
            .execute()
        
        if any_tasks.data:
            return "winding_down"
        else:
            return "cold"
    except Exception:
        return "unknown"


def _infer_rejection_reason(msg: dict) -> str:
    """
    Infer the most likely reason a channel item was rejected.
    
    Uses available context from the message row. Falls back to "other"
    when no specific reason can be determined.
    
    Priority order (checked in code order):
    1. no_content — empty or useless body/summary
    2. not_actionable — classified as informational, not actionable
    3. unknown_sender — sender is not in the people graph
    4. wrong_project — suggested project not found in graph_nodes
    5. duplicate — item body matches an active task
    6. other — catch-all
    """
    # Check no_content first — if there's nothing to evaluate, that's the reason
    body = (msg.get('body') or msg.get('suggested_title') or '').strip()
    summary = (msg.get('summary') or '').strip()
    if not body and not summary:
        return "no_content"
    
    # Check not_actionable — classification is already set
    if msg.get('classification') in ('FYI', 'informational', 'noise', 'read_only'):
        return "not_actionable"
    
    # Check unknown_sender — sender not in people graph
    sender = (msg.get('sender_name') or '').strip()
    if sender and len(sender) > 1:
        try:
            supabase = get_supabase()
            person_check = supabase.table('people').select('id').ilike('name', sender).eq('is_current', True).limit(1).execute()
            if not person_check.data:
                return "unknown_sender"
        except Exception:
            pass  # Fail-open if DB query fails
    
    # Check wrong_project — project suggested but doesn't match the channel source
    # or project is not in the active projects list
    project = (msg.get('suggested_project') or '').strip()
    if project:
        try:
            supabase = get_supabase()
            project_check = supabase.table('graph_nodes').select('id')\
                .eq('type', 'project')\
                .ilike('label', project)\
                .eq('is_current', True)\
                .limit(1)\
                .execute()
            if not project_check.data:
                return "wrong_project"
        except Exception:
            pass
    
    # Check duplicate in active tasks
    if body:
        try:
            from core.lib.duplicate_guard import check_duplicate
            supabase = get_supabase()
            tasks = supabase.table('tasks').select('id, title')\
                .eq('is_current', True)\
                .not_.in_('status', ['done', 'cancelled'])\
                .execute()
            guard = check_duplicate(body, tasks.data or [])
            if guard.get('result') in ('block', 'flag'):
                return "duplicate"
        except Exception:
            pass
    
    return "other"


def _context_dimensions() -> dict:
    """
    Compute context features from the current time.
    
    These are injected into every observation so patterns can split by:
    - time_bucket: morning / afternoon / evening / night
    - mode: family (weekend, Fri after 7PM) / work (weekday)
    - weekday: True if Mon-Fri
    
    This lets the learner distinguish "approved Equisoft emails during work hours"
    from "rejected the same-type email on weekend."
    """
    ist_offset = timezone(timedelta(hours=5, minutes=30))
    now = datetime.now(ist_offset)
    hour = now.hour
    day = now.isoweekday()  # Monday=1, Sunday=7

    # Time bucket
    if 4 <= hour < 12:
        time_bucket = "morning"
    elif 12 <= hour < 17:
        time_bucket = "afternoon"
    elif 17 <= hour < 20:
        time_bucket = "evening"
    else:
        time_bucket = "night"

    # Mode — mirrors the pulse engine's weekend/work logic
    is_weekend = (day >= 6) or (day == 5 and hour >= 19)
    is_pre_monday = (day == 7 and hour >= 19)
    if is_weekend and not is_pre_monday:
        mode = "family"
    else:
        mode = "work"

    return {
        "time_bucket": time_bucket,
        "mode": mode,
        "weekday": day <= 5,
    }


def build_decision_features(msg: dict, channel: str = "", rejection_context: str = None) -> dict:
    """
    Build the feature dict for pattern learning from a channel message row.
    
    Includes:
    - Basic flags: has_project, has_sender
    - Entity strings: project_name (lowercased), sender_name (lowercased)
    - Channel-specific: action_type, has_people, has_summary (for calls)
    - Context dimensions: time_bucket, mode, weekday
    - Rejection reason hint: rejection_reason (inferred from context)
    
    This is the SINGLE source of truth for feature construction.
    Both utils.py (emission) and engine.py (inference) must use this function.
    """
    # Normalized entity strings
    _project_name = (msg.get('suggested_project') or '').strip().lower()
    _sender_name = (msg.get('sender_name') or '').strip().lower()
    
    # Base features
    features = {
        "has_project": bool(_project_name),
        "has_sender": bool(_sender_name),
    }
    
    # Entity strings — only when meaningful length
    if _project_name and len(_project_name) > 1:
        features["project_name"] = _project_name
    if _sender_name and len(_sender_name) > 1:
        features["sender_name"] = _sender_name
    
    # Channel-specific enrichment
    _meta = msg.get('metadata') or {}
    _action_type = _meta.get('action_type') if isinstance(_meta, dict) else None
    _people_mentioned = _meta.get('people_mentioned', []) if isinstance(_meta, dict) else []
    
    if channel == 'call':
        features["action_type"] = _action_type or "unknown"
        features["has_people"] = len(_people_mentioned) > 0
        features["has_summary"] = bool(msg.get('summary'))
    elif channel in ('whatsapp', 'teams'):
        features["has_summary"] = bool(msg.get('summary'))
    
    # Context dimensions — time of day, mode, weekday
    features.update(_context_dimensions())
    
    # Rejection reason — always included so feature hashes are consistent
    # between emission (utils.py) and inference (engine.py) paths.
    # When no specific reason can be inferred (e.g., engine.py doesn't fetch
    # full body), defaults to "none" so the hash matches approved items.
    # If user provided an explicit rejection context (e.g., "already handled"),
    # prefer that over the inferred reason.
    if rejection_context:
        features["rejection_reason"] = rejection_context.strip().lower()[:50]
    else:
        _reason = _infer_rejection_reason(msg)
        features["rejection_reason"] = _reason if _reason != "other" else "none"
    
    # Project lifecycle — differentiates active from winding_down phases
    if _project_name and len(_project_name) > 1:
        features["project_lifecycle"] = _project_lifecycle(_project_name)
    
    return features


# Cross-subsystem blend weights for composite confidence
# These determine how much influence each signal has on the final composite.
# Primary: the issuing subsystem's pattern confidence (e.g., email_pipeline)
# Cross: the best matching related subsystem's confidence (e.g., classification)
# These are module-level constants so they can be tuned without code changes.
CROSS_SUBSYSTEM_BLEND_PRIMARY = 0.70
CROSS_SUBSYSTEM_BLEND_CROSS = 0.30

# Minimum confidence for a cross-subsystem signal to be considered meaningful
CROSS_SIGNAL_MIN_CONFIDENCE = 0.3

# Recommendation boost threshold: if composite exceeds primary by this amount,
# the recommendation can be upgraded from "review" to "suggest"
CROSS_COMPOSITE_BOOST_DELTA = 0.10

async def compute_composite_confidence(features: dict, subsystem: str) -> dict:
    """
    Compute pattern confidence with cross-subsystem signal blending.
    
    Blends the primary subsystem's confidence with related subsystems
    so the pattern learner can synthesize across domains.
    
    For example, if email_pipeline says "approve this project" but
    classification says "this content is usually noise", the composite
    reflects both signals.
    
    Blend ratios are controlled by CROSS_SUBSYSTEM_BLEND_PRIMARY (0.70)
    and CROSS_SUBSYSTEM_BLEND_CROSS (0.30) at the top of this file.
    
    Args:
        features: Feature dict (from build_decision_features or custom)
        subsystem: Primary subsystem name (e.g. "email_pipeline")
        
    Returns:
        Same shape as compute_pattern_confidence, with cross-subsystem blend.
    """
    from core.lib.telemetry import compute_pattern_confidence
    
    primary = await compute_pattern_confidence(features, subsystem)
    
    # Determine related subsystems to check
    primary_domain = subsystem.replace('_pipeline', '').replace('_edges', '')
    from core.lib.planner_critic import CROSS_SUBSYSTEM_MAP
    related = CROSS_SUBSYSTEM_MAP.get(primary_domain, [])
    
    # Check each related subsystem for its confidence on these features
    cross_scores = {}
    for related_subsystem in related:
        cs = await compute_pattern_confidence(features, related_subsystem)
        if cs["confidence"] > CROSS_SIGNAL_MIN_CONFIDENCE:
            cross_scores[related_subsystem] = cs["confidence"]
    
    # Blend: primary (70%) + best cross-signal (30%) — configurable constants
    if cross_scores:
        best_cross = max(cross_scores.values())
        composite_conf = primary["confidence"] * CROSS_SUBSYSTEM_BLEND_PRIMARY + best_cross * CROSS_SUBSYSTEM_BLEND_CROSS
    else:
        composite_conf = primary["confidence"]
    
    recommendation = primary["recommendation"]
    # Boost recommendation if composite is significantly stronger
    if composite_conf > primary["confidence"] + CROSS_COMPOSITE_BOOST_DELTA and recommendation in ("suggest", "review"):
        recommendation = "suggest"
    
    rule = primary["rule"]
    if cross_scores:
        cross_detail = ", ".join(f"{k}={v:.0%}" for k, v in cross_scores.items())
        rule = f"{primary['rule']} | cross: {cross_detail}"
    
    return {
        "confidence": min(composite_conf, 1.0),
        "total_observations": primary["total_observations"],
        "recommendation": recommendation,
        "rule": rule,
    }
