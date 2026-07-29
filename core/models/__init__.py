import contextvars
from dataclasses import dataclass, field
from typing import List, Optional, Literal

from pydantic import BaseModel, Field


ActionType = Literal["task_create", "task_update", "calendar_create", "memory_save", 
                     "workflow_propose", "draft_create", "reminder_set", "none"]
ActionStatus = Literal["executed", "queued", "proposed", "failed", "not_attempted"]


@dataclass
class ActionResult:
    action_type: ActionType = "none"
    status: ActionStatus = "not_attempted"
    entity_id: Optional[str | int] = None
    human_label: Optional[str] = None
    evidence: dict = field(default_factory=dict)


_action_results: contextvars.ContextVar[list[ActionResult]] = contextvars.ContextVar('action_results', default=[])


def accumulate_action(result: ActionResult):
    lst = _action_results.get()
    lst.append(result)
    _action_results.set(lst)


class CompletedTask(BaseModel):
    id: int
    status: str
    reminder_at: Optional[str] = None
    duration_mins: Optional[int] = None


class NewProject(BaseModel):
    name: str
    importance: Optional[int] = 5
    context: Optional[str] = "work"
    description: Optional[str] = None
    keywords: Optional[List[str]] = Field(default_factory=list)


class NewPerson(BaseModel):
    name: str
    role: Optional[str] = None
    strategic_weight: Optional[int] = 5


class ResourceItem(BaseModel):
    url: str
    title: Optional[str] = None
    summary: Optional[str] = None
    cluster_name: Optional[str] = None
    strategic_note: Optional[str] = None


class LogEntry(BaseModel):
    entry_type: str
    content: str


class NewTask(BaseModel):
    title: str
    priority: Optional[str] = None
    estimated_duration: Optional[int] = 15
    reminder_at: Optional[str] = None
    is_revenue_critical: Optional[bool] = False


class PulseOutput(BaseModel):
    completed_task_ids: List[CompletedTask] = Field(default_factory=list)
    new_projects: List[NewProject] = Field(default_factory=list)
    new_people: List[NewPerson] = Field(default_factory=list)
    new_tasks: List[NewTask] = Field(default_factory=list)
    resources: List[ResourceItem] = Field(default_factory=list)
    logs: List[LogEntry] = Field(default_factory=list)
    new_clusters: List[str] = Field(default_factory=list)
    briefing: str
