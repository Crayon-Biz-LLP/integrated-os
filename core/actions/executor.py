from typing import List
from core.actions.models import Action
from core.services.db import get_supabase
from core.lib.audit_logger import audit_log_sync
from core.webhook.telegram import send_telegram

async def execute_planned_actions(
    actions: List[Action], 
    chat_id: int, 
    text: str = "", 
    entity: str = None, 
    source: str = "telegram", 
    sender: str = "user", 
    session_id: str = None
):
    """Executes a list of planned actions."""
    if not actions:
        return
        
    supabase = get_supabase()
    
    # ── Stage 1: Zero Data Loss (Save dump and memory for closures) ──
    # If the user's message was primarily a closure, we need to save it since we bypass the normal handlers
    has_closures = any(a.operation in ["close_task", "suppress_instance", "cancel_recurring", "modify_recurring", "reschedule", "update_metadata", "delete_event"] for a in actions)
    if has_closures and text:
        try:
            supabase.table("raw_dumps").insert({
                "content": text,
                "status": "completed",
                "is_processed": True,
                "direction": "incoming",
                "sender": sender,
                "source": source,
                "message_type": "completion",
                "metadata": {"intent": "COMPLETION", "entity": entity},
            }).execute()
            
            from core.llm import get_embedding
            from core.retrieval.pipeline import schedule_index_memory
            from core.lib.time_utils import compute_expires_at
            from datetime import datetime, timezone
            
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
                from core.pulse.entity_extractor import extract_and_link_entities
                await extract_and_link_entities(text, str(memory_id), 'memory')
        except Exception as e:
            audit_log_sync("executor", "WARNING", f"Failed to save completion history: {e}")
            
    from core.pulse.tools import update_task_status
    from core.services.google_service import delete_calendar_event
    
    sync_failed = False
    failed_tasks = []
    closed_ids = []
    created_items = []
    
    for action in actions:
        if action.operation == "no_op":
            continue
            
        # 1. Handle closures / modifications (require target_id)
        if action.operation in ["close_task", "suppress_instance", "cancel_recurring", "modify_recurring", "reschedule", "update_metadata", "delete_event"]:
            if action.operation != "delete_event":
                try:
                    int(str(action.target_id))
                except (ValueError, TypeError):
                    audit_log_sync("executor", "ERROR", f"Invalid target_id for {action.operation}: {action.target_id}")
                    failed_tasks.append(f"Invalid target_id for {action.operation}: {action.target_id}")
                    sync_failed = True
                    continue

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
            else: # close_task or suppress_instance
                status_to_set = "done"
                reminder_at = None
                recurrence = None
                
            try:
                result_msg = update_task_status(
                    task_id=int(action.target_id), 
                    status=status_to_set,
                    reminder_at=reminder_at,
                    recurrence=recurrence
                )
                if "FAIL:" in result_msg:
                    sync_failed = True
                    failed_tasks.append(f"Task {action.target_id}: {result_msg}")
                else:
                    closed_ids.append(action.target_id)
            except Exception as e:
                sync_failed = True
                failed_tasks.append(f"Task {action.target_id}: {e}")
                
        # 2. Handle creations via delegation
        elif action.operation == "create_task":
            title = action.params.get("title") or action.human_label or text or "New Task"
            try:
                from core.webhook.dispatch import handle_confident_task
                await handle_confident_task(
                    text=text,
                    title=title,
                    time_context=action.params.get("reminder_at") or "",
                    chat_id=chat_id,
                    receipt="Task created",
                    entity=action.params.get("project_name") or entity,
                    source=source,
                    sender=sender,
                    session_id=session_id
                )
                created_items.append(action.human_label or title)
            except Exception as e:
                sync_failed = True
                failed_tasks.append(f"Create task '{title}': {e}")
                
        elif action.operation in ["create_note", "query_info"]:
            content = action.params.get("content") or action.human_label or text or ""
            try:
                from core.webhook.dispatch import handle_confident_note
                await handle_confident_note(
                    text=content,
                    chat_id=chat_id,
                    receipt="Note created",
                    source=source,
                    sender=sender,
                    entity=action.params.get("project_name") or entity,
                    session_id=session_id
                )
                created_items.append(action.human_label or "Note created")
            except Exception as e:
                sync_failed = True
                failed_tasks.append(f"Create note: {e}")
                
        elif action.operation == "create_event":
            title = action.params.get("title") or action.human_label or text or "New Event"
            time = action.params.get("time") or ""
            try:
                from core.webhook.dispatch import handle_confident_task
                await handle_confident_task(
                    text=text,
                    title=title,
                    time_context=time,
                    chat_id=chat_id,
                    receipt="Event created",
                    entity=entity,
                    source=source,
                    sender=sender,
                    session_id=session_id
                )
                created_items.append(action.human_label or title)
            except Exception as e:
                sync_failed = True
                failed_tasks.append(f"Create event '{title}': {e}")
                
    # Feedback to user
    if sync_failed:
        error_details = "\\n".join(failed_tasks)
        await send_telegram(chat_id, f"⚠️ **Partial Sync Failure**\\nSome actions couldn't be completed:\\n\\n{error_details}")
    
    # Send success messages for closures (creations send their own via their handlers)
    if closed_ids:
        active_tasks = []
        try:
            tasks_res = supabase.table("tasks").select("id, title").in_("id", closed_ids).execute()
            active_tasks = tasks_res.data or []
        except Exception:
            pass
        
        labels = [a.human_label for a in actions if a.target_id in closed_ids and a.human_label]
        if labels:
            closed_titles = ", ".join(labels)
        else:
            closed_titles = ", ".join(t["title"] for t in active_tasks if str(t["id"]) in [str(i) for i in closed_ids])
            if not closed_titles:
                closed_titles = f"{len(closed_ids)} items"
        await send_telegram(chat_id, f"✅ Closed: {closed_titles}")
        
