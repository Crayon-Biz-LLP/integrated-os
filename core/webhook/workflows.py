import json
import re
from datetime import datetime, timezone
from core.services.db import get_supabase
from core.lib.audit_logger import audit_log_sync
from core.llm.fallback import generate_content_with_fallback
from core.llm.config import WorkloadProfile
from core.webhook.classify import CLASSIFICATION_MODEL
from core.webhook.telegram import send_telegram
from core.lib.conversation import log_exchange

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
    
    # 1. Deterministic phrase matching (fast path)
    decision = get_deterministic_decision(text)
    
    # 2. LLM Evaluation (slow path)
    if not decision:
        prompt = f"""You are evaluating a user's reply to a pending proposed action.
Proposed Action Type: "{w_type}"
Proposed Details: {json.dumps(payload)}

User's Reply: "{text}"

Did the user explicitly confirm/agree to proceed with the action?
Did the user explicitly decline/cancel it?
Or is this an entirely unrelated message that ignores the proposal?

Return JSON:
{{
  "decision": "confirm" | "decline" | "unrelated"
}}"""

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
            try:
                title = payload.get("title", "New Event")
                supabase.table('tasks').insert({
                    "title": title,
                    "status": "todo",
                    "priority": "normal",
                    "direction": "inbound"
                }).execute()
                reply_text = f"✅ Added '{title}' to your calendar."
            except Exception as e:
                reply_text = f"Failed to execute workflow: {e}"
                
        elif w_type == "task_creation":
            try:
                title = payload.get("title", "New Task")
                supabase.table('tasks').insert({
                    "title": title,
                    "status": "todo",
                    "priority": "normal",
                    "direction": "inbound"
                }).execute()
                reply_text = f"✅ Task created: {title}"
            except Exception as e:
                reply_text = f"Failed to execute workflow: {e}"

        await send_telegram(chat_id, reply_text)
        log_exchange(thread_id, 'user', 'WORKFLOW_REPLY', text, chat_id)
        log_exchange(thread_id, 'bot', 'WORKFLOW_RESOLUTION', reply_text, chat_id)
        return True
        
    return False
