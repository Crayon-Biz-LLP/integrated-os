"""Data contracts for the Pulse Engine.

PulseOutput only contains the briefing narrative. The Action Planner
(core/actions/) handles all task creation, completion, and modification
on the webhook path — the Pulse Engine generates briefings only.

BriefingContext consolidates the 35+ parameters of build_pulse_briefing_prompt
into a single typed dataclass for testability and maintainability.
"""
from pydantic import BaseModel
from dataclasses import dataclass


class PulseOutput(BaseModel):
    """Structured output from the LLM briefing call.

    Only the briefing field is used. The Action Planner handles
    all task operations (create, close, modify) on the webhook path.
    """
    briefing: str = ""


@dataclass
class BriefingContext:
    """Everything the Pulse Engine needs to generate a briefing.

    Collected upfront from DB reads, then passed to the LLM as a single object.
    No writes happen during collection — this is a pure read contract.
    """
    # Time & Mode
    current_time_str: str = ""
    briefing_mode: str = ""
    is_overloaded: bool = False
    is_monday_morning: bool = False
    conversation_history: str = ""

    # Strategy & Business
    season_config: str = ""
    system_context: str = "OPERATIONAL"
    core: str = "None"
    people_names: str = "None"
    practices_context: str = ""

    # Hindsight
    is_hindsight_stale: bool = False
    hindsight_empty: bool = True

    # Tasks
    overdue_tasks_json: str = "None"
    stale_context: str = "None"
    cluster_task_list: str = "No tasks."
    urgency_lists: str = ""
    universal_task_map: str = "None"
    dependency_context: str = "None"

    # Intelligence
    calendar_context: str = ""
    recent_memories_context: str = ""
    hindsight_context: str = "None"
    weekly_patterns_str: str = ""
    graph_task_context: str = ""
    morning_pulse_narrative: str = ""
    serendipity_context: str = "None"
    canonical_context: str = ""
    social_graph_context: str = "None"
    temporal_context: str = "None"
    centrality_context: str = "None"

    # Resources
    pattern_context: str = "None"
    newly_enriched_context: str = "None"
    recent_urls_context: str = "None"
    active_clusters_context: str = "None"

    # History & Metadata
    session_memory_context: str = ""
    delta_context: str = "None"
    adaptive_context: str = "None"
    new_input_tags: str = "None"

    # Inputs
    new_inputs: str = "None"
