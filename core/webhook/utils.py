from core.services.db import get_supabase
import os
import httpx
from datetime import datetime, timezone, timedelta
from core.lib.duplicate_guard import check_duplicate
from core.lib.audit_logger import audit_log_sync

supabase = get_supabase()


async def process_channel_pending_decision(channel: str, pending_id: int, decision: str) -> dict:
    """Shared handler for processing approve/reject for channel-specific pending messages (teams, whatsapp, call)."""
    row_res = supabase.table('messages')\
        .select('*')\
        .eq('id', pending_id)\
        .eq('channel', channel)\
        .is_('danny_decision', 'null')\
        .limit(1)\
        .maybe_single()\
        .execute()

    if not row_res.data:
        decided = supabase.table('messages')\
            .select('id, danny_decision')\
            .eq('id', pending_id)\
            .maybe_single()\
            .execute()
        if decided.data and decided.data.get('danny_decision'):
            return {"success": False, "message": f"This {channel} item was already {decided.data['danny_decision']}.", "action": None}
        return {"success": False, "message": f"Pending {channel} item {pending_id} not found.", "action": None}

    msg = row_res.data
    is_approved = decision.lower() in ["y", "yes", "approve", "approved"]

    title = msg.get('suggested_title') or msg.get('body', '')[:60]
    sender_name = msg.get('sender_name', '')
    sender_id = msg.get('sender_id', '')
    summary = msg.get('summary', '') or msg.get('metadata', {}).get('summary', '')

    if is_approved:
        # Route to raw_dumps for extraction
        supabase.table('raw_dumps').insert({
            "content": title,
            "source": channel,
            "status": "pending",
            "direction": "incoming",
            "sender": "user",
            "message_type": "task",
            "metadata": {
                "sender_name": sender_name,
                "sender_id": sender_id,
                f"{channel}_summary": summary,
                "source": f"{channel}_approval",
                "original_msg_id": msg['id']
            }
        }).execute()
        
        action_msg = "approved and queued for extraction"
        decision_val = "approved"
    else:
        action_msg = "rejected and discarded"
        decision_val = "rejected"

    # Mark as decided
    supabase.table('messages').update({
        'danny_decision': decision_val,
        'decided_at': datetime.now(timezone.utc).isoformat()
    }).eq('id', pending_id).execute()

    return {"success": True, "message": f"Task from {channel} {action_msg}.", "action": decision_val}


def is_already_in_tasks_table(title: str) -> dict:
    """Check if a similar task already exists in the tasks table.
    Uses normalized exact match + anchor entity overlap (Jaccard-like).
    Fails open — always returns 'clear' on errors.

    Returns dict with keys: result ('block'|'flag'|'clear'), matched_id, matched_title, is_superset, ratio.
    """
    try:
        result = supabase.table('tasks')\
            .select('id, title')\
            .not_.in_('status', ['done', 'cancelled'])\
            .execute()
        tasks = result.data or []
        return check_duplicate(title, tasks)
    except Exception as e:
        audit_log_sync("webhook", "WARNING", f"Duplicate guard check failed (failing open): {e}")
        return {"result": "clear", "matched_id": None, "matched_title": None, "is_superset": False, "ratio": 0.0}


def is_recent_raw_dump(content: str, source: str) -> bool:
    """Check if identical content+source was inserted in the last 5 minutes.
    Used as idempotency guard against Telegram double-fires and user double-taps."""
    try:
        cutoff = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
        dup = supabase.table('raw_dumps') \
            .select('id') \
            .eq('content', content) \
            .eq('source', source) \
            .gte('created_at', cutoff) \
            .limit(1) \
            .execute()
        if dup.data:
            print(f"Duplicate guard: Skipping '{content[:50]}...' — inserted within 5 minutes")
            return True
        return False
    except Exception as e:
        audit_log_sync("webhook", "WARNING", f"Duplicate guard check failed (failing open): {e}")
        return False

async def get_recent_context(limit: int = 2) -> list:
    try:
        res = supabase.table('raw_dumps')\
            .select('content')\
            .eq('is_processed', False)\
            .order('created_at', desc=True)\
            .limit(limit)\
            .execute()
        return res.data if res.data else []
    except Exception:
        return []

async def trigger_github_pulse() -> bool:
    """Trigger GitHub Actions workflow dispatch for pulse briefing."""
    try:
        github_token = os.getenv("GITHUB_TOKEN")
        if not github_token:
            print("ERROR: GITHUB_TOKEN not set")
            return False

        owner = os.getenv("GITHUB_OWNER", "Crayon-Biz-LLP")
        repo = os.getenv("GITHUB_REPO", "integrated-os")

        url = f"https://api.github.com/repos/{owner}/{repo}/dispatches"

        headers = {
            "Authorization": f"token {github_token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28"
        }

        payload = {
            "event_type": "trigger_pulse"
        }

        async with httpx.AsyncClient() as client:
            response = await client.post(url, json=payload, headers=headers, timeout=10)

            if response.status_code == 204:
                print("✓ GitHub Actions workflow triggered successfully")
                return True
            else:
                audit_log_sync("webhook", "ERROR", f"GitHub dispatch failed: {response.status_code}")
                return False

    except Exception as e:
        audit_log_sync("webhook", "ERROR", f"ERROR triggering GitHub pulse: {e}")
        return False

async def hybrid_search_graph(query: str, node_id: str = None) -> str:
    """Graph-first search: Find primary entity and its connections."""
    try:
        if node_id:
            primary_id = node_id
            node_res = supabase.table('graph_nodes').select('id, label').eq('id', node_id).limit(1).execute()
            if not node_res.data:
                return ""
            primary_node = node_res.data[0]
        else:
            node_res = supabase.table('graph_nodes').select('id, label').ilike('label', f'%{query}%').limit(1).execute()
            if not node_res.data:
                return ""
            primary_node = node_res.data[0]
            primary_id = primary_node['id']

        edges_res = supabase.table('graph_edges').select('source_node_id, target_node_id, relationship').or_(f'source_node_id.eq.{primary_id},target_node_id.eq.{primary_id}').execute()

        if not edges_res.data:
            return ""

        connected_ids = set()

        for edge in edges_res.data:
            if edge['source_node_id'] == primary_id:
                connected_ids.add(edge['target_node_id'])
            elif edge['target_node_id'] == primary_id:
                connected_ids.add(edge['source_node_id'])

        if connected_ids:
            labels_res = supabase.table('graph_nodes').select('id, label').in_('id', list(connected_ids)).execute()
            label_map = {str(n['id']): n['label'] for n in labels_res.data}

            labeled_map = []
            for edge in edges_res.data:
                src_label = label_map.get(str(edge['source_node_id']), "Unknown")
                tgt_label = label_map.get(str(edge['target_node_id']), "Unknown")

                if edge['source_node_id'] == primary_id:
                    labeled_map.append(f"[{primary_node['label']}] -> [{edge['relationship']}] -> [{tgt_label}]")
                elif edge['target_node_id'] == primary_id:
                    labeled_map.append(f"[{src_label}] -> [{edge['relationship']}] -> [{primary_node['label']}]")

            return "\n".join(labeled_map)

        return ""

    except Exception as e:
        audit_log_sync("webhook", "ERROR", f"Hybrid search error: {e}")
        return ""

