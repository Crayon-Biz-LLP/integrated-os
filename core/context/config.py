from dataclasses import dataclass
from typing import Literal, List
from core.retrieval.ranking import WeightConfig, DEFAULT_WEIGHTS

@dataclass
class StrategyConfig:
    name: str
    threshold: float
    top_k: int
    weights: WeightConfig
    gate_mode: Literal["hard", "soft", "none"]
    semantic_enabled: bool
    semantic_requires_anchor: bool  # If True, semantic search only runs if named anchors exist
    fact_sources: List[Literal["tasks", "people", "emails"]]
    
# Pre-Flight: ultra conservative, semantic only if anchored
PRE_FLIGHT_CONFIG = StrategyConfig(
    name="PRE_FLIGHT",
    threshold=0.7,
    top_k=3,
    weights=DEFAULT_WEIGHTS,
    gate_mode="hard",
    semantic_enabled=True,
    semantic_requires_anchor=True,
    fact_sources=["tasks", "people", "emails"]
)

# Briefing: blended, grounded
BRIEFING_CONFIG = StrategyConfig(
    name="BRIEFING",
    threshold=0.7,
    top_k=8,
    weights=DEFAULT_WEIGHTS,
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
    weights=DEFAULT_WEIGHTS,
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

