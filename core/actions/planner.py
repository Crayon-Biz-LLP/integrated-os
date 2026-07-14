import asyncio
import json
import re
from typing import List
from core.actions.models import Action
from core.services.db import get_supabase
from core.lib.audit_logger import audit_log_sync
from core.llm.fallback import generate_content_with_fallback
from core.llm.config import WorkloadProfile
from core.llm.constants import CLASSIFICATION_MODEL, SYNTHESIS_MODEL

async def plan_actions(text: str, title: str, entity: str, active_anchor: dict = None) -> List[Action]:
    supabase = get_supabase()
    
    # 1. Fetch active tasks (todo/in_progress)
    tasks_res = supabase.table("tasks").select("id, title, status, recurrence, google_event_id").eq("is_current", True).not_.in_("status", ["done", "cancelled"]).execute()
    open_tasks = tasks_res.data or []
    
    # 2. Fetch recurring tasks (even if done, because done means skip instance)
    recurring_res = supabase.table("tasks").select("id, title, status, recurrence, google_event_id").eq("is_current", True).neq("recurrence", "").neq("recurrence", "none").execute()
    recurring_tasks = [t for t in (recurring_res.data or []) if t["status"] != "cancelled"]
    
    # 3. Fetch upcoming calendar events
    from core.services.google_service import get_upcoming_calendar_events
    upcoming_events = await asyncio.to_thread(get_upcoming_calendar_events, 14)
    
    # Pre-process upcoming events into base IDs to find next occurrence times
    base_id_to_time = {}
    for e in upcoming_events:
        base_id = re.sub(r'_\d{8}T\d{6}Z$', '', e["id"])
        # Keep the earliest time for the base ID
        if base_id not in base_id_to_time:
            base_id_to_time[base_id] = e["time"]
    
    # Combine uniquely for tasks
    seen_tasks = set()
    candidates = []
    task_google_event_ids = set()
    
    for t in open_tasks + recurring_tasks:
        if t["id"] not in seen_tasks:
            seen_tasks.add(t["id"])
            gid = t.get("google_event_id")
            if gid:
                task_google_event_ids.add(gid)
            
            next_occ = base_id_to_time.get(gid) if gid else None
            candidates.append({
                "type": "task", 
                "id": t["id"], 
                "title": t["title"], 
                "status": t["status"], 
                "recurrence": t.get("recurrence"),
                "next_occurrence": next_occ
            })
            
    seen_events = set()
    for e in upcoming_events:
        base_id = re.sub(r'_\d{8}T\d{6}Z$', '', e["id"])
        if base_id in task_google_event_ids:
            continue # Event is linked to a task, already handled above
            
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
        if any(w in candidate_words for w in search_words if len(w) > 3):
            filtered_candidates.append(c)
            
    if not filtered_candidates:
        filtered_candidates = candidates[:50] 
        
    candidate_lines = []
    for c in filtered_candidates:
        if c["type"] == "task":
            rec_str = "recurring" if c.get('recurrence') else "one-off"
            next_str = f", next: {c['next_occurrence']}" if c.get('next_occurrence') else ""
            candidate_lines.append(f"Task ID {c['id']}: {c['title']} (status: {c['status']}, {rec_str}{next_str})")
        else:
            candidate_lines.append(f"Event ID {c['id']}: {c['title']} (no linked task, time: {c['time']})")
            
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
- delete_event: removes an external Event.
- create_task: creates a new task. Requires `params.title`. Optional: `params.project_name`, `params.deadline`, `params.priority`, `params.reminder_at`, `params.rrule`.
- create_note: saves information to memory. Requires `params.content`. Optional: `params.project_name`.
- create_event: schedules a calendar event. Requires `params.title`, `params.time`.
- query_info: fetches information from the brain to answer the user's question. Requires `params.query`.
- target_id MUST be the exact numeric ID for existing Tasks, or string ID for existing Events. Not used for create operations.
- Task operations (close_task, cancel_recurring, etc.) MUST use the numeric Task ID. Event IDs can ONLY be used with delete_event.
- IMPORTANT: A recurring task with status 'done' or 'todo' is STILL AN ACTIVE SERIES. 'done' only skips the current week. If the user asks to cancel a recurring series, target ALL matching recurring tasks regardless of their current status.
- If the user uses words like "all", "meetings", or "tasks" (plural), return a separate action for EVERY matching candidate.
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
                
                if str(tid) == "None" and op != "no_op":
                    continue
                    
                actions.append(Action(
                    operation=op,
                    target_id=tid,
                    params=a.get("params", {}),
                    human_label=a.get("human_label", "")
                ))
                
            if actions:
                try:
                    audit_log_sync("planner", "INFO", f"Generated {len(actions)} actions", metadata={"plan": json.dumps([{"operation": a.operation, "target_id": a.target_id} for a in actions])})
                except Exception:
                    pass
                return actions
        except Exception as e:
            audit_log_sync("planner", "WARNING", f"Planner failed with {model}: {e}")
            
    return []
