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
from core.prompts.planner import build_planner_prompt

async def plan_actions(text: str, title: str = "", entity: str = "", active_anchor: dict = None, intent: str = None) -> List[Action]:
    supabase = get_supabase()
    
    # --- DETERMINISTIC PRE-FILTER: "Mark task N as done" → close_task ---
    # Same pattern as the classify pre-filter. Extracts the task ID directly
    # from "Mark task 123 as done" and creates a close_task Action without
    # any LLM call. This is the only reliable way to handle task closures
    # — LLMs consistently fail to generate close_task with the correct target_id.
    _mark_done_match = re.search(r'[Mm]ark\s+task\s+(\d+)\s+as\s+done', text.strip())
    if _mark_done_match and intent == "COMPLETION":
        task_id_str = _mark_done_match.group(1)
        print(f"[PLANNER_DEBUG] Pre-filter matched! text={text!r}, task_id={task_id_str}, intent={intent}")
        try:
            task_id = int(task_id_str)
            task_check = supabase.table("tasks").select("id, status").eq("id", task_id).limit(1).execute()
            if task_check.data:
                if task_check.data[0]["status"] == "done":
                    audit_log_sync("planner", "INFO", f"Task {task_id} already done — skipping close_task")
                    return []
                audit_log_sync("planner", "INFO", f"Deterministic close_task for task {task_id} (pre-filter match)")
                return [Action(
                    operation="close_task",
                    target_id=task_id,
                    params={},
                    human_label=f"Close task {task_id}"
                )]
            else:
                audit_log_sync("planner", "WARNING", f"Task {task_id} not found for close_task (pre-filter)")
        except (ValueError, TypeError):
            audit_log_sync("planner", "WARNING", f"Invalid task ID in close text: '{task_id_str}'")
    
    # ── Fetch all independent data sources ──
    from core.services.google_service import get_upcoming_calendar_events
    tasks_res = await supabase.table("tasks").select("id, title, status, recurrence, google_event_id, projects(name), organizations(name)").eq("is_current", True).not_.in_("status", ["done", "cancelled"]).execute()
    recurring_res = await supabase.table("tasks").select("id, title, status, recurrence, google_event_id, projects(name), organizations(name)").eq("is_current", True).neq("recurrence", "").neq("recurrence", "none").execute()
    upcoming_events_raw = await asyncio.to_thread(get_upcoming_calendar_events, 14)
    orgs_res = await supabase.table("organizations").select("id, name").execute()
    projects_all_res = await supabase.table("projects").select("id, name, organization_id, organizations(name)").eq("is_current", True).neq("status", "archived").execute()
    
    # Handle transient failures gracefully — one failed fetch shouldn't crash the whole plan
    if isinstance(tasks_res, Exception):
        audit_log_sync("planner", "WARNING", f"plan_actions: open tasks fetch failed: {tasks_res}")
        tasks_res = type('obj', (object,), {'data': []})()
    if isinstance(recurring_res, Exception):
        audit_log_sync("planner", "WARNING", f"plan_actions: recurring tasks fetch failed: {recurring_res}")
        recurring_res = type('obj', (object,), {'data': []})()
    if isinstance(upcoming_events_raw, Exception):
        audit_log_sync("planner", "WARNING", f"plan_actions: calendar events fetch failed: {upcoming_events_raw}")
        upcoming_events_raw = []
    if isinstance(orgs_res, Exception):
        audit_log_sync("planner", "WARNING", f"plan_actions: orgs fetch failed: {orgs_res}")
        orgs_res = type('obj', (object,), {'data': []})()
    if isinstance(projects_all_res, Exception):
        audit_log_sync("planner", "WARNING", f"plan_actions: projects fetch failed: {projects_all_res}")
        projects_all_res = type('obj', (object,), {'data': []})()
    
    open_tasks = tasks_res.data or []
    upcoming_events = upcoming_events_raw
    recurring_tasks = [t for t in (recurring_res.data or []) if t["status"] != "cancelled"]
    
    # Pre-process upcoming events into base IDs to find next occurrence times
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
            proj_name = t.get("projects", {}).get("name") if t.get("projects") else None
            org_name = t.get("organizations", {}).get("name") if t.get("organizations") else None
            
            candidates.append({
                "type": "task", 
                "id": t["id"], 
                "title": t["title"], 
                "status": t["status"], 
                "recurrence": t.get("recurrence"),
                "next_occurrence": next_occ,
                "project_name": proj_name,
                "organization_name": org_name
            })
    
    seen_events = set()
    for e in upcoming_events:
        base_id = re.sub(r'_\d{8}T\d{6}Z$', '', e["id"])
        if base_id in task_google_event_ids:
            continue
        if e["id"] not in seen_events:
            seen_events.add(e["id"])
            candidates.append({"type": "event", "id": e["id"], "title": e["title"], "time": e["time"]})
    
    orgs = orgs_res.data or []
    org_lines = "\n".join([f"  - {o['name']} (ID: {o['id']})" for o in orgs]) if orgs else "  - (none)"
    
    projects_all = projects_all_res.data or []
    project_lines = []
    for p in projects_all:
        org_name = p.get('organizations', {}).get('name', 'INBOX') if p.get('organizations') else 'INBOX'
        project_lines.append(f"  - {p['name']} (ID: {p['id']}, org: {org_name})")
    project_lines_str = "\n".join(project_lines) if project_lines else "  - (none)"
    

    # Pre-filter lexically to save tokens
    title_lower = title.lower()
    text_lower = text.lower()
    search_words = set(title_lower.split() + text_lower.split())
    
    filtered_candidates = []
    for c in candidates:
        candidate_words = c["title"].lower().split()
        if c.get("project_name"):
            candidate_words.extend(c["project_name"].lower().split())
        if c.get("organization_name"):
            candidate_words.extend(c["organization_name"].lower().split())
            
        if any(w in candidate_words for w in search_words if len(w) > 3):
            filtered_candidates.append(c)
    
    # GAP A: No lexical matches for COMPLETION → deterministic redirect to create_note
    # If the classifier returned COMPLETION but NO open task's title shares even
    # one content-bearing keyword with the message, the user is stating a milestone
    # about something that was completed — not closing a specific open task.
    # 
    # This is the planner-level counterpart to Guard 1 in classify.py.
    # It catches cases where Guard 1's DB query failed (fail-open) and the
    # classifier fell through to COMPLETION. Instead of calling the LLM
    # (which will return no_op), immediately save as a note.
    if not filtered_candidates and intent == "COMPLETION":
        audit_log_sync("planner", "INFO",
                       f"Gap A: zero lexical matches for COMPLETION → create_note ({text[:60]}...)")
        return [Action(
            operation="create_note",
            params={"content": text},
            human_label=text[:80]
        )]
            
    if not filtered_candidates:
        filtered_candidates = candidates[:50]
        
    candidate_lines = []
    for c in filtered_candidates:
        if c["type"] == "task":
            rec_str = "recurring" if c.get('recurrence') else "one-off"
            next_str = f", next: {c['next_occurrence']}" if c.get('next_occurrence') else ""
            org_proj = []
            if c.get("organization_name"):
                org_proj.append(f"org: {c['organization_name']}")
            if c.get("project_name"):
                org_proj.append(f"proj: {c['project_name']}")
            ctx_str = f" [{', '.join(org_proj)}]" if org_proj else ""
            
            candidate_lines.append(f"Task ID {c['id']}: {c['title']}{ctx_str} (status: {c['status']}, {rec_str}{next_str})")
        else:
            candidate_lines.append(f"Event ID {c['id']}: {c['title']} (no linked task, time: {c['time']})")
            
    candidate_lines_str = "\n".join(candidate_lines)
    
    from datetime import datetime, timezone
    current_time = datetime.now(timezone.utc).astimezone().isoformat()

    prompt = build_planner_prompt(
        current_time=current_time,
        text=text,
        title=title,
        intent=intent,
        entity=entity,
        candidate_lines=candidate_lines_str,
        org_lines=org_lines,
        project_lines=project_lines_str,
        active_anchor=active_anchor,
    )

    try:
        # Use SYNTHESIS_MODEL for COMPLETION intents (close_task needs reliable matching)
        planner_model = SYNTHESIS_MODEL if intent == "COMPLETION" else CLASSIFICATION_MODEL
        res = await generate_content_with_fallback(
            prompt=prompt,
            workload=WorkloadProfile.INTERACTIVE,
            primary_model=planner_model,
            config={"response_mime_type": "application/json"}
        )
        parsed = res.parse_json()
        raw_actions = parsed.get("actions", [])
        
        actions = []
        for a in raw_actions:
            op = a.get("operation", "no_op")
            tid = a.get("target_id")
            
            if str(tid) == "None" and not op.startswith("create_") and op not in ["query_info", "no_op"]:
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
        return []
    except Exception as e:
        audit_log_sync("planner", "WARNING", f"Planner failed: {e}")
        return []
