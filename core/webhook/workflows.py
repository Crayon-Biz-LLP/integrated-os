import re
from datetime import datetime, timezone
from core.services.db import get_supabase
from core.lib.audit_logger import audit_log_sync
from core.llm.fallback import generate_content_with_fallback
from core.llm.config import WorkloadProfile
from core.webhook.classify import CLASSIFICATION_MODEL
from core.webhook.telegram import send_telegram
from core.lib.conversation import log_exchange, _check_topic_overlap
from core.actions import ActionResult, accumulate_action

CONFIRM_PHRASES = {'yes', 'y', 'yep', 'do it', 'go ahead', 'sure', 'ok', 'okay', 'yeah', 'please', 'absolutely'}
DECLINE_PHRASES = {'no', 'n', 'nope', 'cancel', 'skip', 'nevermind', 'ignore', 'stop'}
NEGATION_WORDS = {'not', "n't", 'never'}

def get_deterministic_decision(text: str):
    cleaned = re.sub(r'[^\w\s]', '', text.lower()).strip()
    if cleaned in CONFIRM_PHRASES:
        return 'confirm'
    if cleaned in DECLINE_PHRASES:
        return 'decline'

    words = cleaned.split()
    if len(words) <= 4:
        has_negation = any(w in NEGATION_WORDS for w in words)
        has_confirm = any(w in CONFIRM_PHRASES for w in words)
        has_decline = any(w in DECLINE_PHRASES for w in words)
        if has_confirm and not has_decline and not has_negation:
            return 'confirm'
        if has_decline and not has_confirm and not has_negation:
            return 'decline'

    return None

async def check_and_resume_workflow(chat_id: int, text: str, thread_id: str) -> bool:
    """
    Checks if there's an active workflow for this chat.
    If so, evaluates the user's reply to see if it confirms, declines, or ignores the workflow.
    Returns True if the workflow handled the message, False if normal routing should proceed.
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

    try:
        res = supabase.table('conversation_workflows') \
            .select('*') \
            .eq('chat_id', chat_id) \
            .eq('status', 'active') \
            .eq('awaiting_user_input', True) \
            .execute()
    except Exception as e:
        audit_log_sync("workflow", "ERROR", f"DB lookup failure falling open to general: {e}")
        return False
        
    if not res.data:
        return False
        
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
        return False
        
    workflow = res.data[0]
    w_id = workflow['id']
    w_type = workflow['workflow_type']
    payload = workflow.get('payload') or {}
    
    # 0. Deterministic topical relevance guard (before any LLM call)
    if text and not _check_topic_overlap(text, payload):
        audit_log_sync("workflow", "INFO",
            f"Workflow {w_id} bypassed: message entities don't match workflow payload — falling through")
        return False

    # 1. Deterministic phrase matching (fast path)
    decision = get_deterministic_decision(text)
    
    # 2. LLM Evaluation (slow path)
    if not decision:
        from core.prompts.workflow import build_workflow_resume_prompt
        prompt = build_workflow_resume_prompt(w_type, payload, text)

        try:
            analysis_res = await generate_content_with_fallback(
                prompt=prompt,
                workload=WorkloadProfile.INTERACTIVE,
                primary_model=CLASSIFICATION_MODEL,
                config={'response_mime_type': 'application/json'}
            )
            decision = analysis_res.parse_json().get("decision", "unrelated")
        except Exception as e:
            audit_log_sync("workflow", "ERROR", f"LLM eval failed falling open: {e}")
            return False
            
    # 3. Handle Decision
    if decision == "unrelated":
        # AVOID CANCELLING: Let the user answer later. Just fall open.
        audit_log_sync("workflow", "INFO", f"Workflow {w_id} bypassed due to unrelated reply. Remains active.")
        return False
        
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
            return True
    except Exception as e:
        audit_log_sync("workflow", "ERROR", f"Failed atomic update for {w_id}: {e}")
        return False
        
    if decision == "decline":
        reply_text = "Cancelled."
        await send_telegram(chat_id, reply_text)
        log_exchange(thread_id, 'user', 'WORKFLOW_REPLY', text, chat_id)
        log_exchange(thread_id, 'bot', 'WORKFLOW_RESOLUTION', reply_text, chat_id)
        return True
        
    elif decision == "confirm":
        reply_text = "Done."
        
        if w_type == "calendar_event":
            event_id = payload.get("google_event_id")
            sentinel = payload.get("_calendar_sentinel")
            if event_id:
                reply_text = "Already created."
            elif sentinel:
                # RECOVERY PATH: Sentinel is true, but no event_id.
                # Could be a duplicate webhook hit, OR Google succeeded but DB failed.
                audit_log_sync("workflow", "INFO", f"Sentinel found but no event_id for workflow {w_id}. Attempting recovery.")
                from core.services.google_service import get_calendar_event_by_idempotency_key
                existing_event = get_calendar_event_by_idempotency_key(f"enrichment:{w_id}")
                if existing_event:
                    # Heal the DB
                    supabase.table('conversation_workflows').update({
                        "payload": {**payload, "google_event_id": existing_event["id"]}
                    }).eq('id', w_id).execute()
                    reply_text = "Already created (recovered from interruption)."
                else:
                    # Sentinel is true but event never made it to Google. We can safely retry creation.
                    dt_iso = payload.get("datetime_iso")
                    if dt_iso:
                        try:
                            from core.services.google_service import create_calendar_event
                            event = create_calendar_event(
                                title=payload.get("proposed_title", payload.get("title", "Meeting")),
                                start_iso=dt_iso,
                                duration_minutes=payload.get("duration_minutes", 30),
                                description=payload.get("description", ""),
                                idempotency_key=f"enrichment:{w_id}"
                            )
                            supabase.table('conversation_workflows').update({
                                "payload": {**payload, "_calendar_sentinel": True, "google_event_id": event.get("id")}
                            }).eq('id', w_id).execute()
                            accumulate_action(ActionResult(action_type="calendar_create", status="executed", entity_id=event.get("id"), human_label=payload.get("title")))
                            reply_text = "Done. Calendar event created (recovered)."
                        except Exception as e:
                            audit_log_sync("workflow", "ERROR", f"Recovery creation failed: {e}")
                            reply_text = "Failed to create calendar event during recovery."
                    else:
                        reply_text = "Failed to create calendar event: missing datetime."
            else:
                dt_iso = payload.get("datetime_iso")
                if dt_iso:
                    # PRE-COMMIT SENTINEL
                    try:
                        payload["_calendar_sentinel"] = True
                        supabase.table('conversation_workflows').update({
                            "payload": payload
                        }).eq('id', w_id).execute()
                    except Exception as e:
                        audit_log_sync("workflow", "ERROR", f"Failed to write sentinel for {w_id}: {e}")
                        return False  # Abort before Google call
                        
                    try:
                        from core.services.google_service import create_calendar_event
                        event = create_calendar_event(
                            title=payload.get("proposed_title", payload.get("title", "Meeting")),
                            start_iso=dt_iso,
                            duration_minutes=payload.get("duration_minutes", 30),
                            description=payload.get("description", ""),
                            idempotency_key=f"enrichment:{w_id}"
                        )
                        # POST-COMMIT EVENT ID
                        supabase.table('conversation_workflows').update({
                            "payload": {**payload, "google_event_id": event.get("id")}
                        }).eq('id', w_id).execute()
                        accumulate_action(ActionResult(action_type="calendar_create", status="executed", entity_id=event.get("id"), human_label=payload.get("title")))
                        reply_text = "Done. Calendar event created."
                    except Exception as e:
                        accumulate_action(ActionResult(action_type="calendar_create", status="failed", evidence={"error": str(e)}))
                        audit_log_sync("workflow", "ERROR", f"Failed to create calendar event for workflow {w_id}: {e}")
                        reply_text = "Failed to create calendar event."
                else:
                    reply_text = "Failed to create calendar event: missing datetime."
                    
        elif w_type == "deadline":
            # Just create a task for now or update an existing one if possible
            try:
                title = payload.get("task_title", "New Task")
                deadline_iso = payload.get("deadline_iso")
                res = supabase.table('tasks').insert({
                    "title": title,
                    "status": "todo",
                    "priority": "important",
                    "deadline": deadline_iso,
                    "direction": "inbound"
                }).execute()
                task_id = res.data[0]['id'] if res.data else None
                accumulate_action(ActionResult(action_type="task_create", status="executed" if task_id else "failed", entity_id=task_id, human_label=title))
            except Exception as e:
                accumulate_action(ActionResult(action_type="task_create", status="failed", evidence={"error": str(e)}))

        elif w_type == "task_creation" or w_type == "awaiting_actionable_confirmation":
            try:
                title = payload.get("title", "New Item")
                res = supabase.table('tasks').insert({
                    "title": title,
                    "status": "todo",
                    "priority": "normal",
                    "direction": "inbound"
                }).execute()
                task_id = res.data[0]['id'] if res.data else None
                accumulate_action(ActionResult(
                    action_type="task_create",
                    status="executed" if task_id else "failed",
                    entity_id=task_id,
                    human_label=title
                ))
            except Exception as e:
                accumulate_action(ActionResult(
                    action_type="task_create",
                    status="failed",
                    evidence={"error": str(e)}
                ))
                
        elif w_type == "awaiting_disambiguation_confirmation":
            pass # No database mutation, just acknowledging.

        await send_telegram(chat_id, reply_text)
        log_exchange(thread_id, 'user', 'WORKFLOW_REPLY', text, chat_id)
        log_exchange(thread_id, 'bot', 'WORKFLOW_RESOLUTION', reply_text, chat_id)
        return True
        
    return False
