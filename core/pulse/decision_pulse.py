"""Decision Pulse — pending approvals, no AI.

Extracted from core/pulse/engine.py as a focused module.
Handles pending decisions from email, calls, WhatsApp, Teams,
graph nodes, and graph edges. Shows inline Telegram keyboards
for approve/reject. No LLM calls involved here.
"""
import os
import json
import random
from datetime import datetime, timedelta, timezone

from core.webhook.telegram import send_telegram
from core.services.push_notification import send_push_notification
from core.lib.audit_logger import audit_log_sync
from core.pulse.llm import supabase
from core.lib.redis_cache import acquire_lock, release_lock
from core.pulse.run_logger import create_pulse_run, complete_pulse_run


async def process_decision_pulse(auth_secret: str = None, trigger: str = "api"):
    """List pending approvals from messages, graph nodes, and edges.

    Fetches pending items, auto-approves high-confidence ones via pattern
    matching, then sends a Telegram message with inline approve/reject
    keyboards for the remaining items. No AI involved.
    """
    from core.lib.audit_logger import set_trace_id
    set_trace_id()

    pulse_secret = os.getenv("PULSE_SECRET")
    if pulse_secret and auth_secret != pulse_secret:
        return {"error": "Unauthorized.", "status": 401}

    lock_key = "pulse_concurrency_lock"
    if not acquire_lock(lock_key, ttl=300):
        return {"success": False, "message": "Pulse or Decision Pulse already running. Concurrency lock active."}

    run_id = await create_pulse_run(supabase, "decision", trigger)

    try:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()

        # Expire old pending decisions
        try:
            supabase.table('messages')\
                .update({'danny_decision': 'expired'})\
                .is_('danny_decision', 'null')\
                .eq('classification', 'actionable')\
                .lt('created_at', cutoff)\
                .execute()
        except Exception as e:
            audit_log_sync("decision_pulse", "WARNING", f"Failed to expire old pending decisions: {e}")

        # Revert stale awaiting_details graph items back to pending
        try:
            supabase.table('pending_nodes')\
                .update({'status': 'pending'})\
                .eq('status', 'awaiting_details')\
                .lt('created_at', cutoff)\
                .execute()
        except Exception as e:
            audit_log_sync("decision_pulse", "WARNING", f"Failed to revert awaiting_details: {e}")

        # Fetch all pending messages
        pending_res = supabase.table('messages')\
            .select('id, channel, classification, suggested_title, suggested_project, sender_name, metadata, subject')\
            .is_('danny_decision', 'null')\
            .in_('channel', ['email', 'call', 'whatsapp', 'teams'])\
            .order('created_at', desc=False)\
            .limit(50)\
            .execute()

        email_items = []
        call_items = []
        whatsapp_items = []
        teams_items = []

        for row in (pending_res.data or []):
            if row['channel'] == 'email' and row.get('classification') == 'actionable' and len(email_items) < 5:
                email_items.append(row)
            elif row['channel'] == 'call' and row.get('classification') == 'actionable' and len(call_items) < 5:
                row['action_type'] = row.get('metadata', {}).get('action_type', 'task')
                call_items.append(row)
            elif row['channel'] == 'whatsapp' and row.get('classification') == 'actionable' and len(whatsapp_items) < 5:
                whatsapp_items.append(row)
            elif row['channel'] == 'teams' and row.get('classification') == 'actionable' and len(teams_items) < 5:
                teams_items.append(row)

        # ── Auto-approve high-confidence channel items before display ──
        from core.lib.telemetry import compute_pattern_confidence
        from core.webhook.email import process_email_pending_decision
        from core.webhook.utils import process_channel_pending_decision
        from core.pulse.graph import process_graph_pending_decision, process_pending_edge_decision

        auto_approved_ids = set()
        from core.lib.decision_features import build_decision_features, compute_composite_confidence
        for item_list in [email_items, call_items, whatsapp_items, teams_items]:
            for row in list(item_list):
                channel = row['channel']
                _features = build_decision_features(row, channel)
                pattern_result = await compute_composite_confidence(_features, f"{channel}_pipeline")
                if pattern_result.get("recommendation") in ("approve", "auto_approve"):
                    if channel == 'email':
                        await process_email_pending_decision(row['id'], 'approve', auto_decided=True)
                    else:
                        await process_channel_pending_decision(channel, row['id'], 'approve', auto_decided=True)
                    auto_approved_ids.add(row['id'])
                    audit_log_sync("decision_pulse", "INFO", f"Auto-approved {channel} item {row['id']} ({pattern_result.get('rule', 'N/A')})")

        email_items = [r for r in email_items if r['id'] not in auto_approved_ids]
        call_items = [r for r in call_items if r['id'] not in auto_approved_ids]
        whatsapp_items = [r for r in whatsapp_items if r['id'] not in auto_approved_ids]
        teams_items = [r for r in teams_items if r['id'] not in auto_approved_ids]

        # ── Startup: prune orphaned call_pipeline patterns from old feature hash space ──
        from core.lib.telemetry import prune_orphaned_patterns
        try:
            prune_result = await prune_orphaned_patterns()
            if prune_result["total_orphans"] > 0:
                audit_log_sync(
                    "decision_pulse", "INFO",
                    f"Pattern migration: pruned {prune_result['deleted']} orphaned patterns "
                    f"({prune_result['total_orphans']} found, dry_run={prune_result['dry_run']})"
                )
        except Exception as prune_err:
            audit_log_sync("decision_pulse", "WARNING", f"Pattern prune failed (non-blocking): {prune_err}")

        # ── Auto-approve high-confidence graph nodes before display ──
        pending_graph = supabase.table('pending_nodes')\
            .select('id, label, type:node_type, source_text')\
            .eq('status', 'pending')\
            .order('created_at', desc=False)\
            .limit(5)\
            .execute()

        graph_items = pending_graph.data or []

        auto_approved_graph_ids = set()
        for row in list(graph_items):
            features = {"node_type": row["type"], "has_context": bool(row.get("source_text"))}
            pattern_result = await compute_pattern_confidence(features, "entity_extraction")
            if pattern_result.get("recommendation") in ("approve", "auto_approve"):
                await process_graph_pending_decision(row['id'], 'approve', auto_decided=True)
                auto_approved_graph_ids.add(row['id'])
                audit_log_sync("decision_pulse", "INFO", f"Auto-approved graph node {row['id']} ({row['label']}) — {pattern_result.get('rule', 'N/A')}")

        graph_items = [r for r in graph_items if r['id'] not in auto_approved_graph_ids]

        # ── Auto-approve high-confidence graph edges ──
        pending_edges_res = supabase.table('pending_graph_edges')\
            .select('id, source_label, target_label, relationship, source_type, target_type')\
            .eq('status', 'pending')\
            .order('created_at', desc=False)\
            .limit(5)\
            .execute()

        pending_edges = pending_edges_res.data or []

        auto_approved_edge_ids = set()
        for row in list(pending_edges):
            features = {"relationship": row["relationship"], "source_type": row.get("source_type"), "target_type": row.get("target_type")}
            pattern_result = await compute_pattern_confidence(features, "entity_extraction")
            if pattern_result.get("recommendation") in ("approve", "auto_approve"):
                await process_pending_edge_decision(row['id'], 'approve', auto_decided=True)
                auto_approved_edge_ids.add(row['id'])
                audit_log_sync("decision_pulse", "INFO",
                    f"Auto-approved edge {row['id']} ({row['relationship']}) — {pattern_result.get('rule', 'N/A')}")
            elif pattern_result.get("recommendation") == "suggest" and row.get('source_label') and row.get('target_label'):
                # Borderline edge: use Planner/Critic deliberate() for cross-subsystem signals
                try:
                    from core.lib.planner_critic import deliberate
                    _edge_text = f"{row['source_label']} {row['relationship']} {row['target_label']}"
                    delib_result = await deliberate(
                        candidates=[
                            {"label": "approve", "primary": pattern_result.get("confidence", 0.5)},
                            {"label": "review", "primary": 1.0 - pattern_result.get("confidence", 0.5)},
                        ],
                        text=_edge_text,
                        subsystem="graph_edge",
                    )
                    if delib_result.get("recommendation") == "auto_execute" and delib_result.get("best") == "approve":
                        await process_pending_edge_decision(row['id'], 'approve', auto_decided=True)
                        auto_approved_edge_ids.add(row['id'])
                        audit_log_sync("decision_pulse", "INFO",
                            f"Planner-approved edge {row['id']} ({row['relationship']}) — cross-subsystem {delib_result['candidates'][0]['reasoning']}")
                except Exception as delib_err:
                    audit_log_sync("decision_pulse", "WARNING", f"Edge deliberation failed (non-blocking): {delib_err}")

        pending_edges = [r for r in pending_edges if r['id'] not in auto_approved_edge_ids]

        # Build deliberation scores for remaining items (show pattern confidence)
        async def _score_row(row: dict, subsystem: str) -> str:
            if subsystem == "graph_edges":
                feat = {"relationship": row["relationship"], "source_type": row.get("source_type"), "target_type": row.get("target_type")}
                _subsystem = "entity_extraction"
            elif subsystem in ("entity_extraction",):
                feat = {"node_type": row["type"], "has_context": bool(row.get("source_text"))}
                _subsystem = subsystem
            else:
                from core.lib.decision_features import build_decision_features
                channel = subsystem.replace('_pipeline', '') if '_pipeline' in subsystem else subsystem
                feat = build_decision_features(row, channel)
                _subsystem = subsystem
            pr = await compute_pattern_confidence(feat, _subsystem)
            if pr["recommendation"] in ("approve", "auto_approve"):
                return f"✅ *auto* ({pr['rule']})"
            elif pr["recommendation"] == "suggest":
                return f"💡 *suggest* ({pr['rule']})"
            else:
                return f"🔍 *review* ({pr['rule']})"

        total = len(email_items) + len(call_items) + len(whatsapp_items) + len(teams_items) + len(graph_items) + len(pending_edges)
        if total == 0:
            await complete_pulse_run(supabase, run_id, status="completed", metadata={"reason": "no_pending"})
            release_lock(lock_key)
            return {"success": True, "message": "No pending decisions."}

        # --- P4: Push notification for pending decisions (rate-limited) ---
        try:
            push_fingerprint_items = []
            for row in email_items:
                push_fingerprint_items.append(("email", row['id']))
            for row in call_items:
                push_fingerprint_items.append(("call", row['id']))
            for row in whatsapp_items:
                push_fingerprint_items.append(("whatsapp", row['id']))
            for row in teams_items:
                push_fingerprint_items.append(("teams", row['id']))
            for row in graph_items:
                push_fingerprint_items.append(("graph_node", row['id']))
            for row in pending_edges:
                push_fingerprint_items.append(("graph_edge", row['id']))
            push_fingerprint_items.sort()
            current_fp = json.dumps(push_fingerprint_items)

            last_fp_row = supabase.table('core_config').select('content').eq('key', 'last_decision_push_fp').execute()
            last_fp = (last_fp_row.data[0]['content'] if last_fp_row.data else None)

            if current_fp != last_fp:
                channels = []
                if email_items:
                    channels.append(f"{len(email_items)} email")
                if call_items:
                    channels.append(f"{len(call_items)} call")
                if whatsapp_items:
                    channels.append(f"{len(whatsapp_items)} WhatsApp")
                if teams_items:
                    channels.append(f"{len(teams_items)} Teams")
                if graph_items:
                    channels.append(f"{len(graph_items)} graph node")
                if pending_edges:
                    channels.append(f"{len(pending_edges)} graph edge")
                push_title = f"{total} pending decisions"
                push_body = f"From: {', '.join(channels)}"
                await send_push_notification(
                    title=push_title,
                    body=push_body,
                    data={"type": "decision"},
                )
                supabase.table('core_config').upsert({
                    'key': 'last_decision_push_fp',
                    'content': current_fp
                }, on_conflict='key').execute()
            else:
                audit_log_sync("decision_pulse", "INFO", "Rate-limited decision push — no changes since last push")
        except Exception as push_err:
            audit_log_sync("decision_pulse", "WARNING", f"Push notification failed (non-critical): {push_err}")

        # ── Build auto-processed digest ──
        auto_total = len(auto_approved_ids) + len(auto_approved_graph_ids) + len(auto_approved_edge_ids)
        digest_line = f"🤖 *Auto-processed:* {len(auto_approved_ids)} channel items, {len(auto_approved_graph_ids)} graph nodes, {len(auto_approved_edge_ids)} graph edges" if auto_total > 0 else None

        openers = [
            "Danny, you got some pending decisions based out of your emails, call logs and beeper messages — your call on each?",
            "Danny, you got some pending decisions from emails, calls, and beeper — your call on each?",
            "Emails, call extracts, and texts waiting on a nod. Tap to approve or drop.",
        ]
        lines = [random.choice(openers), ""]
        if digest_line:
            lines.append(digest_line)
            lines.append("")
            lines.append("_Remaining items need your review:_")
            lines.append("")

        if email_items:
            lines.append(f"📨 EMAIL DECISIONS ({len(email_items)})")
            for row in email_items:
                proj = f" ({row['suggested_project']})" if row.get('suggested_project') else ""
                title = row.get('suggested_title') or row.get('subject') or 'Untitled'
                score_tag = await _score_row(row, f"{row['channel']}_pipeline")
                lines.append(f"[e{row['id']}] {title[:50]}{proj} — {score_tag}")
            lines.append("")

        if call_items:
            lines.append(f"📞 CALL EXTRACTS ({len(call_items)})")
            for row in call_items:
                proj = f" ({row['suggested_project']})" if row.get('suggested_project') else ""
                prefix = "📋 " if row.get('action_type') == 'task' else "💡 "
                score_tag = await _score_row(row, f"{row['channel']}_pipeline")
                lines.append(f"{prefix}[c{row['id']}] {(row.get('suggested_title') or 'Untitled')[:50]}{proj} — {score_tag}")
            lines.append("")

        if whatsapp_items:
            lines.append(f"💬 WHATSAPP EXTRACTS ({len(whatsapp_items)})")
            for row in whatsapp_items:
                proj = f" ({row['suggested_project']})" if row.get('suggested_project') else ""
                from_str = f" — {row['sender_name']}" if row.get('sender_name') else ""
                score_tag = await _score_row(row, f"{row['channel']}_pipeline")
                lines.append(f"💬 [w{row['id']}] {(row.get('suggested_title') or 'Untitled')[:50]}{proj}{from_str} — {score_tag}")
            lines.append("")

        if teams_items:
            lines.append(f"🟣 TEAMS CHATS ({len(teams_items)})")
            for row in teams_items:
                proj = f" ({row['suggested_project']})" if row.get('suggested_project') else ""
                from_str = f" — {row['sender_name']}" if row.get('sender_name') else ""
                score_tag = await _score_row(row, f"{row['channel']}_pipeline")
                lines.append(f"🟣 [t{row['id']}] {(row.get('suggested_title') or 'Untitled')[:50]}{proj}{from_str} — {score_tag}")
            lines.append("")

        if graph_items:
            lines.append(f"🕸️ GRAPH NODES ({len(graph_items)})")
            for row in graph_items:
                score_tag = await _score_row(row, "entity_extraction")
                lines.append(f"👤 [g{row['id']}] {row['label']} ({row['type']}) — {score_tag}")
            lines.append("")

        if pending_edges:
            lines.append(f"🔗 GRAPH EDGES ({len(pending_edges)})")
            for row in pending_edges:
                score_tag = await _score_row(row, "graph_edges")
                lines.append(f"🔗 [pe{row['id']}] {row['source_label']} → {row['relationship']} → {row['target_label']} — {score_tag}")
            lines.append("")

        # Show awaiting_details count
        try:
            awaiting_res = supabase.table('pending_nodes').select('id', count='exact').eq('status', 'awaiting_details').execute()
            awaiting_count = awaiting_res.count if hasattr(awaiting_res, 'count') else len(awaiting_res.data or [])
            if awaiting_count:
                lines.append(f"⏳ {awaiting_count} node(s) waiting for details (tap ✅ to provide context)")
                lines.append("")
        except Exception as e:
            audit_log_sync("decision_pulse", "WARNING", f"Failed to fetch awaiting_details count: {e}")

        message = "\n".join(lines).strip()

        telegram_chat_id = os.getenv("TELEGRAM_CHAT_ID")
        send_success = False

        if telegram_chat_id and message:
            keyboard = []
            for row in email_items:
                sc = f"e{row['id']}"
                keyboard.append([
                    {"text": f"✅ {sc}", "callback_data": f"approve_{sc}"},
                    {"text": f"❌ {sc}", "callback_data": f"reject_{sc}"}
                ])
            if email_items:
                keyboard.append([
                    {"text": "✅ Approve All Emails", "callback_data": "approve_all_emails"},
                    {"text": "❌ Reject All Emails", "callback_data": "reject_all_emails"}
                ])
            for row in call_items:
                sc = f"c{row['id']}"
                keyboard.append([
                    {"text": f"✅ {sc}", "callback_data": f"approve_{sc}"},
                    {"text": f"❌ {sc}", "callback_data": f"reject_{sc}"}
                ])
            if call_items:
                keyboard.append([
                    {"text": "✅ Approve All Calls", "callback_data": "approve_all_calls"},
                    {"text": "❌ Reject All Calls", "callback_data": "reject_all_calls"}
                ])
            for row in whatsapp_items:
                sc = f"w{row['id']}"
                keyboard.append([
                    {"text": f"✅ {sc}", "callback_data": f"approve_{sc}"},
                    {"text": f"❌ {sc}", "callback_data": f"reject_{sc}"}
                ])
            if whatsapp_items:
                keyboard.append([
                    {"text": "✅ Approve All WhatsApp", "callback_data": "approve_all_whatsapp"},
                    {"text": "❌ Reject All WhatsApp", "callback_data": "reject_all_whatsapp"}
                ])
            for row in teams_items:
                sc = f"t{row['id']}"
                keyboard.append([
                    {"text": f"✅ {sc}", "callback_data": f"approve_{sc}"},
                    {"text": f"❌ {sc}", "callback_data": f"reject_{sc}"}
                ])
            if teams_items:
                keyboard.append([
                    {"text": "✅ Approve All Teams", "callback_data": "approve_all_teams"},
                    {"text": "❌ Reject All Teams", "callback_data": "reject_all_teams"}
                ])
            for row in graph_items:
                sc = f"g{row['id']}"
                keyboard.append([
                    {"text": f"✅ {sc}", "callback_data": f"approve_{sc}"},
                    {"text": f"❌ {sc}", "callback_data": f"reject_{sc}"}
                ])
            if graph_items:
                keyboard.append([
                    {"text": "✅ Approve All Nodes", "callback_data": "approve_all_nodes"},
                    {"text": "❌ Reject All Nodes", "callback_data": "reject_all_nodes"}
                ])
            for row in pending_edges:
                sc = f"pe{row['id']}"
                keyboard.append([
                    {"text": f"✅ {row['relationship']}", "callback_data": f"approve_{sc}"},
                    {"text": "✏️ Edit", "callback_data": f"edit_{sc}"},
                    {"text": "❌", "callback_data": f"reject_{sc}"}
                ])
            if pending_edges:
                keyboard.append([
                    {"text": "✅ Approve All Edges", "callback_data": "approve_all_edges"},
                    {"text": "❌ Reject All Edges", "callback_data": "reject_all_edges"}
                ])
            if auto_total > 0:
                undo_row = []
                if auto_approved_ids:
                    undo_row.append({"text": f"↩️ Undo {len(auto_approved_ids)} channel", "callback_data": "undo_auto_channels"})
                if auto_approved_graph_ids:
                    undo_row.append({"text": f"↩️ Undo {len(auto_approved_graph_ids)} node", "callback_data": "undo_auto_graph"})
                if auto_approved_edge_ids:
                    undo_row.append({"text": f"↩️ Undo {len(auto_approved_edge_ids)} edge", "callback_data": "undo_auto_edge"})
                if undo_row:
                    keyboard.append(undo_row)

            send_success = await send_telegram(
                chat_id=telegram_chat_id,
                message_text=message,
                show_keyboard=False,
                inline_keyboard=keyboard if keyboard else None
            )

        shown_ids = []
        if send_success:
            for row in email_items + call_items + whatsapp_items + teams_items:
                shown_ids.append(row['id'])
            if shown_ids:
                supabase.table('messages')\
                    .update({'shown_in_brief': True})\
                    .in_('id', shown_ids)\
                    .execute()

        await complete_pulse_run(supabase, run_id, status="completed",
            metadata={"decision_count": total, "send_success": send_success})
        release_lock(lock_key)
        return {"success": True, "decision_count": total}

    except Exception as e:
        import traceback
        audit_log_sync("pulse", "CRITICAL", f"Decision Pulse Critical Error: {e}")
        traceback.print_exc()
        await complete_pulse_run(supabase, run_id, status="failed", error_message=str(e))
        release_lock(lock_key)
        return {"error": str(e)}
