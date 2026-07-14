"""
completion_handler.py
Owns the full lifecycle of COMPLETION dumps.
State machine: processing_completion -> awaiting_completion_match | completed | partially_synced
"""
from core.llm.constants import CLASSIFICATION_MODEL
from core.llm import get_embedding
import asyncio

from core.lib.audit_logger import audit_log_sync
from core.lib.time_utils import compute_expires_at
from core.webhook.utils import supabase
from core.retrieval.pipeline import schedule_index_memory
from core.services.db import maybe_single_safe
from core.decisions import record_decision
from datetime import datetime, timezone
from core.actions import ActionResult, accumulate_action
from core.actions.planner import plan_actions

# ── Constants ─────────────────────────────────────────────────────────────────
COMPLETION_MESSAGE_TYPE = "completion"
STATUS_PROCESSING       = "processing_completion"
STATUS_AWAITING         = "awaiting_completion_match"
STATUS_COMPLETED        = "completed"
STATUS_PARTIAL          = "partially_synced"

async def handle_confident_completion(
    text: str,
    title: str,
    chat_id: int,
    receipt: str = None,
    entity: str = None,
    source: str = "telegram",
    sender: str = "user",
    exclude_signal_types: list = None
):
    dump_id = None
    try:
        # ── Stage 1: Insert with canonical message_type immediately ──────────
        dump_res = supabase.table("raw_dumps").insert({
            "content":      text,
            "status":       STATUS_PROCESSING,
            "is_processed": False,
            "direction":    "incoming",
            "sender":       sender,
            "source":       source,
            "message_type": COMPLETION_MESSAGE_TYPE,
            "metadata": {
                "intent": "COMPLETION",
                "title":  title,
                "entity": entity,
            },
        }).execute()
        dump_id = dump_res.data[0]["id"] if dump_res.data else None

        # ── Stage 2: Embed + write to memories immediately (zero data loss) ──
        embedding = (await get_embedding(text)).vector
        embed_valid = bool(embedding and any(embedding))
        memory_id = None
        try:
            mem_res = supabase.table("memories").insert({
                "content":          text,
                "memory_type":      "note",
                "embedding":        embedding if embed_valid else None,
                "embedding_status": "success" if embed_valid else "failed",
                "source":           "webhook_completion",
                "metadata": {
                    "intent": "COMPLETION",
                    "entity": entity,
                },
                "expires_at": compute_expires_at(text, datetime.now(timezone.utc).isoformat())
            }).execute()
            memory_id = mem_res.data[0]['id'] if mem_res.data else None
            accumulate_action(ActionResult(action_type="memory_save", status="executed", entity_id=memory_id, human_label="Completion logged"))
            schedule_index_memory(memory_id, text, "note", "webhook_completion")
            if memory_id:
                from core.pulse.entity_extractor import extract_and_link_entities
                await extract_and_link_entities(text, str(memory_id), 'memory')
        except Exception as mem_err:
            audit_log_sync("completion", "WARNING", f"Memory write failed for dump {dump_id}: {mem_err}")
            pass

        # ── Stage 3: Planner resolves actions ─────────────────────────────────
        actions = await plan_actions(text, title, entity, exclude_signal_types=exclude_signal_types)
        
        # Filter valid actions
        valid_actions = [a for a in actions if a.operation != "no_op" and a.target_id]
        
        if not valid_actions:
            # Fallback to PROJECT_UPDATE
            supabase.table("raw_dumps").update({
                "status": "processed", 
                "is_processed": True,
                "message_type": "note",
                "metadata": {
                    "intent": "PROJECT_UPDATE",
                    "title": title,
                    "entity": entity,
                    "degraded_from_completion": True,
                    "park_reason": "no_match_found_by_planner"
                }
            }).eq("id", dump_id).execute()
            
            if memory_id:
                supabase.table("memories").update({
                    "metadata": {
                        "intent": "PROJECT_UPDATE",
                        "entity": entity,
                        "degraded_from_completion": True
                    }
                }).eq("id", memory_id).execute()
            
            from core.webhook.dispatch import _run_post_capture_enrichment
            followup_msg = await _run_post_capture_enrichment(
                text, chat_id, None, None, None,
                receipt=receipt, enable_workflow=False, active_anchor=None,
                memory_id=memory_id, exclude_signal_types=exclude_signal_types
            )
            ack = receipt or "✅ Logged as update (no matching task found)."
            await _send(chat_id, f"{ack}{followup_msg}")
            return
            
        # Extract task IDs to pass to legacy execute_completion_closure for now
        # (This keeps the change small while achieving the fix)
        task_ids = [a.target_id for a in valid_actions]
        
        # We need the task metadata for execute_completion_closure
        active_tasks = []
        if task_ids:
            tasks_res = supabase.table("tasks").select("id, title").in_("id", task_ids).eq("is_current", True).execute()
            active_tasks = tasks_res.data or []

        # Wait, if operation is cancel_recurring, execute_completion_closure will call update_task_status(status="done").
        # Wait, update_task_status("done") for recurring task currently skips an instance. 
        # If we want to cancel the series, we need status="cancelled".
        # Let's handle ops here:
        from core.pulse.tools import update_task_status
        sync_failed = False
        closed_ids = []
        failed_tasks = []
        
        for action in valid_actions:
            if action.operation != "delete_event":
                try:
                    int(str(action.target_id))
                except (ValueError, TypeError):
                    audit_log_sync("completion", "ERROR", f"Invalid target_id for {action.operation}: {action.target_id}")
                    failed_tasks.append(f"Invalid target_id for {action.operation}: {action.target_id}")
                    sync_failed = True
                    continue

            if action.operation == "delete_event":
                from core.services.google_service import delete_calendar_event
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
                    if "new_priority" in action.params: upd["priority"] = action.params["new_priority"]
                    if "new_deadline" in action.params: upd["deadline"] = action.params["new_deadline"]
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
                if "Error" in result_msg:
                    sync_failed = True
                    failed_tasks.append(f"Task {action.target_id}: {result_msg}")
                else:
                    closed_ids.append(action.target_id)
            except Exception as e:
                sync_failed = True
                failed_tasks.append(f"Task {action.target_id}: {e}")
                
        if sync_failed:
            _park(dump_id, STATUS_PARTIAL, "sync_failed")
            error_details = "\\n".join(failed_tasks)
            await _send(chat_id, f"⚠️ **Partial Sync Failure**\\nSome tasks couldn't be closed due to an external error. Please check manually:\\n\\n{error_details}")
        else:
            _park(dump_id, STATUS_COMPLETED, None, is_processed=True)
            
        if closed_ids:
            # Use the LLM's human_label if provided, otherwise fallback
            labels = [a.human_label for a in valid_actions if a.target_id in closed_ids and a.human_label]
            if labels:
                closed_titles = ", ".join(labels)
            else:
                closed_titles = ", ".join(t["title"] for t in active_tasks if t["id"] in closed_ids)
                if not closed_titles:
                    closed_titles = f"{len(closed_ids)} items"
            await _send(chat_id, f"✅ Handled: {closed_titles}")

    except Exception as e:
        audit_log_sync("completion", "ERROR", f"handle_confident_completion fatal: {e}")
        if dump_id:
            _park(dump_id, STATUS_AWAITING, f"fatal_error: {str(e)[:100]}")
        await _send(chat_id, "Completion received. Had trouble matching — parked for review.")


def _park(dump_id, status, reason=None, is_processed=False):
    """Single update point for all dump state transitions."""
    if not dump_id:
        return
    update = {"status": status, "is_processed": is_processed}
    if reason:
        try:
            existing = maybe_single_safe(supabase.table("raw_dumps").select("metadata").eq("id", dump_id))
            meta = (existing.data or {}).get("metadata") or {}
            meta["park_reason"] = reason
            update["metadata"] = meta
        except Exception:
            pass
    supabase.table("raw_dumps").update(update).eq("id", dump_id).execute()


async def _send(chat_id, message):
    from core.webhook.telegram import send_telegram
    await send_telegram(chat_id, message)
