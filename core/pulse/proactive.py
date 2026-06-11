from core.webhook.utils import supabase
from core.lib.audit_logger import audit_log_sync

async def check_proactive_signals(entity_name: str) -> str:
    """Check for pending drafts or tasks related to the active anchor entity."""
    if not entity_name:
        return ""
        
    signals = []
    try:
        # Check email drafts
        drafts_res = supabase.table('email_drafts').select('id').eq('status', 'pending').ilike('draft_body', f'%{entity_name}%').execute()
        if drafts_res.data:
            signals.append(f"You have {len(drafts_res.data)} unsent email draft(s) mentioning {entity_name}.")
            
        # Check email pending tasks
        email_tasks_res = supabase.table('messages').select('id').eq('channel', 'email').is_('danny_decision', 'null').ilike('suggested_title', f'%{entity_name}%').execute()
        if email_tasks_res.data:
            signals.append(f"You have {len(email_tasks_res.data)} pending email task(s) related to {entity_name}.")
            
        # Check whatsapp pending
        wa_res = supabase.table('messages').select('id').eq('channel', 'whatsapp').eq('processing_status', 'pending').ilike('body', f'%{entity_name}%').execute()
        if wa_res.data:
            signals.append(f"You have {len(wa_res.data)} pending WhatsApp message(s) mentioning {entity_name}.")
            
    except Exception as e:
        audit_log_sync("proactive", "WARNING", f"Signal check failed for {entity_name}: {e}")
        
    if signals:
        return "💡 **Proactive Note:**\n" + "\n".join(f"- {s}" for s in signals)
    return ""
