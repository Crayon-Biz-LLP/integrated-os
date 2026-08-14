from contextlib import contextmanager
from core.services.db import (
    channel_tenant_scope,
    tenant_aware_client,
)
from core.decisions import record_decision
import os
import httpx
from datetime import datetime, timezone
from core.lib.duplicate_guard import check_duplicate
from core.lib.audit_logger import audit_log_sync
from core.lib.telemetry import emit_observation
from core.services.briefing_refresh import fire_briefing_refresh
from core.lib.graph_rules import resolve_alias

# M3 sweep: the module-wide binding is now the tenant-aware facade. Every
# `supabase.table(...)` / `supabase.rpc(...)` call in core/webhook/* flows
# through it — tenant-scoped (fail-closed) after db/78, legacy unscoped
# before. Importers keep `from core.webhook.utils import supabase` untouched.
supabase = tenant_aware_client()


@contextmanager
def webhook_tenant_scope():
    """(M3) webhook alias of the generic channel_tenant_scope() — kept so
    the webhook module's entry wrappers read naturally. Same semantics:
    no-op when a tenant context is already active; pre-db/78 resolves to
    None → runs unscoped legacy, exactly as before.
    """
    with channel_tenant_scope():
        yield


async def process_channel_pending_decision(channel: str, pending_id: int, decision: str, auto_decided: bool = False, rejection_context: str = None) -> dict:
    """Shared handler for processing approve/reject for channel-specific pending messages (teams, whatsapp, call).
    (M3: wrapped in the channel tenant scope.)
    
    Args:
        channel: 'call', 'whatsapp', 'teams'
        pending_id: Message ID in the messages table
        decision: 'approve' or 'reject'
        auto_decided: Whether this was an auto-decision (from Decision Pulse)
        rejection_context: Optional user-provided explanation for rejection
                          (e.g., "already handled", "wrong project"). Captured from
                          Telegram shortcode trailing text like "c42 reject, handled offline".
    """
    with webhook_tenant_scope():
        return await _process_channel_pending_decision(channel, pending_id, decision, auto_decided, rejection_context)


async def _process_channel_pending_decision(channel: str, pending_id: int, decision: str, auto_decided: bool = False, rejection_context: str = None) -> dict:
    """Inner implementation (M3: tenant scope applied by the public wrapper)."""
    row_res = supabase.table('messages')\
        .select('*')\
        .eq('id', pending_id)\
        .eq('channel', channel)\
        .is_('danny_decision', 'null')\
        .eq('direction', 'incoming')\
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
    summary = msg.get('summary', '') or msg.get('metadata', {}).get('summary', '')

    if is_approved:
        # Process immediately via Action Planner
        from core.actions.planner import plan_actions
        from core.actions.executor import execute_planned_actions
        
        chat_id = int(os.getenv("TELEGRAM_CHAT_ID", "0"))
        
        # Gap 1: Run entity resolution on the title to pass entity context to planner
        resolved_entity = None
        try:
            from core.lib.entity_linker import resolve_entities
            entity_resolution = resolve_entities(
                text=title,
                planner_org_name=msg.get('suggested_project'),
                write_signal_on_miss=True,
            )
            if entity_resolution.organization_name or entity_resolution.project_name:
                resolved_entity = entity_resolution.organization_name or entity_resolution.project_name
                audit_log_sync("webhook", "INFO",
                    f"Gap 1: Resolved entity '{resolved_entity}' for {channel} approval #{pending_id}")
        except Exception:
            pass
        
        try:
            original_text = msg.get('body') or title
            actions = await plan_actions(
                text=original_text,
                title=title,
                intent="TASK",
                entity=resolved_entity,
            )
            if actions:
                await execute_planned_actions(actions, chat_id, text=original_text, source=channel, entity=resolved_entity)
            action_msg = "approved and processed"
        except Exception as plan_err:
            audit_log_sync("webhook", "ERROR", f"Failed to plan/execute {channel} approval: {plan_err}")
            action_msg = "approved but processing failed"
        
        decision_val = "approved"
    else:
        action_msg = "rejected and discarded"
        decision_val = "rejected"

    # Mark as decided
    supabase.table('messages').update({
        'danny_decision': decision_val,
        'decided_at': datetime.now(timezone.utc).isoformat()
    }).eq('id', pending_id).execute()

    # Unified feature construction with context dimensions + rejection reason
    from core.lib.decision_features import build_decision_features
    _features = build_decision_features(msg, channel, rejection_context=rejection_context)

    await emit_observation(
        subsystem=f'{channel}_pipeline',
        event_type='approval' if is_approved else 'rejection',
        features=_features,
        predicted='actionable',
        actual='actionable' if is_approved else 'rejected',
        outcome='confirmed' if is_approved else 'rejected',
        source=f'{channel}_decision_pulse'
    )

    # Record a decision in the structured decisions table
    try:
        record_decision(
            decision_type="channel_approval" if is_approved else "channel_rejection",
            title=title[:120],
            context=f"{channel} item #{pending_id}: {summary[:200] if summary else title[:200]}",
            entity_type="message",
            entity_id=str(pending_id),
            confidence=1.0,
            source=f"{channel}_decision_pulse",
            auto_decided=auto_decided,
        )
    except Exception as dec_err:
        audit_log_sync("webhook", "WARNING", f"Failed to record channel decision: {dec_err}")

    # The board changed (an item was approved/rejected) — refresh the live
    # briefing so the app catches up immediately.
    fire_briefing_refresh(source=f"{channel}_decision")

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
            .eq('is_current', True)\
            .not_.in_('status', ['done', 'cancelled'])\
            .execute()
        tasks = result.data or []
        return check_duplicate(title, tasks)
    except Exception as e:
        audit_log_sync("webhook", "WARNING", f"Duplicate guard check failed (failing open): {e}")
        return {"result": "clear", "matched_id": None, "matched_title": None, "is_superset": False, "ratio": 0.0}

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
            audit_log_sync("webhook", "ERROR", "GITHUB_TOKEN not set")
            return False

        from core.lib.constants import resolve_github_config
        owner, repo = resolve_github_config()

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
                audit_log_sync("webhook", "INFO", "GitHub Actions workflow triggered successfully")
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
            node_res = supabase.table('graph_nodes').select('id, label').ilike('label', f'%{query}%').eq('is_current', True).limit(1).execute()
            if not node_res.data:
                return ""
            primary_node = node_res.data[0]
            primary_id = primary_node['id']

        edges_res = supabase.table('graph_edges').select('source_node_id, target_node_id, relationship').or_(f'source_node_id.eq.{primary_id},target_node_id.eq.{primary_id}').eq('is_current', True).execute()

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
                src_label = resolve_alias(label_map.get(str(edge['source_node_id']), "Unknown"))
                tgt_label = resolve_alias(label_map.get(str(edge['target_node_id']), "Unknown"))

                if edge['source_node_id'] == primary_id:
                    primary_resolved = resolve_alias(primary_node['label'])
                    labeled_map.append(f"[{primary_resolved}] -> [{edge['relationship']}] -> [{tgt_label}]")
                elif edge['target_node_id'] == primary_id:
                    primary_resolved = resolve_alias(primary_node['label'])
                    labeled_map.append(f"[{src_label}] -> [{edge['relationship']}] -> [{primary_resolved}]")

            return "\n".join(labeled_map)

        return ""

    except Exception as e:
        audit_log_sync("webhook", "ERROR", f"Hybrid search error: {e}")
        return ""

