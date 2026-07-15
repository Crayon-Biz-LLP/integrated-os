from dataclasses import dataclass, field
from typing import Optional, Literal, Union

Operation = Literal[
    "close_task",
    "suppress_instance",
    "modify_recurring",
    "cancel_recurring", 
    "create_task",
    "create_note",
    "create_event",
    "query_info",
    "reschedule",
    "update_metadata",
    "delete_event",
    "no_op"
]


@dataclass
class CreateTaskParams:
    """Typed parameters for create_task operations.

    All ID fields are resolved by the planner via LLM prompt context.
    Name fields are fallbacks when the planner can't resolve exact IDs.
    """
    title: str
    project_name: Optional[str] = None
    organization_name: Optional[str] = None
    project_id: Optional[str] = None
    organization_id: Optional[str] = None
    deadline: Optional[str] = None
    reminder_at: Optional[str] = None
    priority: str = "important"
    duration_mins: int = 15
    recurrence: Optional[str] = None
    direction: str = "inbound"
    committed_to: Optional[str] = None


@dataclass
class CreateNoteParams:
    """Typed parameters for create_note operations."""
    content: str
    project_name: Optional[str] = None
    project_id: Optional[str] = None
    organization_id: Optional[str] = None


@dataclass
class CreateEventParams:
    """Typed parameters for create_event operations."""
    title: str
    time: str
    duration_mins: int = 30


@dataclass
class Action:
    operation: Operation
    target_id: Optional[Union[int, str]] = None
    params: dict = field(default_factory=dict)
    confidence: float = 1.0
    human_label: str = ""
    project_id: Optional[str] = None
    organization_id: Optional[str] = None
