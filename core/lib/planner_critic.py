"""
Planner/Critic module for deliberative decision-making.

Provides:
- deliberate() — Generic deliberation engine for any decision domain
- plan_and_critique_intent() — Intent-specific wrapper for classification

The deliberate() function scores N candidates against multiple signals:
1. PRIMARY: The caller's primary confidence (e.g., LLM confidence)
2. PATTERN: Historical pattern confidence for this decision type
3. ENTITY: Entity overlap between text and known graph labels
4. CROSS_SUBSYSTEM: Pattern confidence from related subsystems
5. BOOST: Pattern recommendation boost

Returns a ranked recommendation with transparency.
"""

from typing import Optional
from core.lib.telemetry import compute_pattern_confidence
from core.lib.audit_logger import audit_log_sync
from core.services.db import get_supabase


# Default signal weights for generic deliberation
# These are initial values — per-signal accuracy tracking adjusts at runtime.
DEFAULT_SIGNAL_WEIGHTS = {
    "primary": 0.35,           # Caller's primary confidence (LLM, classifier, etc.)
    "pattern": 0.30,           # Historical pattern confidence for this subsystem
    "cross_subsystem": 0.15,   # Pattern confidence from related subsystems
    "entity": 0.10,            # Entity overlap with known graph labels
    "boost": 0.10,             # Pattern recommendation boost (e.g., "approve"→boost)
}

# Cross-subsystem mapping: when deliberating about one domain,
# also check related subsystems for pattern confidence.
# Map: {domain_being_decided: [related_subsystems_to_check]}
CROSS_SUBSYSTEM_MAP = {
    "classification": ["email_pipeline", "call_pipeline", "graph_edges"],
    "channel_decision": ["classification", "entity_extraction"],
    "graph_edge": ["entity_extraction", "classification"],
    "entity_extraction": ["classification", "email_pipeline", "call_pipeline"],
}


async def _extract_entities_from_text(text: str) -> list[str]:
    """Find known entity labels (people, orgs, projects) in text."""
    try:
        supabase = get_supabase()
        node_res = supabase.table('graph_nodes').select('label').in_(
            'type', ['person', 'organization', 'project']
        ).neq('epistemic_status', 'hypothetical').eq('is_current', True).execute()
        known_labels = [n['label'] for n in (node_res.data or [])]
    except Exception:
        known_labels = []

    text_lower = text.lower()
    entity_words = []
    for label in known_labels:
        if label.lower() in text_lower:
            entity_words.append(label)
    # Multi-word labels spanning adjacent tokens
    words = text_lower.split()
    for i in range(len(words)):
        for j in range(i + 2, min(i + 5, len(words) + 1)):
            chunk = ' '.join(words[i:j])
            label_match = next((lb for lb in known_labels if lb.lower() == chunk), None)
            if label_match and label_match not in entity_words:
                entity_words.append(label_match)
    return entity_words


def _resolve_entity_type(entity_words: list[str]) -> str:
    """
    Resolve the dominant entity type from a list of entity labels.
    
    Queries graph_nodes in a single bulk query to determine the type
    (person/organization/project) for each entity word, returning the
    most influential type.
    
    Priority: person > organization > project > default
    
    Args:
        entity_words: List of entity label strings found in text
        
    Returns:
        One of: "person", "organization", "project", "default"
    """
    if not entity_words:
        return "default"
    try:
        supabase = get_supabase()
        # Single query: fetch types for all entity labels at once
        # Use the first 5 entity words to limit scope
        labels_to_check = entity_words[:5]
        res = supabase.table('graph_nodes').select('type, label')\
            .in_('label', labels_to_check)\
            .neq('epistemic_status', 'hypothetical')\
            .eq('is_current', True)\
            .execute()
        # Collect all seen types
        seen_types = set()
        for n in (res.data or []):
            ntype = n.get('type', '')
            if ntype in ('person', 'organization', 'project'):
                seen_types.add(ntype)
        # Return highest-priority type
        if 'person' in seen_types:
            return 'person'
        if 'organization' in seen_types:
            return 'organization'
        if 'project' in seen_types:
            return 'project'
    except Exception:
        pass
    return "default"


async def deliberate(
    candidates: list[dict],
    text: str = "",
    subsystem: str = "classification",
    signal_weights: Optional[dict] = None,
    primary_key: str = "primary",
    cross_subsystem_domains: Optional[list[str]] = None,
) -> dict:
    """
    Generic deliberation engine.

    Takes N candidates with their primary confidence scores, scores each
    against pattern history, entity overlap, and cross-subsystem signals.

    Args:
        candidates: List of dicts, each with at least:
            {"label": str, "primary": float (0-1)}
            May also include: {"pattern_features": dict, "pattern_subsystem": str}
        text: Optional text for entity extraction bonus
        subsystem: Subsystem for pattern lookups
        signal_weights: Override DEFAULT_SIGNAL_WEIGHTS
        primary_key: Dict key for the primary confidence (default "primary")
        cross_subsystem_domains: Override CROSS_SUBSYSTEM_MAP for this subsystem

    Returns:
        {
            "candidates": [{label, score, reasoning, raw_signals}],
            "best": str,
            "runner_up": str | None,
            "delta": float,
            "recommendation": "auto_execute" | "defer"
        }
    """
    weights = signal_weights or DEFAULT_SIGNAL_WEIGHTS
    entity_words = await _extract_entities_from_text(text) if text else []
    has_entities = len(entity_words) > 0

    cross_subsystems = cross_subsystem_domains or CROSS_SUBSYSTEM_MAP.get(subsystem, [])

    scored_candidates = []
    for cand in candidates:
        label = cand.get("label", "unknown")
        primary_score = cand.get(primary_key, 0.0)

        # Pattern confidence for this specific label/decision
        pattern_features = cand.get("pattern_features", {"decision": label})
        pattern_subsystem = cand.get("pattern_subsystem", subsystem)
        pattern_result = await compute_pattern_confidence(pattern_features, pattern_subsystem)
        pattern_score = pattern_result.get("confidence", 0.0)
        pattern_recommendation = pattern_result.get("recommendation", "review")

        # Cross-subsystem signal: pattern confidence from related subsystems
        cross_score = 0.0
        cross_rule = ""
        for cs_subsystem in cross_subsystems:
            cs_result = await compute_pattern_confidence(pattern_features, cs_subsystem)
            cs_conf = cs_result.get("confidence", 0.0)
            if cs_conf > cross_score:
                cross_score = cs_conf
                cross_rule = cs_result.get("rule", "")

        # Entity overlap bonus — configurable per entity type
        # _resolve_entity_type() maps entity words to person/org/project
        # for granular weighting (people matter more than projects)
        entity_overlap = 0.0
        if has_entities:
            # Categorize each entity word by type for granular weighting
            _entity_type = _resolve_entity_type(entity_words)
            _entity_bonus_map = {
                "person": 0.15,
                "organization": 0.10,
                "project": 0.08,
                "default": 0.05,
            }
            if label in ("TASK", "approve", "create"):
                entity_overlap = _entity_bonus_map.get(_entity_type, _entity_bonus_map["default"])
            else:
                entity_overlap = _entity_bonus_map["default"]

        # Pattern recommendation boost
        pattern_boost = 0.10 if pattern_recommendation in ("approve", "auto_approve") else 0.0

        # Composite score
        composite = (
            primary_score * weights.get("primary", 0.35) +
            pattern_score * weights.get("pattern", 0.30) +
            cross_score * weights.get("cross_subsystem", 0.15) +
            entity_overlap +
            pattern_boost * weights.get("boost", 0.10)
        )
        composite = min(composite, 1.0)

        # Reasoning string
        parts = [f"Primary: {primary_score:.0%}"]
        if pattern_score > 0:
            parts.append(f"Pattern: {pattern_score:.0%} ({pattern_result.get('rule', 'N/A')})")
        if cross_score > 0:
            parts.append(f"Cross: {cross_score:.0%} ({cross_rule})")
        if entity_overlap > 0 and has_entities:
            parts.append(f"Entities: {', '.join(entity_words[:3])}")

        scored_candidates.append({
            "label": label,
            "score": round(composite, 3),
            "reasoning": " | ".join(parts),
            "raw_signals": {
                "primary": primary_score,
                "pattern": pattern_score,
                "cross_subsystem": cross_score,
                "entity_overlap": entity_overlap,
                "pattern_boost": pattern_boost,
            },
        })

    scored_candidates.sort(key=lambda c: c["score"], reverse=True)
    best = scored_candidates[0]
    runner_up = scored_candidates[1] if len(scored_candidates) > 1 else None
    delta = round(best["score"] - (runner_up["score"] if runner_up else 0.0), 3)

    # Auto-execute if best is clearly ahead
    recommendation = "auto_execute" if (delta > 0.20 or best["score"] >= 0.85) else "defer"

    return {
        "candidates": scored_candidates,
        "best": best["label"],
        "runner_up": runner_up["label"] if runner_up else None,
        "delta": delta,
        "recommendation": recommendation,
    }


async def plan_and_critique_intent(
    text: str,
    classification: dict,
) -> dict:
    """
    Intent-specific wrapper around deliberate().

    Takes the raw classification with possible_intents and uses the
    generic deliberation engine to score each candidate.

    This is the existing API used by handler.py — signature unchanged.
    """
    possible_intents = classification.get("possible_intents", [])
    primary_intent = classification.get("intent", "")

    if not possible_intents:
        return {
            "candidates": [{
                "intent": primary_intent,
                "score": classification.get("confidence", 0.5),
                "reasoning": "LLM primary choice (no alternatives)",
                "raw_signals": {"llm": classification.get("confidence", 0.5)},
            }],
            "best": primary_intent,
            "runner_up": None,
            "delta": 0.0,
            "recommendation": "auto_execute",
        }

    # Build candidates for the generic deliberator
    candidates_for_deliberation = []
    for pi in possible_intents:
        llm_key = f"confidence_{pi.lower()}"
        llm_score = classification.get(llm_key, 0.0) or 0.0
        if llm_score == 0.0:
            llm_score = classification.get("confidence", 0.5) / max(len(possible_intents), 1)

        candidates_for_deliberation.append({
            "label": pi,
            "primary": llm_score,
            "pattern_features": {"intent": pi},
            "pattern_subsystem": "classification",
        })

    result = await deliberate(
        candidates=candidates_for_deliberation,
        text=text,
        subsystem="classification",
    )

    # Rename label→intent in output for backward compatibility
    for c in result.get("candidates", []):
        c["intent"] = c.pop("label")

    audit_log_sync(
        "webhook", "INFO",
        f"Planner/Critic (refactored): best={result['best']} ({result['candidates'][0]['score']:.2f}) "
        f"runner_up={result['runner_up']} delta={result['delta']:.2f} "
        f"rec={result['recommendation']}"
    )

    return result
