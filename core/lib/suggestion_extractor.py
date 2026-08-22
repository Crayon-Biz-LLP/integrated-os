import asyncio
import logging
import re
from typing import Optional, List, Tuple

from pydantic import ValidationError

from core.actions.models import (
    Action,
    NeedsClarification,
    PLAN_ACTION_ADAPTER,
    inject_deterministic_delta,
    inject_deterministic_title,
    validation_missing_fields,
)
from core.llm.fallback import generate_content_with_fallback
from core.llm.config import WorkloadProfile
from core.llm.constants import CLASSIFICATION_MODEL, SYNTHESIS_MODEL
from core.services.db import tenant_aware_client
from core.lib.audit_logger import audit_log_sync
from core.lib.time_utils import get_user_timezone, resolve_relative_dates, tz_label, tz_offset_str

logger = logging.getLogger(__name__)

# Schema combines the planner's action schema with the suggestion card fields
SUGGESTION_SCHEMA = {
    "type": "object",
    "properties": {
        "document_type": {"type": "string"},
        "summary": {"type": "string"},
        "matched_task_id": {"type": ["integer", "null"]},
        "actions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "operation": {
                        "type": "string",
                        "enum": [
                            "create_task", "create_note", "create_event", "query_info",
                            "close_task", "cancel_recurring", "suppress_instance",
                            "modify_recurring", "reschedule", "update_metadata",
                            "delete_event", "no_op"
                        ]
                    },
                    "target_id": {"type": "string"},
                    "params": {"type": "object"},
                    "human_label": {"type": "string"},
                    "confidence": {"type": "number"}
                },
                "required": ["operation"]
            }
        }
    },
    "required": ["document_type", "summary", "actions"]
}

def build_unified_prompt(
    current_time: str,
    text: str,
    title: str,
    intent: str | None,
    entity: str,
    candidate_lines: str,
    org_lines: str,
    active_anchor: dict = None,
    resolved_dates: str = "",
    learned_hints: str = ""
) -> str:
    tz_lbl = tz_label()
    tz_off = tz_offset_str()

    thread_context = ""
    if active_anchor:
        anchor_name = active_anchor.get('name', '')
        anchor_type = active_anchor.get('type', '')
        anchor_org_id = active_anchor.get('last_org_id')
        parts = []
        if anchor_name:
            parts.append(f"Entity: {anchor_name}")
        if anchor_type:
            parts.append(f"Type: {anchor_type}")
        if anchor_org_id:
            parts.append(f"Organization ID: {anchor_org_id}")
        if parts:
            thread_context = "\nTHREAD CONTEXT: " + " | ".join(parts)

    resolved_section = resolved_dates.strip() if resolved_dates and resolved_dates.strip() else "- None detected."
    learned_section = learned_hints.strip() if learned_hints and learned_hints.strip() else ""
    learned_block = f"\nLEARNED FROM PAST CLARIFICATIONS (MUST-FOLLOW):\n{learned_section}" if learned_section else ""

    return f"""You are an action planner and content extractor. Match the user's request to the correct tasks/events and operations, and extract a summary.
Return ONLY valid JSON matching the schema.

CURRENT TIME: {current_time}
{thread_context}

TIME FORMATTING RULES:
- All times MUST be in {tz_lbl} (UTC{tz_off}) using ISO-8601 format.
- "today 3pm" → YYYY-MM-DDT15:00:00{tz_off}
- "tomorrow" → set params.deadline to the date (YYYY-MM-DD) and return null for reminder_at.
- "next Friday 2pm" → compute the date of next Friday and output YYYY-MM-DDT14:00:00{tz_off}
- Relative deltas ("defer by 7 days"): do NOT compute the date yourself. Output params.time_delta = {{"amount": N, "unit": "days|weeks", "direction": "later|earlier"}}.
- If a RESOLVED_RELATIVE_DATES entry is shown, output that absolute date in params.new_reminder_at instead of computing it.
- If no time is given, return null for reminder_at. Set params.deadline to the date instead. Do not invent a time.

RESOLVED_RELATIVE_DATES:
{resolved_section}{learned_block}

User text: "{text}"
Extracted intent title: "{title}"
Classifier intent: "{intent or 'UNKNOWN'}"
Entity: "{entity}"

Candidates (Existing Tasks/Events):
{candidate_lines}

Available Organizations:
{org_lines}

Rules for actions:
- close_task: marks a normal Task as done.
- suppress_instance: skips the next occurrence of a recurring Task.
- cancel_recurring: ends a recurring Task entirely.
- modify_recurring: changes the schedule of a recurring Task.
- reschedule: changes the time of a non-recurring Task.
- update_metadata: changes priority or deadline.
- delete_event: removes an external Event.
- create_task: creates a new task. Requires params.title. For ID resolution, include params.organization_id from the lists above.
- create_note: saves information to memory. Requires params.content.
- create_event: schedules a calendar event. Requires params.title, params.time.
- query_info: fetches information from the brain.
- target_id MUST be the exact numeric ID for existing Tasks, or string ID for existing Events. Not used for create operations.
- IMPORTANT: If the request refers to an existing task from the Candidates list, set "matched_task_id" to its numeric ID, and use operations like reschedule/update_metadata/close_task instead of create_task. If it's a new task, matched_task_id should be null.
- For NOTE intent → create_note. For TASK intent → create_task. For COMPLETION → close_task.
- Return empty array or no_op for actions if nothing matches.
- Document Type: <invoice|meeting_minutes|contract|report|receipt|proposal|message|other>
- Summary: <2-3 sentence summary>
"""

async def extract_suggestions(text: str, title: str = "", entity: str = "", active_anchor: dict = None, intent: str = None) -> Tuple[List[Action], Optional[dict]]:
    """Parse content into structured actions and entities, absorbing planner logic.
    Returns:
        (actions_list, suggestion_dict)
        suggestion_dict contains document_type, summary, matched_task_id
    """
    if not text or not text.strip():
        return [], None

    supabase = tenant_aware_client()
    
    # --- DETERMINISTIC PRE-FILTER ---
    _mark_done_match = re.search(r'[Mm]ark\s+task\s+(\d+)\s+as\s+done', text.strip())
    if _mark_done_match and intent == "COMPLETION":
        task_id_str = _mark_done_match.group(1)
        try:
            task_id = int(task_id_str)
            task_check = supabase.table("tasks").select("id, status").eq("id", task_id).limit(1).execute()
            if task_check.data:
                if task_check.data[0]["status"] == "done":
                    return [], None
                return [Action(operation="close_task", target_id=task_id, params={}, human_label=f"Close task {task_id}")], None
        except (ValueError, TypeError):
            pass
            
    # Context Gathering
    tasks_res = supabase.table("tasks").select("id, title, status, recurrence, google_event_id, graph_nodes(label)").eq("is_current", True).not_.in_("status", ["done", "cancelled"]).execute()
    open_tasks = tasks_res.data or []
    
    recurring_res = supabase.table("tasks").select("id, title, status, recurrence, google_event_id, graph_nodes(label)").eq("is_current", True).neq("recurrence", "").neq("recurrence", "none").execute()
    recurring_tasks = [t for t in (recurring_res.data or []) if t["status"] != "cancelled"]
    
    from core.services.google_service import get_upcoming_calendar_events
    upcoming_events = await asyncio.to_thread(get_upcoming_calendar_events, 14)
    
    try:
        from core.services.outlook_service import get_outlook_calendar_events_range
        from datetime import datetime, timezone, timedelta
        now = datetime.now(timezone.utc)
        outlook_ev = await asyncio.to_thread(get_outlook_calendar_events_range, now, now + timedelta(days=14))
        upcoming_events.extend(outlook_ev)
    except Exception:
        pass
        
    base_id_to_time = {}
    for e in upcoming_events:
        base_id = re.sub(r'_\d{8}T\d{6}Z$', '', e["id"])
        if base_id not in base_id_to_time:
            base_id_to_time[base_id] = e["time"]
            
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
            org_name = t.get("graph_nodes", {}).get("label") if t.get("graph_nodes") else None
            candidates.append({
                "type": "task", "id": t["id"], "title": t["title"], "status": t["status"], 
                "recurrence": t.get("recurrence"), "next_occurrence": next_occ, "organization_name": org_name
            })
            
    seen_events = set()
    for e in upcoming_events:
        base_id = re.sub(r'_\d{8}T\d{6}Z$', '', e["id"])
        if base_id in task_google_event_ids:
            continue
        if e["id"] not in seen_events:
            seen_events.add(e["id"])
            candidates.append({"type": "event", "id": e["id"], "title": e["title"], "time": e["time"]})
            
    orgs_res = supabase.table("graph_nodes").select("id, label").eq("type", "organization").eq("is_current", True).execute()
    orgs = orgs_res.data or []
    org_lines = "\n".join([f"  - {o['label']} (ID: {o['id']})" for o in orgs]) if orgs else "  - (none)"

    # Lexical pre-filter
    title_lower = title.lower()
    text_lower = text.lower()
    search_words = set(title_lower.split() + text_lower.split())
    
    filtered_candidates = []
    for c in candidates:
        candidate_words = c["title"].lower().split()
        if c.get("organization_name"):
            candidate_words.extend(c["organization_name"].lower().split())
        if any(w in candidate_words for w in search_words if len(w) >= 3):
            filtered_candidates.append(c)
            
    if not filtered_candidates and intent != "COMPLETION":
        text_lower = text.lower()
        for c in candidates:
            title_lower = c["title"].lower()
            if any(w in title_lower for w in search_words if len(w) >= 2 and w not in ('a', 'an', 'the', 'to', 'in', 'on', 'at', 'of', 'for', 'by', 'is', 'it', 'my', 'be')):
                filtered_candidates.append(c)
                
    if not filtered_candidates and intent == "COMPLETION":
        return [Action(operation="create_note", params={"content": text}, human_label=text[:80])], None
        
    if not filtered_candidates:
        filtered_candidates = candidates[:50]
        
    candidate_lines = []
    for c in filtered_candidates:
        if c["type"] == "task":
            rec_str = "recurring" if c.get('recurrence') else "one-off"
            next_str = f", next: {c['next_occurrence']}" if c.get('next_occurrence') else ""
            ctx_str = f" [{c.get('organization_name')}]" if c.get("organization_name") else ""
            candidate_lines.append(f"Task ID {c['id']}: {c['title']}{ctx_str} (status: {c['status']}, {rec_str}{next_str})")
        else:
            candidate_lines.append(f"Event ID {c['id']}: {c['title']} (no linked task, time: {c['time']})")
            
    from datetime import datetime
    current_time = datetime.now(get_user_timezone()).isoformat()
    resolved_text = resolve_relative_dates(text, datetime.now(get_user_timezone()))
    resolved_dates = resolved_text if resolved_text != text else ""
    
    from core.lib.learning_hints import get_action_planner_hint
    learned_hints = await get_action_planner_hint()

    prompt = build_unified_prompt(
        current_time=current_time, text=text, title=title, intent=intent, entity=entity,
        candidate_lines="\n".join(candidate_lines), org_lines=org_lines,
        active_anchor=active_anchor, resolved_dates=resolved_dates, learned_hints=learned_hints
    )

    try:
        planner_model = SYNTHESIS_MODEL if intent == "COMPLETION" else CLASSIFICATION_MODEL
        res = await generate_content_with_fallback(
            prompt=prompt,
            workload=WorkloadProfile.INTERACTIVE,
            primary_model=planner_model,
            config={
                "response_mime_type": "application/json",
                "response_schema": SUGGESTION_SCHEMA,
            }
        )
        parsed = res.parse_json()
        raw_actions = parsed.get("actions", [])
        
        actions = []
        for a in raw_actions:
            op = a.get("operation", "no_op")
            tid = a.get("target_id")
            if str(tid) == "None" and not op.startswith("create_") and op not in ["query_info", "no_op"]:
                continue
            
            a = inject_deterministic_delta(a, text)
            a = inject_deterministic_title(a, title, text)
            
            try:
                typed_action = PLAN_ACTION_ADAPTER.validate_python(a)
                actions.append(typed_action)
            except ValidationError as ve:
                missing = validation_missing_fields(ve.errors())
                raise NeedsClarification(
                    message=f"Planner produced an invalid {op} action: {ve.errors()[:3]}",
                    text=text, operation=op, target_id=tid, missing_fields=missing or None
                )
                
        # Format the suggestion dict for the card
        suggestion_dict = None
        if parsed.get("document_type") and parsed.get("summary"):
            suggestion_dict = {
                "document_type": parsed.get("document_type"),
                "summary": parsed.get("summary"),
                "matched_task_id": parsed.get("matched_task_id"),
                "suggested_actions": raw_actions
            }
            
        if actions:
            audit_log_sync("suggestion_extractor", "INFO", f"Generated {len(actions)} actions")
            
        return actions, suggestion_dict
        
    except NeedsClarification:
        raise
    except Exception as e:
        audit_log_sync("suggestion_extractor", "WARNING", f"Extraction failed: {e}")
        return [], None
