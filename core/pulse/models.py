"""Data contracts for the Pulse Engine.

Extracted from core/pulse/engine.py to define clear input/output boundaries.
"""
from typing import List, Optional
from pydantic import BaseModel, Field


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
    parent_project_name: Optional[str] = None


class NewPerson(BaseModel):
    name: str
    role: Optional[str] = None
    strategic_weight: Optional[int] = 5


class ResourceItem(BaseModel):
    url: str
    title: Optional[str] = None
    summary: Optional[str] = None
    cluster_name: Optional[str] = None
    project_name: Optional[str] = None
    strategic_note: Optional[str] = None


class LogEntry(BaseModel):
    entry_type: str
    content: str


class NewTask(BaseModel):
    title: str
    project_name: Optional[str] = None
    priority: Optional[str] = None
    estimated_duration: Optional[int] = 15
    reminder_at: Optional[str] = None
    is_revenue_critical: Optional[bool] = False


class PulseOutput(BaseModel):
    """Structured output from the LLM briefing call.

    The LLM returns this as structured JSON. The briefing field is the
    primary narrative; other fields are advisory (the Action Planner
    handles actual task creation on the webhook path).
    """
    completed_task_ids: List[CompletedTask] = Field(default_factory=list)
    new_projects: List[NewProject] = Field(default_factory=list)
    new_people: List[NewPerson] = Field(default_factory=list)
    new_tasks: List[NewTask] = Field(default_factory=list)
    resources: List[ResourceItem] = Field(default_factory=list)
    logs: List[LogEntry] = Field(default_factory=list)
    new_clusters: List[str] = Field(default_factory=list)
    briefing: str = ""
