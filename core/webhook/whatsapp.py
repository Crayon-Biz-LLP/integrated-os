from datetime import datetime, timezone
from core.webhook.utils import supabase


async def process_whatsapp_pending_decision(pending_id: int, decision: str, supabase_client=None) -> dict:
    """Process approve/reject for a WhatsApp pending message.

    For 'approve': writes to raw_dumps (Pulse classifies/routes on next run).
    For 'reject': sets danny_decision='rejected'.

    Args:
        pending_id: ID in whatsapp_messages table.
        decision: 'approve' or 'reject'.
        supabase_client: Optional supabase client (defaults to module-level).

    Returns: dict with keys: success (bool), message (str), action (str|None).
    """
    client = supabase_client or supabase

    row_res = client.table('messages')\
        .select('*')\
        .eq('id', pending_id)\
        .eq('channel', 'whatsapp')\
        .is_('danny_decision', 'null')\
        .limit(1)\
        .maybe_single()\
        .execute()

    if not row_res.data:
        decided = client.table('messages')\
            .select('id, danny_decision')\
            .eq('id', pending_id)\
            .eq('channel', 'whatsapp')\
            .not_.is_('danny_decision', 'null')\
            .limit(1)\
            .maybe_single()\
            .execute()
        if decided and decided.data:
            return {
                "success": False, "action": "already_decided",
                "message": f"[{pending_id}] was already {decided.data['danny_decision']}."
            }
        return {
            "success": False, "action": "not_found",
            "message": f"No WhatsApp message found matching [{pending_id}]."
        }

    row = row_res.data
    title = row.get('suggested_title', row.get('body', ''))
    sender_name = row.get('sender_name', '')
    sender_phone = row.get('sender_id', '')
    summary = row.get('summary', '')

    if decision == 'approve':
        try:
            insert_data = {
                "content": title,
                "source": "whatsapp",
                "status": "pending",
                "direction": "incoming",
                "sender": "user",
                "message_type": "task",
                "metadata": {
                    "sender_name": sender_name,
                    "sender_phone": sender_phone,
                    "whatsapp_summary": summary,
                    "source": "whatsapp_approval"
                }
            }
            client.table('raw_dumps').insert([insert_data]).execute()
        except Exception as e:
            return {
                "success": False, "action": "staging_failed",
                "message": f"Task staging failed for [{row['id']}]. You can retry. ({e})"
            }

        client.table('messages').update({
            'danny_decision': 'approved',
            'decided_at': datetime.now(timezone.utc).isoformat()
        }).eq('id', row['id']).execute()

        print(f"Staged to raw_dumps via WhatsApp approval: {title}")
        return {"success": True, "action": "approved", "message": f"Staged: {title}"}

    elif decision == 'reject':
        client.table('messages').update({
            'danny_decision': 'rejected',
            'decided_at': datetime.now(timezone.utc).isoformat()
        }).eq('id', row['id']).execute()
        return {"success": True, "action": "rejected", "message": f"Dropped: {title}"}

    else:
        return {
            "success": False, "action": "invalid_action",
            "message": f"Invalid decision: {decision}. Must be 'approve' or 'reject'."
        }
