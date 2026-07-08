from dataclasses import dataclass
from typing import Literal, List
from core.retrieval.ranking import WeightConfig, DEFAULT_WEIGHTS

# Dedicated weight configs matching pre-refactoring behavioral values.
# Old memory.py explicitly passed recency_weight=0.4, importance_weight=0.2
# for both get_recent_memories_for_briefing and retrieve_hindsight_memories.
# The DEFAULT_WEIGHTS (recency=0.15, importance=0.10) diluted these by >2x.
BRIEFING_WEIGHTS = WeightConfig(
    semantic=0.25, ppr=0.25, specificity=0.10,
    recency=0.4, importance=0.2,
    project_boost=0.10, person_boost=0.05
)
HINDSIGHT_WEIGHTS = BRIEFING_WEIGHTS  # Same weights as old hindsight path

@dataclass
class StrategyConfig:
    name: str
    threshold: float
    top_k: int
    weights: WeightConfig
    gate_mode: Literal["hard", "soft", "none"]
    semantic_enabled: bool
    semantic_requires_anchor: bool  # If True, semantic search only runs if named anchors exist
    fact_sources: List[Literal["tasks", "people", "emails", "meeting_minutes"]]
    
# Pre-Flight: fetch recent, semantically-similar context for upcoming meetings.
# Uses legacy vector path (pipeline.py forces use_associative=False) so all
# memories — including ones created minutes ago that haven't been indexed yet —
# are findable via the memories.embedding column.
# top_k=12 gives headroom: fact sources (tasks, people) consume some slots, and
# we need enough remaining slots for memories about the meeting topic.
# threshold=0.45 (interim, lowered from 0.55) — the meeting_minutes keyword pass
# (Fix 2) covers named-entity meetings; the lower semantic threshold catches
# loosely-similar but contextually relevant content as a safety net.
PRE_FLIGHT_CONFIG = StrategyConfig(
    name="PRE_FLIGHT",
    threshold=0.45,
    top_k=12,
    weights=DEFAULT_WEIGHTS,
    gate_mode="hard",
    semantic_enabled=True,
    semantic_requires_anchor=True,
    fact_sources=["tasks", "people", "emails", "meeting_minutes"]
)

# Briefing: blended, grounded
BRIEFING_CONFIG = StrategyConfig(
    name="BRIEFING",
    threshold=0.7,
    top_k=8,
    weights=BRIEFING_WEIGHTS,
    gate_mode="hard",
    semantic_enabled=True,
    semantic_requires_anchor=False,  # Briefing can search broadly, but hard gates apply
    fact_sources=["tasks", "people"]
)

# Hindsight: blended, semantic, slightly looser threshold
HINDSIGHT_CONFIG = StrategyConfig(
    name="HINDSIGHT",
    threshold=0.6,
    top_k=5,
    weights=HINDSIGHT_WEIGHTS,
    gate_mode="soft",
    semantic_enabled=True,
    semantic_requires_anchor=False,
    fact_sources=[]
)

# Hydrate Tasks: broad, loose threshold, downranking instead of rejection
HYDRATE_TASKS_CONFIG = StrategyConfig(
    name="HYDRATE_TASKS",
    threshold=0.5,
    top_k=10,
    weights=DEFAULT_WEIGHTS,
    gate_mode="soft",
    semantic_enabled=True,
    semantic_requires_anchor=False,
    fact_sources=[]
)

# Hydrate Memories: standard recall
HYDRATE_MEMORIES_CONFIG = StrategyConfig(
    name="HYDRATE_MEMORIES",
    threshold=0.6,
    top_k=5,
    weights=DEFAULT_WEIGHTS,
    gate_mode="soft",
    semantic_enabled=True,
    semantic_requires_anchor=False,
    fact_sources=[]
)

# BrainSynth: deep recall, exploratory, no gates
BRAIN_SYNTH_CONFIG = StrategyConfig(
    name="BRAIN_SYNTH",
    threshold=0.5,
    top_k=30,
    weights=DEFAULT_WEIGHTS,
    gate_mode="none",
    semantic_enabled=True,
    semantic_requires_anchor=False,
    fact_sources=[]
)

