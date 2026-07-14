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
class Action:
    operation: Operation
    target_id: Optional[Union[int, str]] = None
    params: dict = field(default_factory=dict)
    confidence: float = 1.0
    human_label: str = ""
