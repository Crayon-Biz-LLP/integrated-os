"""Data contracts for the Pulse Engine.

PulseOutput only contains the briefing narrative. The Action Planner
(core/actions/) handles all task creation, completion, and modification
on the webhook path — the Pulse Engine generates briefings only.
"""
from pydantic import BaseModel


class PulseOutput(BaseModel):
    """Structured output from the LLM briefing call.

    Only the briefing field is used. The Action Planner handles
    all task operations (create, close, modify) on the webhook path.
    """
    briefing: str = ""
