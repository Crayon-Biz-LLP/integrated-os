from datetime import datetime, timezone
from core.webhook.utils import supabase


async def process_call_pending_decision(pending_id: int, decision: str, supabase_client=None) -> dict:
    """Process approve/reject for a call pending item.

    For 'approve': writes to raw_dumps (Pulse classifies/routes on next run).
    For 'reject': sets danny_decision='rejected'.

    Args:
        pending_id: ID in call_pending_items table.
        decision: 'approve' or 'reject'.
        supabase_client: Optional supabase client (defaults to module-level).

    Returns: dict with keys: success (bool), message (str), action (str|None).
    """
    client = supabase_client or supabase

    row_res = client.table('call_pending_items')\
        .select('*')\
        .eq('id', pending_id)\
        .is_('danny_decision', 'null')\
        .limit(1)\
        .maybe_single()\
        .execute()

    if not row_res.data:
        decided = client.table('call_pending_items')\
            .select('id, danny_decision')\
            .eq('id', pending_id)\
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
            "message": f"No call item found matching [{pending_id}]."
        }

    row = row_res.data
    title = row.get('suggested_title', '')
    recording_id = row.get('recording_id')
    action_type = row.get('action_type', 'task')
    summary = row.get('summary', '')

    if decision == 'approve':
        try:
            insert_data = {
                "content": title,
                "source": "call",
                "status": "pending",
                "direction": "incoming",
                "sender": "user",
                "message_type": "task" if action_type == 'task' else 'note',
                "metadata": {
                    "recording_id": recording_id,
                    "action_type": action_type,
                    "call_summary": summary,
                    "source": "call_approval"
                }
            }
            client.table('raw_dumps').insert([insert_data]).execute()
        except Exception as e:
            return {
                "success": False, "action": "staging_failed",
                "message": f"Task staging failed for [{row['id']}]. You can retry. ({e})"
            }

        client.table('call_pending_items').update({
            'danny_decision': 'approved',
            'decided_at': datetime.now(timezone.utc).isoformat()
        }).eq('id', row['id']).execute()

        print(f"Staged to raw_dumps via call approval: {title}")
        return {"success": True, "action": "approved", "message": f"Staged: {title}"}

    elif decision == 'reject':
        client.table('call_pending_items').update({
            'danny_decision': 'rejected',
            'decided_at': datetime.now(timezone.utc).isoformat()
        }).eq('id', row['id']).execute()
        return {"success": True, "action": "rejected", "message": f"Dropped: {title}"}

    else:
        return {
            "success": False, "action": "invalid_action",
            "message": f"Invalid decision: {decision}. Must be 'approve' or 'reject'."
        }
