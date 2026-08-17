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

    The briefing field goes to Telegram (unchanged).
    The voice_line field goes to the Flutter app via app_intelligence table.
    The home_mode field drives the Flutter home screen layout
    (proceed|decide|sprint|catch_up|wrap).
    The top_focal_item field is the single most important item for the app's
    focal card — picked by the LLM using full context, not a formula.
    The Action Planner handles all task operations (create, close, modify).
    """
    briefing: str = ""
    voice_line: str = ""
    home_mode: str = "proceed"
    top_focal_item: dict = {}


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
    sample_task_id: str = "123"
    sample_task_title: str = "Complete the pending report"
    sample_task_reason: str = "This is blocking the next phase of the project."

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
