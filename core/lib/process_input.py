"""Typed intake contract and validation for process_single_dump.

Leaf module — no dependency on webhook or agents packages.
"""
import re
import hashlib
from dataclasses import dataclass
from typing import Literal

from core.services.google_service import format_rfc3339


@dataclass
class ProcessInput:
    """Typed intake contract for the canonical text-processing pipeline.

    Category-specific fields are enforced by normalize_and_validate():
    - NOTE: memory_type, expires_at
    - TASK: title, reminder_at, priority, duration_mins, recurrence,
            direction, committed_to, project_name
    - RESOURCE: url
    """
    category: Literal["NOTE", "TASK", "RESOURCE"]
    text: str
    source: str
    idempotency_key: str | None = None
    memory_type: str = "note"
    expires_at: str | None = None
    title: str | None = None
    project_name: str | None = None
    reminder_at: str | None = None
    priority: str = "important"
    duration_mins: int = 15
    recurrence: str | None = None
    direction: str = "inbound"
    committed_to: str | None = None
    url: str | None = None


class InvalidInput(ValueError):
    ...


def normalize_and_validate(input: ProcessInput) -> ProcessInput:
    """Normalize fields and enforce category-specific constraints.

    Raises InvalidInput on violations. Returns a new ProcessInput with
    all fields cleaned, normalised, and category-safe.
    """
    category = input.category.upper().strip()
    if category not in ("NOTE", "TASK", "RESOURCE"):
        raise InvalidInput(f"Unknown category: {category}")

    text = input.text.strip()
    source = (input.source or "unknown").strip().lower()
    priority = (input.priority or "important").strip().lower()
    if priority not in ("urgent", "important", "normal", "low"):
        priority = "important"
    duration_mins = max(1, input.duration_mins if input.duration_mins is not None else 15)

    title = None
    project_name = None
    reminder_at = None
    recurrence = None
    direction = None
    committed_to = None
    url = None
    memory_type = None
    expires_at = None

    if category == "TASK":
        title = (input.title or "").strip()
        if not title:
            title = text[:80].strip() if text else ""
        if not title:
            raise InvalidInput("TASK requires a non-empty title or text")
        project_name = (input.project_name or "").strip() or None
        if input.reminder_at:
            reminder_at = format_rfc3339(input.reminder_at)
        recurrence = input.recurrence
        direction = (input.direction or "inbound").strip().lower()
        committed_to = (input.committed_to or "").strip() or None
    elif category == "NOTE":
        memory_type = (input.memory_type or "note").strip().lower()
        expires_at = input.expires_at
    elif category == "RESOURCE":
        url = (input.url or "").strip()
        if not url:
            match = re.search(r'https?://\S+', text)
            if match:
                url = match.group(0).rstrip('.,;:!?)"\'')
        if not url:
            raise InvalidInput("RESOURCE requires a URL")

    idempotency_key = input.idempotency_key
    if not idempotency_key:
        dedup_raw = f"{source}:{text}:{title or ''}:{project_name or ''}"
        idempotency_key = f"auto:{hashlib.md5(dedup_raw.encode()).hexdigest()[:16]}"

    return ProcessInput(
        category=category,
        text=text,
        source=source,
        idempotency_key=idempotency_key,
        memory_type=memory_type if category == "NOTE" else "note",
        expires_at=expires_at,
        title=title,
        project_name=project_name,
        reminder_at=reminder_at,
        priority=priority,
        duration_mins=duration_mins,
        recurrence=recurrence,
        direction=direction or "inbound",
        committed_to=committed_to,
        url=url,
    )
