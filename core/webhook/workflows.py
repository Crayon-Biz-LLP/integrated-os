from datetime import datetime, timezone
from typing import Tuple, Optional
from core.services.db import get_supabase
from core.lib.audit_logger import audit_log_sync
from core.llm.fallback import generate_content_with_fallback
from core.llm.config import WorkloadProfile
from core.webhook.classify import CLASSIFICATION_MODEL
from core.webhook.telegram import send_telegram
from core.lib.conversation import log_exchange, _check_topic_overlap
from core.actions import ActionResult, accumulate_action
from core.pulse.tools import create_task_direct


async def check_and_resume_workflow(chat_id: int, text: str, thread_id: str) -> Tuple[bool, Optional[str]]:
    """
    Checks if there's an active workflow for this chat.
    If so, evaluates the user's reply to see if it confirms, declines, or ignores the workflow.
    Returns (True, None) if the message was consumed, (True, ancillary_text) if the workflow
    handled the offer but there's a separate instruction to re-process, (False, None) if normal routing.
    """
    supabase = get_supabase()
    
    # Prune expired workflows first
    try:
        now_iso = datetime.now(timezone.utc).isoformat()
        supabase.table('conversation_workflows').update({
            'status': 'expired',
            'resolved_at': now_iso,
            'updated_at': now_iso
        }).eq('chat_id', chat_id).eq('status', 'active').lt('expires_at', now_iso).execute()
    except Exception:
        pass
    
    # Always query DB directly (cache removed — DB lookup is fast and restart-safe)
    try:
        res = supabase.table('conversation_workflows') \
            .select('*') \
            .eq('chat_id', chat_id) \
            .eq('status', 'active') \
            .eq('awaiting_user_input', True) \
            .execute()
    except Exception as e:
        audit_log_sync("workflow", "ERROR", f"DB lookup failure falling open to general: {e}")
        return False, None
        
    if not res.data:
        return False, None
        
    if len(res.data) > 1:
        # Mark older ones as superseded
        sorted_ws = sorted(res.data, key=lambda x: x['created_at'])
        superseded_ids = [w['id'] for w in sorted_ws[:-1]]
        now_iso = datetime.now(timezone.utc).isoformat()
        supabase.table('conversation_workflows').update({
            'status': 'cancelled', 
            'resolved_at': now_iso,
            'updated_at': now_iso
        }).in_('id', superseded_ids).execute()
        
        audit_log_sync("workflow", "WARNING", f"Multiple active workflows for chat {chat_id}. Superseded older ones, falling open.")
        return False, None
        
    workflow = res.data[0]
    w_id = workflow['id']
    w_type = workflow['workflow_type']
    payload = workflow.get('payload') or {}
    
    # 0. Deterministic topical relevance guard (before any LLM call)
    if text and not _check_topic_overlap(text, payload):
        audit_log_sync("workflow", "INFO",
            f"Workflow {w_id} bypassed: message entities don't match workflow payload — falling through")
        return False, None

    # 1+2: Decision determination — always through LLM (deterministic bypass removed)
    from core.prompts.workflow import build_workflow_resume_prompt
    prompt = build_workflow_resume_prompt(w_type, payload, text)
    try:
        analysis_res = await generate_content_with_fallback(
            prompt=prompt,
            workload=WorkloadProfile.INTERACTIVE,
            primary_model=CLASSIFICATION_MODEL,
            config={'response_mime_type': 'application/json'}
        )
        raw = analysis_res.parse_json()
        
        if w_type == "batch":
            signal_decisions = raw.get("decisions", [])
            has_other_content = raw.get("has_other_content", False)
            other_content_text = raw.get("other_content_text", "")
            decision = "confirm" if any(sd.get("decision") == "confirm" for sd in signal_decisions) else "decline"
        else:
            decision = raw.get("decision", "unrelated")
    except Exception as e:
        audit_log_sync("workflow", "ERROR", f"LLM eval failed falling open: {e}")
        return False, None
            
    # 3. Handle Decision
    if decision == "unrelated":
        audit_log_sync("workflow", "INFO", f"Workflow {w_id} bypassed due to unrelated reply. Remains active.")
        return False, None
        
    # ATOMIC UPDATE FOR IDEMPOTENCY
    try:
        now_iso = datetime.now(timezone.utc).isoformat()
        update_res = supabase.table('conversation_workflows').update({
            'status': 'resolved' if decision == 'confirm' else 'cancelled',
            'resolved_at': now_iso,
            'updated_at': now_iso
        }).eq('id', w_id).eq('status', 'active').execute()
        
        if not update_res.data:
            audit_log_sync("workflow", "WARNING", f"Workflow {w_id} already resolved concurrently. Skipping.")
            return True, other_content_text if decision == "decline" and 'other_content_text' in locals() and other_content_text else None
    except Exception as e:
        audit_log_sync("workflow", "ERROR", f"Failed atomic update for {w_id}: {e}")
        return False, None
    
    if decision == "decline":
        has_other = 'has_other_content' in locals() and has_other_content and other_content_text.strip()
        reply_text = "Cancelled the pending items." if has_other else "Cancelled."
        await send_telegram(chat_id, reply_text)
        log_exchange(thread_id, 'user', 'WORKFLOW_REPLY', text, chat_id)
        log_exchange(thread_id, 'bot', 'WORKFLOW_RESOLUTION', reply_text, chat_id)
        if has_other:
            return True, other_content_text.strip()
        return True, None
        
    elif decision == "confirm":
        reply_text = "Done."

        if w_type == "batch":
            signals_list = payload.get("signals", [])
            # Cache active tasks once for task_closure matching
            active_tasks = []
            for sd in signal_decisions:
                if sd.get("decision") != "confirm":
                    continue
                idx = sd.get("index")
                if idx is None or idx < 0 or idx >= len(signals_list):
                    continue
                sig = signals_list[idx]
                sig_type = sig.get("type")
                title = sig.get("task_title") or sig.get("proposed_title") or sig.get("title") or sig.get("target_task_description", "") or "New Task"
                reminder_at = sig.get("reminder_at")

                if sig_type in ("deadline", "calendar_event"):
                    project_name = sig.get("project_name")
                    organization_name = sig.get("organization_name")
                    res = await create_task_direct(title=title, reminder_at=reminder_at, project_name=project_name, organization_name=organization_name)
                    if res.get("action") == "created":
                        reply_text += f"\n✅ Task created: {title}"
                elif sig_type == "task_imperative":
                    project_name = sig.get("project_name")
                    organization_name = sig.get("organization_name")
                    res = await create_task_direct(title=title, project_name=project_name, organization_name=organization_name)
                    if res.get("action") == "created":
                        reply_text += f"\n✅ Task created: {title}" 
                elif sig_type == "task_closure":
                    if not active_tasks:
                        tasks_res = supabase.table("tasks") \
                            .select("id, title") \
                            .eq("is_current", True) \
                            .not_.in_("status", ["done", "cancelled"]) \
                            .execute()
                        active_tasks = tasks_res.data or []
                    target = sig.get("target_task_description", "") or title
                    target_lower = target.lower()
                    matching = [
                        t for t in active_tasks
                        if any(word in t["title"].lower() for word in target_lower.split() if len(word) > 3)
                    ]
                    if matching:
                        from core.pulse.tools import update_task_status
                        for t in matching:
                            update_task_status(task_id=t["id"], status="done")

        elif w_type in ("deadline", "calendar_event"):
            title = payload.get("task_title") or payload.get("proposed_title") or payload.get("title", "New Task")
            reminder_at = payload.get("reminder_at")
            result = await create_task_direct(title=title, reminder_at=reminder_at)
            task_id = result.get("task_id")
            accumulate_action(ActionResult(
                action_type="task_create",
                status="executed" if task_id else "failed",
                entity_id=task_id, human_label=title))

        elif w_type == "task_creation" or w_type == "awaiting_actionable_confirmation":
            title = payload.get("title", "New Item")
            result = await create_task_direct(title=title)
            task_id = result.get("task_id")
            accumulate_action(ActionResult(
                action_type="task_create",
                status="executed" if task_id else "failed",
                entity_id=task_id, human_label=title))
                
        elif w_type == "awaiting_disambiguation_confirmation":
            pass # No database mutation, just acknowledging.

        await send_telegram(chat_id, reply_text)
        log_exchange(thread_id, 'user', 'WORKFLOW_REPLY', text, chat_id)
        log_exchange(thread_id, 'bot', 'WORKFLOW_RESOLUTION', reply_text, chat_id)
        has_other = 'has_other_content' in locals() and has_other_content and other_content_text.strip()
        if has_other:
            return True, other_content_text.strip()
        return True, None
        
    return False, None
