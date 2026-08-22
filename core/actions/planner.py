import asyncio
import json
import re
from typing import List
from pydantic import ValidationError
from core.actions.models import (
    Action,
    NeedsClarification,
    PLAN_ACTION_ADAPTER,
    inject_deterministic_delta,
    inject_deterministic_title,
    validation_missing_fields,
)
from core.services.db import tenant_aware_client
from core.lib.audit_logger import audit_log_sync
from core.llm.fallback import generate_content_with_fallback
from core.llm.config import WorkloadProfile
from core.llm.constants import CLASSIFICATION_MODEL, SYNTHESIS_MODEL
from core.prompts.planner import build_planner_prompt

async def plan_actions(text: str, title: str = "", entity: str = "", active_anchor: dict = None, intent: str = None) -> List[Action]:
    supabase = tenant_aware_client()
    
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
    
    # 1. Fetch active tasks (todo/in_progress)
    # NOTE: requires migration 75 (tasks.organization_id -> graph_nodes).
    tasks_res = supabase.table("tasks").select("id, title, status, recurrence, google_event_id, graph_nodes(label)").eq("is_current", True).not_.in_("status", ["done", "cancelled"]).execute()
    open_tasks = tasks_res.data or []
    
    # 2. Fetch recurring tasks (even if done, because done means skip instance)
    recurring_res = supabase.table("tasks").select("id, title, status, recurrence, google_event_id, graph_nodes(label)").eq("is_current", True).neq("recurrence", "").neq("recurrence", "none").execute()
    recurring_tasks = [t for t in (recurring_res.data or []) if t["status"] != "cancelled"]
    
    # 3. Fetch upcoming calendar events
    from core.services.google_service import get_upcoming_calendar_events
    upcoming_events = await asyncio.to_thread(get_upcoming_calendar_events, 14)
    
    # 3b. Fetch Outlook calendar events
    try:
        from core.services.outlook_service import get_outlook_calendar_events_range
        from datetime import datetime, timezone, timedelta
        now = datetime.now(timezone.utc)
        end_time = now + timedelta(days=14)
        outlook_ev = await asyncio.to_thread(get_outlook_calendar_events_range, now, end_time)
        upcoming_events.extend(outlook_ev)
    except Exception as e:
        print(f"Error fetching outlook events for planner: {e}")
    
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
            org_name = t.get("graph_nodes", {}).get("label") if t.get("graph_nodes") else None
            
            candidates.append({
                "type": "task", 
                "id": t["id"], 
                "title": t["title"], 
                "status": t["status"], 
                "recurrence": t.get("recurrence"),
                "next_occurrence": next_occ,
                "organization_name": org_name
            })
            
    seen_events = set()
    for e in upcoming_events:
        base_id = re.sub(r'_\d{8}T\d{6}Z$', '', e["id"])
        if base_id in task_google_event_ids:
            continue # Event is linked to a task, already handled above
            
        if e["id"] not in seen_events:
            seen_events.add(e["id"])
            candidates.append({"type": "event", "id": e["id"], "title": e["title"], "time": e["time"]})
            
    # 4. Fetch organizations for LLM resolution (consolidation: graph-first)
    orgs_res = supabase.table("graph_nodes").select("id, label").eq("type", "organization").eq("is_current", True).execute()
    orgs = orgs_res.data or []
    org_lines = "\n".join([f"  - {o['label']} (ID: {o['id']})" for o in orgs]) if orgs else "  - (none)"
    

    # Pre-filter lexically to save tokens
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
    
    # Secondary fallback: if lexical filter found nothing for non-COMPLETION intents,
    # do substring matching against full title. Catches messages like "Fix the API bug"
    # where ALL words are ≤3 chars and the primary filter yielded zero candidates.
    if not filtered_candidates and intent != "COMPLETION":
        text_lower = text.lower()
        for c in candidates:
            title_lower = c["title"].lower()
            # Check if any content-bearing word from text appears as substring in title
            if any(w in title_lower for w in search_words if len(w) >= 2 and w not in ('a', 'an', 'the', 'to', 'in', 'on', 'at', 'of', 'for', 'by', 'is', 'it', 'my', 'be')):
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
            org_context = c.get("organization_name", "")
            ctx_str = f" [{org_context}]" if org_context else ""
            
            candidate_lines.append(f"Task ID {c['id']}: {c['title']}{ctx_str} (status: {c['status']}, {rec_str}{next_str})")
        else:
            candidate_lines.append(f"Event ID {c['id']}: {c['title']} (no linked task, time: {c['time']})")
            
    candidate_lines_str = "\n".join(candidate_lines)
    
    from datetime import datetime, timezone
    current_time = datetime.now(timezone.utc).astimezone().isoformat()

    # Phase 2 (invariant #2): resolve relative date expressions deterministically
    # so the LLM never has to compute calendar math. Only the resolved copy is
    # passed to the prompt — the raw `text` is untouched (it's used verbatim
    # for notes / fallback content and the lexical candidate pre-filter).
    from core.lib.time_utils import get_user_timezone, resolve_relative_dates
    resolved_text = resolve_relative_dates(text, datetime.now(get_user_timezone()))
    resolved_dates = resolved_text if resolved_text != text else ""

    # Learning loop (vision #4): past clarifications for this tenant steer the
    # prompt so the same operation-class mistakes get rarer. Fail-open + cached.
    from core.lib.learning_hints import get_action_planner_hint
    learned_hints = await get_action_planner_hint()

    prompt = build_planner_prompt(
        current_time=current_time,
        text=text,
        title=title,
        intent=intent,
        entity=entity,
        candidate_lines=candidate_lines_str,
        org_lines=org_lines,
        active_anchor=active_anchor,
        resolved_dates=resolved_dates,
        learned_hints=learned_hints,
    )

    try:
        # Use SYNTHESIS_MODEL for COMPLETION intents (close_task needs reliable matching)
        planner_model = SYNTHESIS_MODEL if intent == "COMPLETION" else CLASSIFICATION_MODEL
        # Phase 5 (invariant #6): shape-level response schema constrains the LLM
        # at generation time on both providers; per-op required fields stay in
        # the typed models (strict backstop). Providers degrade gracefully if the
        # schema is rejected.
        from core.prompts.planner import PLANNER_ACTIONS_SCHEMA
        res = await generate_content_with_fallback(
            prompt=prompt,
            workload=WorkloadProfile.INTERACTIVE,
            primary_model=planner_model,
            config={
                "response_mime_type": "application/json",
                "response_schema": PLANNER_ACTIONS_SCHEMA,
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
                
            # Phase 2 backstop (invariant #2): the LLM reads phrasing, code does
            # arithmetic. If the LLM emitted a time-bearing op with no time, the
            # raw text is re-read deterministically and the delta injected — so
            # a "defer by 7 days" flake can never be asked about or dropped.
            a = inject_deterministic_delta(a, text)

            # Title backstop (same invariant): the LLM intermittently emits
            # create_task/create_event with no title — a title-less create is
            # blocked at the executor gate and the request silently degrades to
            # a fallback note (the S2 flake class). Re-read the text
            # deterministically instead of dropping the request.
            a = inject_deterministic_title(a, title, text)

            # Phase 1 fail-closed (invariant #3): every action must satisfy its
            # per-op schema before it can reach the executor. A malformed action
            # (e.g. reschedule with no new_reminder_at — the Aug 12 silent-ack
            # failure) raises NeedsClarification so the user is asked instead of
            # being acknowledged with zero writes.
            try:
                typed_action = PLAN_ACTION_ADAPTER.validate_python(a)
            except ValidationError as ve:
                missing = validation_missing_fields(ve.errors())
                raise NeedsClarification(
                    message=f"Planner produced an invalid {op} action: {ve.errors()[:3]}",
                    text=text,
                    operation=op,
                    target_id=tid,
                    missing_fields=missing or None,
                )
            actions.append(typed_action)
            
        if actions:
            try:
                audit_log_sync("planner", "INFO", f"Generated {len(actions)} actions", metadata={"plan": json.dumps([{"operation": a.operation, "target_id": a.target_id} for a in actions])})
            except Exception:
                pass
            return actions
        return []
    except NeedsClarification:
        # Re-raise — the dispatch layer routes it to the user as a question.
        # Must not be swallowed by the generic handler below.
        raise
    except Exception as e:
        audit_log_sync("planner", "WARNING", f"Planner failed: {e}")
        return []
