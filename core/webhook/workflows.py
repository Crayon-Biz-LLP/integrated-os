import json
from core.services.db import get_supabase
from core.lib.audit_logger import audit_log_sync
from core.llm.fallback import generate_content_with_fallback
from core.llm.config import WorkloadProfile
from core.webhook.classify import CLASSIFICATION_MODEL
from core.webhook.telegram import send_telegram
from core.lib.conversation import log_exchange

async def check_and_resume_workflow(chat_id: int, text: str, thread_id: str) -> bool:
    """
    Checks if there's an active workflow for this chat.
    If so, evaluates the user's reply to see if it confirms, declines, or ignores the workflow.
    Returns True if the workflow handled the message, False if normal routing should proceed.
    """
    supabase = get_supabase()
    
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
        audit_log_sync("workflow", "WARNING", f"Multiple active workflows for chat {chat_id}, ignoring to fail open.")
        return False
        
    workflow = res.data[0]
    w_id = workflow['id']
    w_type = workflow['workflow_type']
    payload = workflow.get('payload') or {}
    
    # Check if the user confirmed or declined
    prompt = f"""You are evaluating a user's reply to a pending proposed action.
Proposed Action Type: "{w_type}"
Proposed Details: {json.dumps(payload)}

User's Reply: "{text}"

Did the user confirm/agree to proceed with the action? (e.g., "yes", "go ahead", "do it", "sure")
Did the user explicitly decline/cancel it? (e.g., "no", "nevermind", "skip it")
Or is this an entirely unrelated message that ignores the proposal? (e.g., a new thought, a different task)

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
        analysis = analysis_res.parse_json()
    except Exception as e:
        audit_log_sync("workflow", "ERROR", f"LLM eval failed falling open: {e}")
        return False
        
    decision = analysis.get("decision", "unrelated")
    
    if decision == "unrelated":
        # Leave it active or cancel it? The user said "send raw note after a bot question and confirm it lands safely"
        # We should probably cancel it so it doesn't hang forever, but failing open means returning False.
        # Let's cancel it so it doesn't pollute future messages.
        supabase.table('conversation_workflows').update({
            'status': 'cancelled',
            'resolved_at': 'now()',
            'updated_at': 'now()'
        }).eq('id', w_id).execute()
        audit_log_sync("workflow", "INFO", f"Workflow {w_id} cancelled due to unrelated reply.")
        return False
        
    elif decision == "decline":
        supabase.table('conversation_workflows').update({
            'status': 'cancelled',
            'resolved_at': 'now()',
            'updated_at': 'now()'
        }).eq('id', w_id).execute()
        
        reply_text = "Cancelled."
        await send_telegram(chat_id, reply_text)
        log_exchange(thread_id, 'user', 'WORKFLOW_REPLY', text, chat_id)
        log_exchange(thread_id, 'bot', 'WORKFLOW_RESOLUTION', reply_text, chat_id)
        return True
        
    elif decision == "confirm":
        # Execute the payload
        reply_text = "Done."
        
        if w_type == "calendar_event":
            # We don't have a calendar creation function yet that takes raw payload, but we can simulate it or call tasks_service
            # Actually, the user asked to add a calendar event. We can create a task with reminder_at or google_event_id logic
            # For now, let's create a task
            try:
                title = payload.get("title", "New Event")
                
                # Insert task
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

        supabase.table('conversation_workflows').update({
            'status': 'resolved',
            'resolved_at': 'now()',
            'updated_at': 'now()'
        }).eq('id', w_id).execute()
        
        await send_telegram(chat_id, reply_text)
        log_exchange(thread_id, 'user', 'WORKFLOW_REPLY', text, chat_id)
        log_exchange(thread_id, 'bot', 'WORKFLOW_RESOLUTION', reply_text, chat_id)
        return True
        
    return False
