import asyncio
import json
from typing import List
from core.actions.models import Action
from core.services.db import get_supabase
from core.lib.audit_logger import audit_log_sync
from core.llm.fallback import generate_content_with_fallback
from core.llm.config import WorkloadProfile
from core.llm.constants import CLASSIFICATION_MODEL, SYNTHESIS_MODEL

async def plan_actions(text: str, title: str, entity: str, active_anchor: dict = None, exclude_signal_types: list = None) -> List[Action]:
    supabase = get_supabase()
    
    # 1. Fetch active tasks (todo/in_progress)
    tasks_res = supabase.table("tasks").select("id, title, status, recurrence").eq("is_current", True).not_.in_("status", ["done", "cancelled"]).execute()
    open_tasks = tasks_res.data or []
    
    # 2. Fetch recurring tasks (even if done, because done means skip instance)
    recurring_res = supabase.table("tasks").select("id, title, status, recurrence").eq("is_current", True).neq("recurrence", "").neq("recurrence", "none").execute()
    recurring_tasks = [t for t in (recurring_res.data or []) if t["status"] != "cancelled"]
    
    # 3. Fetch upcoming calendar events
    from core.services.google_service import get_upcoming_calendar_events
    upcoming_events = await asyncio.to_thread(get_upcoming_calendar_events, 14)
    
    # Combine uniquely for tasks
    seen_tasks = set()
    candidates = []
    for t in open_tasks + recurring_tasks:
        if t["id"] not in seen_tasks:
            seen_tasks.add(t["id"])
            candidates.append({"type": "task", "id": t["id"], "title": t["title"], "status": t["status"], "recurrence": t.get("recurrence")})
            
    seen_events = set()
    for e in upcoming_events:
        if e["id"] not in seen_events:
            seen_events.add(e["id"])
            candidates.append({"type": "event", "id": e["id"], "title": e["title"], "time": e["time"]})
            
    if not candidates:
        return [Action(operation="no_op", human_label="No tasks or events available to act on")]

    # Pre-filter lexically to save tokens
    title_lower = title.lower()
    text_lower = text.lower()
    search_words = set(title_lower.split() + text_lower.split())
    
    filtered_candidates = []
    for c in candidates:
        candidate_words = c["title"].lower().split()
        # Keep if any meaningful word overlaps
        if any(w in candidate_words for w in search_words if len(w) > 3):
            filtered_candidates.append(c)
            
    if not filtered_candidates:
        # Fallback to all if strict filter fails
        filtered_candidates = candidates[:50] 
        
    candidate_lines = []
    for c in filtered_candidates:
        if c["type"] == "task":
            candidate_lines.append(f"Task ID {c['id']}: {c['title']} (status: {c['status']}, recurring: {bool(c.get('recurrence'))})")
        else:
            candidate_lines.append(f"Event ID {c['id']}: {c['title']} (time: {c['time']})")
    candidate_lines_str = "\n".join(candidate_lines)
    
    from datetime import datetime, timezone
    current_time = datetime.now(timezone.utc).astimezone().isoformat()

    prompt = f"""You are an action planner. Match the user's request to the correct tasks/events and operations.
Return ONLY valid JSON: {{"actions": [{{"operation": "close_task|cancel_recurring|suppress_instance|modify_recurring|reschedule|update_metadata|delete_event|no_op", "target_id": "123", "params": {{"new_reminder_at": "YYYY-MM-DDTHH:MM:SS"}}, "human_label": "Description"}}]}}

CURRENT TIME: {current_time}

User text: "{text}"
Extracted intent title: "{title}"
Entity: "{entity}"

Candidates:
{candidate_lines_str}

Rules:
- close_task: marks a normal Task as done.
- suppress_instance: skips the next occurrence of a recurring Task.
- cancel_recurring: ends a recurring Task entirely.
- modify_recurring: changes the schedule of a recurring Task (`params.new_rrule` and `params.new_reminder_at`).
- reschedule: changes the time of a non-recurring Task (`params.new_reminder_at`).
- update_metadata: changes priority or deadline of a Task (`params.new_priority`, `params.new_deadline`).
- delete_event: removes an external Event (use when target is an Event ID).
- target_id MUST be from the Candidates list (use the Task ID or Event ID).
- Return empty array or no_op if nothing matches."""

    for model in (CLASSIFICATION_MODEL, SYNTHESIS_MODEL):
        try:
            res = await generate_content_with_fallback(
                prompt=prompt,
                workload=WorkloadProfile.INTERACTIVE,
                primary_model=model,
                config={"response_mime_type": "application/json"}
            )
            parsed = res.parse_json()
            raw_actions = parsed.get("actions", [])
            
            actions = []
            for a in raw_actions:
                op = a.get("operation", "no_op")
                tid = a.get("target_id")
                
                # We do minimal validation here since IDs can be strings (events) or ints (tasks)
                if str(tid) == "None" and op != "no_op":
                    continue
                    
                actions.append(Action(
                    operation=op,
                    target_id=tid,
                    params=a.get("params", {}),
                    human_label=a.get("human_label", "")
                ))
                
            if actions:
                return actions
        except Exception as e:
            audit_log_sync("planner", "WARNING", f"Planner failed with {model}: {e}")
            
    return []
