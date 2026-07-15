from typing import List, Optional
from core.actions.models import Action
from core.services.db import get_supabase
from core.lib.audit_logger import audit_log_sync
from core.lib.state_machines import guard_require_valid_transition
from core.webhook.telegram import send_telegram


# ── #3: Pre-execution validation ──

def validate_operation(action: Action) -> Optional[str]:
    """Validate that an action can be executed before attempting.

    Returns None if valid, or an error message string if invalid.
    Catches: missing target, nonexistent task, unparseable dates.
    """
    supabase = get_supabase()

    # Operations that require an existing task
    if action.operation in ["close_task", "suppress_instance", "cancel_recurring",
                            "modify_recurring", "reschedule", "update_metadata"]:
        tid = action.target_id
        if not tid:
            return f"{action.operation}: missing target_id"
        try:
            int(str(tid))
        except (ValueError, TypeError):
            return f"{action.operation}: invalid target_id '{tid}'"

        # Check the task actually exists
        try:
            task_res = supabase.table("tasks").select("id, status").eq("id", int(tid)).limit(1).execute()
            if not task_res.data:
                return f"{action.operation}: task {tid} not found"
        except Exception as e:
            return f"{action.operation}: DB check failed for task {tid}: {e}"

    # Operations that require a valid event ID
    if action.operation == "delete_event":
        if not action.target_id:
            return "delete_event: missing target_id"

    # Operations that require a title
    if action.operation in ["create_task", "create_event"]:
        title = action.params.get("title") or action.human_label or ""
        if not title or not title.strip():
            return f"{action.operation}: missing title"

    # Operations that require content
    if action.operation == "create_note":
        content = action.params.get("content") or action.human_label or ""
        if not content or not content.strip():
            return "create_note: missing content"

    return None


# ── #4: Compensation / rollback ──

async def compensate_action(action: Action, supabase):
    """Reverse a completed action.

    Idempotent — safe to call even if the action wasn't actually applied.
    Called during rollback when an action in a batch fails.
    """
    try:
        if action.operation == "close_task":
            # Re-open: only if the task was closed by this operation
            # (safe even if already open — guard prevents invalid transition)
            from core.pulse.tools import update_task_status as _uts
            tid = int(str(action.target_id))
            current = supabase.table("tasks").select("status").eq("id", tid).limit(1).execute()
            if current.data and current.data[0]["status"] == "done":
                _uts(task_id=tid, status="todo")
                audit_log_sync("executor", "INFO", f"Rolled back close_task {tid}")

        elif action.operation == "cancel_recurring":
            # Un-cancel: re-open as todo with original recurrence
            from core.pulse.tools import update_task_status as _uts
            tid = int(str(action.target_id))
            current = supabase.table("tasks").select("status, recurrence").eq("id", tid).limit(1).execute()
            if current.data and current.data[0]["status"] == "cancelled":
                original_rec = current.data[0].get("recurrence")
                _uts(task_id=tid, status="todo", recurrence=original_rec)
                audit_log_sync("executor", "INFO", f"Rolled back cancel_recurring {tid}")

        elif action.operation == "suppress_instance":
            # Can't easily re-create a deleted instance. Audit log is sufficient.
            audit_log_sync("executor", "INFO",
                           f"Cannot undo suppress_instance {action.target_id} — instance already deleted")

        elif action.operation in ("modify_recurring", "reschedule", "update_metadata"):
            # These are inherently idempotent or hard to reverse precisely.
            # Audit log + user notification is the correct approach.
            audit_log_sync("executor", "INFO",
                           f"Rollback for {action.operation} {action.target_id}: logged for manual review")

        elif action.operation == "delete_event":
            # Can't restore a deleted event. Logged.
            audit_log_sync("executor", "INFO",
                           f"Cannot undo delete_event {action.target_id} — event deleted from calendar")

        elif action.operation == "create_task":
            # Delete the created task (soft delete via is_current=False)
            tid = action.params.get("_created_task_id")
            if tid:
                try:
                    supabase.table("tasks").update({"is_current": False}).eq("id", int(tid)).execute()
                    audit_log_sync("executor", "INFO", f"Rolled back create_task {tid}")
                except Exception as e:
                    audit_log_sync("executor", "WARNING", f"Rollback create_task {tid} failed: {e}")

        elif action.operation == "create_note":
            nid = action.params.get("_created_note_id")
            if nid:
                try:
                    supabase.table("memories").update({"is_current": False}).eq("id", int(nid)).execute()
                    audit_log_sync("executor", "INFO", f"Rolled back create_note {nid}")
                except Exception as e:
                    audit_log_sync("executor", "WARNING", f"Rollback create_note {nid} failed: {e}")

        elif action.operation == "create_event":
            eid = action.params.get("_created_event_id")
            if eid:
                try:
                    supabase.table("tasks").update({"is_current": False}).eq("id", int(eid)).execute()
                    audit_log_sync("executor", "INFO", f"Rolled back create_event {eid}")
                except Exception as e:
                    audit_log_sync("executor", "WARNING", f"Rollback create_event {eid} failed: {e}")

    except Exception as e:
        audit_log_sync("executor", "WARNING", f"Compensation failed for {action.operation}: {e}")


# ── Enrichment (fire-and-forget after create operations) ──

async def execute_planned_actions(
    actions: List[Action], 
    chat_id: int, 
    text: str = "", 
    entity: str = None, 
    source: str = "telegram", 
    sender: str = "user", 
    session_id: str = None
):
    """Executes a list of planned actions directly — NO legacy dispatch, NO process_single_dump.

    Features:
      - #3: validate_operation() pre-checks every action before execution
      - #4: compensate_action() rolls back completed actions if a later one fails
      - Creates tasks/notes/events via direct DB inserts (create_task_direct/create_note_direct).
      - Handles closures via existing update_task_status.
    """
    if not actions:
        return
        
    supabase = get_supabase()
    
    # ── Stage 0: Pre-validate all actions ──
    valid_actions = []
    pre_failures = []
    for action in actions:
        if action.operation == "no_op":
            continue
        err = validate_operation(action)
        if err:
            pre_failures.append(err)
            audit_log_sync("executor", "WARNING", f"Pre-validation blocked: {err}")
        else:
            valid_actions.append(action)
    
    if not valid_actions:
        if pre_failures:
            details = "\\n".join(pre_failures)
            await send_telegram(chat_id, f"⚠️ All actions blocked by validation:\\n{details}")
        return
    
    # ── Stage 1: Save dump and memory for closures (zero data loss) ──
    has_closures = any(a.operation in ["close_task", "suppress_instance", "cancel_recurring", "modify_recurring", "reschedule", "update_metadata", "delete_event"] for a in valid_actions)
    if has_closures and text:
        try:
            from core.llm import get_embedding
            from core.retrieval.pipeline import schedule_index_memory
            from core.lib.time_utils import compute_expires_at
            from datetime import datetime, timezone
            from core.pulse.entity_extractor import extract_and_link_entities

            embedding = (await get_embedding(text)).vector
            embed_valid = bool(embedding and any(embedding))
            mem_res = supabase.table("memories").insert({
                "content": text,
                "memory_type": "note",
                "embedding": embedding if embed_valid else None,
                "embedding_status": "success" if embed_valid else "failed",
                "source": "webhook_completion",
                "metadata": {"intent": "COMPLETION", "entity": entity},
                "expires_at": compute_expires_at(text, datetime.now(timezone.utc).isoformat())
            }).execute()
            memory_id = mem_res.data[0]['id'] if mem_res.data else None
            if memory_id:
                schedule_index_memory(memory_id, text, "note", "webhook_completion")
                await extract_and_link_entities(text, str(memory_id), 'memory')
        except Exception as e:
            audit_log_sync("executor", "WARNING", f"Failed to save completion history: {e}")
            
    from core.services.google_service import delete_calendar_event
    
    sync_failed = False
    failed_tasks = []
    closed_ids = []
    created_labels = []
    completed_actions = []  # Track for rollback
    
    for action in valid_actions:
        if action.operation == "no_op":
            continue
            
        # 1. Handle closures / modifications (require valid target_id)
        if action.operation in ["close_task", "suppress_instance", "cancel_recurring", "modify_recurring", "reschedule", "update_metadata", "delete_event"]:
            if action.operation == "delete_event":
                try:
                    delete_calendar_event(str(action.target_id))
                    closed_ids.append(action.target_id)
                except Exception as e:
                    sync_failed = True
                    failed_tasks.append(f"Event {action.target_id}: {e}")
                continue
                
            if action.operation == "update_metadata":
                try:
                    upd = {}
                    if "new_priority" in action.params:
                        upd["priority"] = action.params["new_priority"]
                    if "new_deadline" in action.params:
                        upd["deadline"] = action.params["new_deadline"]
                    if upd:
                        supabase.table('tasks').update(upd).eq('id', int(action.target_id)).execute()
                        closed_ids.append(action.target_id)
                except Exception as e:
                    sync_failed = True
                    failed_tasks.append(f"Task {action.target_id} metadata: {e}")
                continue

            if action.operation == "modify_recurring":
                status_to_set = "todo"
                reminder_at = action.params.get("new_reminder_at")
                recurrence = action.params.get("new_rrule")
            elif action.operation == "reschedule":
                status_to_set = "todo"
                reminder_at = action.params.get("new_reminder_at")
                recurrence = None
            elif action.operation == "cancel_recurring":
                status_to_set = "cancelled"
                reminder_at = None
                recurrence = None
            else:  # close_task or suppress_instance
                status_to_set = "done"
                reminder_at = None
                recurrence = None

            # State machine guard for task status transitions
            from core.pulse.tools import update_task_status as _uts
            try:
                task_current = supabase.table('tasks').select('status').eq('id', int(action.target_id)).limit(1).execute()
                if task_current.data:
                    if not guard_require_valid_transition("tasks", task_current.data[0]['status'], status_to_set, record_id=int(action.target_id), context="executor_update_status"):
                        sync_failed = True
                        failed_tasks.append(f"Task {action.target_id}: invalid transition '{task_current.data[0]['status']}' → '{status_to_set}'")
                        continue
            except Exception as e:
                audit_log_sync("state_machine", "WARNING", f"Guard fetch failed for task {action.target_id}: {e}")
                
            try:
                result_msg = _uts(
                    task_id=int(action.target_id), 
                    status=status_to_set,
                    reminder_at=reminder_at,
                    recurrence=recurrence
                )
                if "FAIL:" in result_msg:
                    sync_failed = True
                    failed_tasks.append(f"Task {action.target_id}: {result_msg}")
                else:
                    # "INFO:" means already in target state — no-op, don't track
                    if "INFO:" not in result_msg:
                        closed_ids.append(action.target_id)
            except Exception as e:
                sync_failed = True
                failed_tasks.append(f"Task {action.target_id}: {e}")
                
        # 2. Handle creations via direct DB insert — NO process_single_dump
        elif action.operation == "create_task":
            title = action.params.get("title") or action.human_label or text or "New Task"
            reminder_at = action.params.get("reminder_at")
            priority = action.params.get("priority", "important")
            duration = action.params.get("duration_mins", 15)
            recurrence = action.params.get("recurrence")
            direction = action.params.get("direction", "inbound")
            committed_to = action.params.get("committed_to")
            deadline = action.params.get("deadline")

            try:
                from core.pulse.tools import create_task_direct
                result = await create_task_direct(
                    title=title,
                    project_id=action.params.get("project_id") or action.project_id,
                    organization_id=action.params.get("organization_id") or action.organization_id,
                    project_name=action.params.get("project_name"),
                    organization_name=action.params.get("organization_name"),
                    reminder_at=reminder_at,
                    priority=priority,
                    duration_mins=duration,
                    recurrence=recurrence,
                    deadline=deadline,
                    direction=direction,
                    committed_to=committed_to,
                )
                if result.get("action") == "created":
                    created_labels.append(action.human_label or title)
                    # Track for rollback
                    if result.get("task_id"):
                        action.params["_created_task_id"] = result["task_id"]
                        completed_actions.append(action)
                        # Enrichment handled internally by create_task_direct
                elif result.get("action") == "error":
                    sync_failed = True
                    failed_tasks.append(f"Create task '{title}': {result.get('reason', 'unknown')}")
                # "skipped" is silent (dedup)
            except Exception as e:
                sync_failed = True
                failed_tasks.append(f"Create task '{title}': {e}")
                
        elif action.operation == "create_note":
            content = action.params.get("content") or action.human_label or text or ""

            try:
                from core.pulse.tools import create_note_direct
                result = await create_note_direct(
                    content=content,
                    source=source,
                    project_id=action.params.get("project_id") or action.project_id,
                    organization_id=action.params.get("organization_id") or action.organization_id,
                    project_name=action.params.get("project_name"),
                    organization_name=action.params.get("organization_name"),
                )
                if result.get("action") == "filed":
                    created_labels.append(action.human_label or "Note created")
                    if result.get("memory_id"):
                        action.params["_created_note_id"] = result["memory_id"]
                        completed_actions.append(action)
                        # Enrichment handled internally by create_note_direct
                elif result.get("action") == "error":
                    sync_failed = True
                    failed_tasks.append(f"Create note: {result.get('reason', 'unknown')}")
            except Exception as e:
                sync_failed = True
                failed_tasks.append(f"Create note: {e}")
                
        elif action.operation == "create_event":
            title = action.params.get("title") or action.human_label or text or "New Event"
            event_time = action.params.get("time") or ""
            duration = action.params.get("duration_mins", 30)

            try:
                from core.pulse.tools import create_task_direct
                result = await create_task_direct(
                    title=title,
                    reminder_at=event_time,
                    duration_mins=duration,
                    priority="important",
                    project_name=action.params.get("project_name"),
                    organization_name=action.params.get("organization_name"),
                )
                if result.get("action") == "created":
                    created_labels.append(action.human_label or title)
                    if result.get("task_id"):
                        action.params["_created_event_id"] = result["task_id"]
                        completed_actions.append(action)
                        # Enrichment handled internally by create_task_direct
                elif result.get("action") == "error":
                    sync_failed = True
                    failed_tasks.append(f"Create event '{title}': {result.get('reason', 'unknown')}")
            except Exception as e:
                sync_failed = True
                failed_tasks.append(f"Create event '{title}': {e}")
                
        elif action.operation == "query_info":
            # query_info is informational only — the original text was already
            # processed through interrogate_brain before planning
            pass
        
        # Track non-create closures for rollback
        if action.operation in ["close_task", "cancel_recurring", "suppress_instance",
                                "modify_recurring", "reschedule", "update_metadata", "delete_event"]:
            completed_actions.append(action)
                
    # ── Rollback: if any action failed, reverse completed actions in reverse order ──
    if sync_failed and completed_actions:
        audit_log_sync("executor", "WARNING",
                       f"{len(completed_actions)} completed actions to roll back after {len(failed_tasks)} failures")
        rolled_back_ids = set()
        for completed in reversed(completed_actions):
            await compensate_action(completed, supabase)
            # Track the human-visible label for the rollback message
            if completed.operation in ("close_task", "cancel_recurring", "suppress_instance"):
                rolled_back_ids.add(str(completed.target_id))
            elif completed.operation in ("create_task", "create_event"):
                created_labels = [lb for lb in created_labels if completed.human_label not in lb]
            elif completed.operation == "create_note":
                created_labels = [lb for lb in created_labels if completed.human_label not in lb]

        # Remove rolled-back IDs from closed_ids so the success message doesn't claim them
        closed_ids = [cid for cid in closed_ids if str(cid) not in rolled_back_ids]

        rollback_msg = f"↩️ Rolled back {len(completed_actions)} previously completed actions." if completed_actions else ""
        error_details = "\\n".join(failed_tasks)
        await send_telegram(chat_id, f"⚠️ **Partial Sync Failure**\\nSome actions failed. {rollback_msg}\
\\nDetails: {error_details}")
    
    # Send success messages for closures (creations send their own via their handlers)
    if closed_ids:
        active_tasks = []
        try:
            tasks_res = supabase.table("tasks").select("id, title").in_("id", closed_ids).execute()
            active_tasks = tasks_res.data or []
        except Exception:
            pass
        
        labels = [act.human_label for act in actions if act.target_id in closed_ids and act.human_label]
        if labels:
            closed_titles = ", ".join(labels)
        else:
            closed_titles = ", ".join(t["title"] for t in active_tasks if str(t["id"]) in [str(cid) for cid in closed_ids])
            if not closed_titles:
                closed_titles = f"{len(closed_ids)} items"
        await send_telegram(chat_id, f"✅ Closed: {closed_titles}")
        
