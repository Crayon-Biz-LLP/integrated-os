from core.llm.constants import CLASSIFICATION_MODEL, SYNTHESIS_MODEL
import os
import json
import re
import random
import asyncio
from core.webhook.telegram import send_telegram
from datetime import datetime, timedelta, timezone
from pydantic import BaseModel, Field
from typing import List, Optional

from core.lib.audit_logger import info, warning, error, audit_log_sync
from core.lib.url_filter import is_url_text
from core.lib.temporal_lineage import detect_drift
from core.lib.conversation import get_or_create_session, format_history_for_prompt
from core.decisions import record_decision

from core.services.google_service import get_tasks_service

from core.pulse.llm import (
    supabase,
)
from core.llm.fallback import generate_content_with_fallback
from core.llm.config import WorkloadProfile
from core.pulse.utils import format_error, get_project_name, build_routing_context, normalize_cluster_title
from core.pulse.memory import (
    write_outcome_memory,
    detect_temporal_patterns, serendipity_engine, adaptive_briefing_learner,
    retrieve_hindsight_memories, generate_after_action_report,
)
from core.pulse.context import context_provider
from core.pulse.graph import (
    check_task_dependencies,
    analyze_communication_patterns, fetch_graph_task_context,
    get_graph_centrality_context,
)
from core.pulse.pipeline import update_heartbeat, check_pipeline_health
from core.pulse.calendar import (
    sync_completed_tasks_from_google,
)
from core.pulse.practices import (
    detect_practices, build_practice_edges, build_practice_correlations,
    sync_practice_canonical_pages, build_rhythms_section,
)
from core.pulse.resources import batch_enrich_resources
from core.services.push_notification import send_push_notification


# ──────────────────────────────────────────
# RECURRING TASK AUTO-EXPIRY
# ──────────────────────────────────────────
# B4: Briefing history helpers
_BRIEFING_HISTORY_HOURS = 48
_BRIEFING_HISTORY_LIMIT = 3

def _store_briefing_to_history(briefing_text: str):
    """Store a condensed summary of this briefing in memories for future context."""
    if not briefing_text:
        return
    try:
        # Extract a compact summary (first 200 chars of substantive content)
        summary = briefing_text.strip()[:200].replace('\n', ' ').strip()
        supabase.table('memories').insert({
            'content': f"[BRIEFING] {summary}",
            'memory_type': 'pulse_briefing',
            'source': 'pulse_engine',
            'expires_at': (datetime.now(timezone.utc) + timedelta(hours=_BRIEFING_HISTORY_HOURS * 2)).isoformat()
        }).execute()
    except Exception:
        pass

def _get_recent_briefings_context() -> str:
    """B4: Return a string listing what was already briefed, so the AI avoids repetition.
    
    Returns empty string if no recent briefings exist (fail-open).
    """
    try:
        rows = supabase.table('memories') \
            .select('content, created_at') \
            .eq('memory_type', 'pulse_briefing') \
            .eq('is_current', True) \
            .gte('created_at', (datetime.now(timezone.utc) - timedelta(hours=_BRIEFING_HISTORY_HOURS)).isoformat()) \
            .order('created_at', desc=True) \
            .limit(_BRIEFING_HISTORY_LIMIT) \
            .execute()
        if not rows.data:
            return ""
        parts = []
        for r in rows.data:
            content = r.get('content', '')
            created = r.get('created_at', '')[:16] if r.get('created_at') else ''
            if content:
                # Strip [BRIEFING] prefix for the prompt
                cleaned = content.replace('[BRIEFING]', '').strip()
                parts.append(f"- [{created}] {cleaned}")
        if not parts:
            return ""
        return f"PREVIOUSLY BRIEFED (last {len(parts)} briefings — do NOT repeat this content verbatim):\n" + "\n".join(parts)
    except Exception:
        return ""


def _auto_expire_recurring_tasks():
    """Parse RRULE UNTIL/COUNT on active recurring tasks. If the recurrence has
    ended, mark the task as auto-expired (status=cancelled) so it stops
    appearing in briefings."""
    try:
        rows = supabase.table('tasks')\
            .select('id, title, recurrence, reminder_at, created_at')\
            .eq('status', 'todo')\
            .eq('is_current', True)\
            .not_.is_('recurrence', None)\
            .execute()
        if not rows.data:
            return

        now = datetime.now(timezone.utc)
        expired = []
        for task in rows.data:
            rrule = (task.get('recurrence') or '').strip()

            # Attempt 1: UNTIL=YYYYMMDDTHHMMSSZ (UTC datetime)
            m = re.search(r'UNTIL=(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})Z', rrule)
            if m:
                until_dt = datetime(
                    int(m.group(1)), int(m.group(2)), int(m.group(3)),
                    int(m.group(4)), int(m.group(5)), int(m.group(6)),
                    tzinfo=timezone.utc
                )
                if now >= until_dt:
                    expired.append(task['id'])
                continue

            # Attempt 2: UNTIL=YYYYMMDD (date only)
            m = re.search(r'UNTIL=(\d{4})(\d{2})(\d{2})(?!T)', rrule)
            if m:
                until_dt = datetime(
                    int(m.group(1)), int(m.group(2)), int(m.group(3)),
                    23, 59, 59, tzinfo=timezone.utc
                )
                if now >= until_dt:
                    expired.append(task['id'])
                continue

            # Attempt 3: COUNT-based recurrence (e.g., RRULE:FREQ=WEEKLY;COUNT=10)
            m_count = re.search(r'COUNT=(\d+)', rrule)
            m_freq = re.search(r'FREQ=(\w+)', rrule)
            if m_count and m_freq:
                count = int(m_count.group(1))
                freq = m_freq.group(1).upper()
                # Use reminder_at as the first occurrence, fall back to created_at
                start_str = task.get('reminder_at') or task.get('created_at')
                if not start_str:
                    continue
                try:
                    start_dt = datetime.fromisoformat(start_str.replace('Z', '+00:00'))
                except Exception:
                    continue
                # Calculate the last occurrence
                if freq == 'DAILY':
                    last_dt = start_dt + timedelta(days=count - 1)
                elif freq == 'WEEKLY':
                    last_dt = start_dt + timedelta(weeks=count - 1)
                elif freq == 'MONTHLY':
                    last_dt = start_dt + timedelta(days=30 * (count - 1))
                elif freq == 'YEARLY':
                    last_dt = start_dt + timedelta(days=365 * (count - 1))
                else:
                    continue
                if now >= last_dt:
                    expired.append(task['id'])
                continue

        if expired:
            for tid in expired:
                supabase.table('tasks').update({
                    'status': 'cancelled',
                    'completed_at': now.isoformat()
                }).eq('id', tid).execute()
                try:
                    record_decision(
                        decision_type="task_auto_expiry",
                        title=f"Auto-expired recurring task #{tid}",
                        context="Recurrence (RRULE) has ended — no future instances remain.",
                        entity_type="task",
                        entity_id=str(tid),
                        confidence=1.0,
                        source="pulse_engine",
                        auto_decided=True,
                    )
                except Exception as dec_err:
                    audit_log_sync("pulse", "WARNING", f"Failed to record auto-expiry decision: {dec_err}")
            audit_log_sync("pulse", "INFO", f"⏰ Auto-expired {len(expired)} recurring tasks (recurrence ended).")
    except Exception as e:
        audit_log_sync("pulse", "WARNING", f"Auto-expiry check failed: {e}")


# 🛡️ CLEAN MODELS (Removed Config blocks to prevent API rejection)
class CompletedTask(BaseModel):
    id: int
    status: str
    reminder_at: Optional[str] = None
    duration_mins: Optional[int] = None

class NewProject(BaseModel):
    name: str
    importance: Optional[int] = 5
    context: Optional[str] = "work"
    description: Optional[str] = None
    keywords: Optional[List[str]] = Field(default_factory=list)
    parent_project_name: Optional[str] = None

class NewPerson(BaseModel):
    name: str
    role: Optional[str] = None
    strategic_weight: Optional[int] = 5

class ResourceItem(BaseModel):
    url: str
    title: Optional[str] = None
    summary: Optional[str] = None
    cluster_name: Optional[str] = None
    project_name: Optional[str] = None
    strategic_note: Optional[str] = None

class LogEntry(BaseModel):
    entry_type: str
    content: str

class NewTask(BaseModel):
    title: str
    project_name: Optional[str] = None
    priority: Optional[str] = None
    estimated_duration: Optional[int] = 15
    reminder_at: Optional[str] = None
    is_revenue_critical: Optional[bool] = False

class PulseOutput(BaseModel):
    completed_task_ids: List[CompletedTask] = Field(default_factory=list)
    new_projects: List[NewProject] = Field(default_factory=list)
    new_people: List[NewPerson] = Field(default_factory=list)
    new_tasks: List[NewTask] = Field(default_factory=list)
    resources: List[ResourceItem] = Field(default_factory=list)
    logs: List[LogEntry] = Field(default_factory=list)
    new_clusters: List[str] = Field(default_factory=list)
    briefing: str










# --- 📋 DECISION PULSE (No AI, just pending decisions) ---
async def process_decision_pulse(auth_secret: str = None, trigger: str = "api"):
    from core.lib.audit_logger import set_trace_id
    set_trace_id()
    
    from core.pulse.run_logger import create_pulse_run, complete_pulse_run
    from core.lib.redis_cache import acquire_lock, release_lock
    
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
        except Exception:
            pass

        # Revert stale awaiting_details graph items back to pending
        try:
            supabase.table('pending_nodes')\
                .update({'status': 'pending'})\
                .eq('status', 'awaiting_details')\
                .lt('created_at', cutoff)\
                .execute()
        except Exception:
            pass

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
            .select('id, label, type, source_text')\
            .eq('status', 'pending')\
            .order('created_at', desc=False)\
            .limit(5)\
            .execute()

        graph_items = pending_graph.data or []

        auto_approved_graph_ids = set()
        for row in list(graph_items):
            features = {"node_type": row["type"], "has_context": bool(row.get("source_text"))}
            pattern_result = await compute_pattern_confidence(features, "entity_extraction")
            # Hardcoded 0.85 removed — now uses MIN_AUTO_APPROVE_OBSERVATIONS (5 obs) + error-rate demotion
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
            return {"success": True, "message": "No pending decisions."}

        # --- P4: Push notification for pending decisions (rate-limited) ---
        try:
            # Compute a fingerprint of current items — only push when changed
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

            # Check last pushed fingerprint
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
                # Store the fingerprint so we don't re-push same items
                supabase.table('core_config').upsert({
                    'key': 'last_decision_push_fp',
                    'content': current_fp
                }, on_conflict='key').execute()
            else:
                audit_log_sync("decision_pulse", "INFO", "Rate-limited decision push — no changes since last push")
        except Exception as push_err:
            audit_log_sync("decision_pulse", "WARNING", f"Push notification failed (non-critical): {push_err}")

        # ── Build auto-processed digest with inline undo buttons ──
        auto_total = len(auto_approved_ids) + len(auto_approved_graph_ids) + len(auto_approved_edge_ids)
        if auto_total > 0:
            digest_line = f"🤖 *Auto-processed:* {len(auto_approved_ids)} channel items, {len(auto_approved_graph_ids)} graph nodes, {len(auto_approved_edge_ids)} graph edges"
        else:
            digest_line = None

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

        # Show awaiting_details count so items stuck in clarification are visible
        try:
            awaiting_res = supabase.table('pending_nodes').select('id', count='exact').eq('status', 'awaiting_details').execute()
            awaiting_count = awaiting_res.count if hasattr(awaiting_res, 'count') else len(awaiting_res.data or [])
            if awaiting_count:
                lines.append(f"⏳ {awaiting_count} node(s) waiting for details (tap ✅ to provide context)")
                lines.append("")
        except Exception:
            pass

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
            # Add undo buttons for auto-processed items at the bottom of keyboard
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


async def discover_new_clusters():
    """Continuous cluster discovery. Analyzes unmapped resources for natural groupings
    and creates new clusters when 3+ related resources form a coherent theme."""
    try:
        unclustered_res = supabase.table('resources').select(
            'id, url, title, summary, strategic_note, category'
        ).is_('cluster_id', None).eq('is_current', True).limit(100).execute()
        unclustered = unclustered_res.data or []
        if len(unclustered) < 3:
            audit_log_sync("pulse", "INFO", f"📍 Cluster discovery: only {len(unclustered)} unmapped resources, need 3+.")
            return []

        existing_res = supabase.table('clusters').select('id, title').eq('status', 'active').execute()
        existing_titles = set(m['title'].lower() for m in (existing_res.data or []))
        existing_list = ", ".join(sorted(existing_titles)) or "None"

        resources_json = json.dumps([{
            "id": r['id'],
            "url": r.get('url', ''),
            "title": r.get('title', ''),
            "summary": r.get('summary', ''),
            "strategic_note": r.get('strategic_note', ''),
            "category": r.get('category', '')
        } for r in unclustered], indent=2)

        prompt = f"""You are a cluster discoverer. Review these unclustered resources.

Existing active clusters: {existing_list}

Rules:
- Identify any natural groupings of 3+ resources that form a coherent strategic theme NOT covered by existing active clusters.
- Only suggest a new cluster if at least 3 resources clearly belong together under a single strategic theme.
- If no such grouping exists, return an empty array.
- Do not suggest clusters that overlap with existing cluster titles.

Return ONLY valid JSON array:
[
  {{"cluster_title": "New Cluster Name", "resource_ids": [1, 2, 3], "description": "Strategic intent for this cluster"}}
]

Resources:
{resources_json}"""

        response = await generate_content_with_fallback(
            prompt=prompt,
            workload=WorkloadProfile.SYNTHESIS,
            primary_model=CLASSIFICATION_MODEL,
            config={'response_mime_type': 'application/json'},
            require_json=True
        )

        discovered = response.parse_json()
        if not isinstance(discovered, list):
            return []

        created = []
        ist_ts = datetime.now(timezone(timedelta(hours=5, minutes=30)))
        for item in discovered:
            title = item.get('cluster_title', '').strip()
            resource_ids = item.get('resource_ids', [])
            if not title or len(resource_ids) < 3:
                continue
            norm = normalize_cluster_title(title)
            if not norm or norm in existing_titles:
                continue
            description = item.get('description', f'Auto-discovered from {len(resource_ids)} related resources on {ist_ts.strftime("%Y-%m-%d")}.')
            insert_res = supabase.table('clusters').insert({
                "title": title,
                "status": "active",
                "description": description
            }).execute()
            if not insert_res.data:
                continue
            new_cluster_id = insert_res.data[0]['id']
            existing_titles.add(norm)
            supabase.table('resources').update({
                "cluster_id": new_cluster_id
            }).in_('id', resource_ids).execute()
            created.append(title)
            audit_log_sync("pulse", "INFO", f"🔗 Cluster discovery: created '{title}' with {len(resource_ids)} resources")

        if created:
            audit_log_sync("pulse", "INFO", f"✅ Cluster discovery created {len(created)} new clusters: {', '.join(created)}")
        else:
            audit_log_sync("pulse", "INFO", "📍 Cluster discovery: no new clusters found.")
        return created

    except Exception as e:
        audit_log_sync("pulse", "WARNING", f"⚠️ Cluster discovery error: {e}")
        return []


async def process_pulse(auth_secret: str = None, request_id: str = None, trigger: str = "api"):
    """
    Process pulse with optional request_id for idempotency.
    
    Args:
        auth_secret: Pulse secret for auth
        request_id: Unique ID for idempotency (e.g. from GitHub Actions)
        trigger: The source trigger (e.g. 'api', 'cron', 'github_action')
    """
    from core.lib.audit_logger import set_trace_id
    set_trace_id(request_id)
    
    from core.pulse.run_logger import create_pulse_run, complete_pulse_run
    from core.lib.redis_cache import acquire_lock, release_lock

    pulse_secret = os.getenv("PULSE_SECRET")
    if pulse_secret and auth_secret != pulse_secret:
        return {"error": "Unauthorized.", "status": 401}
        
    lock_key = "pulse_concurrency_lock"
    if not acquire_lock(lock_key, ttl=300):
        return {"success": False, "message": "Pulse or Decision Pulse already running. Concurrency lock active."}

    error_log = []
    run_id = None
    try:
        # 1. Idempotency Check
        if request_id:
            # Always use metadata->>request_id (JSONB) for idempotency
            # This works whether or not the dedicated column exists
            existing = supabase.table('raw_dumps') \
                .select('id, status') \
                .eq('metadata->>request_id', request_id) \
                .limit(1) \
                .execute()
            
            if existing.data:
                info("pulse", f"Idempotency: request_id {request_id} already processed")
                return {"success": True, "idempotent": True, "message": "Already processed"}
        
        # 🛡️ THE ZOMBIE RECOVERY: Reset any dumps stuck in 'processing' for more than 10 mins
        try:
            ten_mins_ago = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
            supabase.table('raw_dumps') \
                .update({"status": "staged"}) \
                .eq('status', 'processing') \
                .lt('created_at', ten_mins_ago) \
                .execute()
        except Exception as e:
            error("pulse", f"Zombie Recovery skipped: {e}", format_error(e))

        # --- 1.1 SECURITY GATEKEEPER ---
        pulse_secret = os.getenv("PULSE_SECRET")
        if pulse_secret and auth_secret != pulse_secret:
            return {"error": "Unauthorized manual trigger.", "status": 401}
        if not pulse_secret:
            warning("pulse", "PULSE_SECRET not set. Auth check bypassed.")

        # --- 1.2 PULSE RUN LOGGING (after auth) ---
        run_id = await create_pulse_run(supabase, "main", trigger)

        # --- 0. GOOGLE→SUPABASE SYNC (After auth check) ---
        tasks_service = get_tasks_service()
        try:
            completed_from_google = await asyncio.to_thread(sync_completed_tasks_from_google, supabase, tasks_service)
            for title, proj_name in (completed_from_google or []):
                await write_outcome_memory(title, proj_name)
        except Exception as e:
            error("pulse", f"Google tasks sync failed, continuing pulse: {e}", format_error(e))
        
        # --- 0.1 HEARTBEAT & HEALTH CHECK ---
        try:
            await update_heartbeat()
            health_report = await check_pipeline_health()
            audit_log_sync("pulse", "INFO", str(health_report))
        except Exception as e:
            warning("pulse", f"Heartbeat/Health check failed: {e}", format_error(e))
        
        # --- 0.3 CONVERSATION HISTORY (Phase 5) ---
        conversation_history = ""
        try:
            pulse_chat_id = int(os.getenv("TELEGRAM_CHAT_ID", "0"))
            if pulse_chat_id:
                session_id, hist_pairs, active_anchor = get_or_create_session(pulse_chat_id)
                if hist_pairs:
                    conversation_history = format_history_for_prompt(hist_pairs)
        except Exception as e:
            warning("pulse", f"Conversation history fetch failed: {e}")
        
        # --- 0.1 BATCH ENRICHMENT (One Gemini call for all unenriched resources) ---
        try:
            batch_enrich_results = await batch_enrich_resources()
        except Exception as e:
            error("pulse", f"Batch enrichment failed, continuing pulse: {e}", format_error(e))
            batch_enrich_results = []

        # --- 0.2 CONTINUOUS CLUSTER DISCOVERY ---
        try:
            await discover_new_clusters()
        except Exception as e:
            error("pulse", f"Cluster discovery failed, continuing pulse: {e}", format_error(e))
        
        # --- 1. READ: Fetch and Lock ---
        # 1.1 Fetch pending, staged, synced, partially_synced, and awaiting_completion_match items
        dumps_res = supabase.table('raw_dumps') \
            .select('id, content, metadata, status, message_type') \
            .in_('status', ['pending', 'staged', 'synced', 'partially_synced', 'awaiting_completion_match']) \
            .execute()

        all_dumps = dumps_res.data or []

        synced_dumps = [d for d in all_dumps if d.get('status') == 'synced']
        dumps = [d for d in all_dumps if d.get('status') != 'synced']

        completion_dump_ids = []
        
        if dumps:
            dump_ids = [d['id'] for d in dumps]
            
            # 🔒 THE LOCK: Immediately claim these for processing
            if request_id:
                # Store request_id in metadata for idempotency
                for d in dumps:
                    try:
                        raw_meta = d.get('metadata') or {}
                        if isinstance(raw_meta, str):
                            meta = json.loads(raw_meta) if raw_meta else {}
                        elif isinstance(raw_meta, dict):
                            meta = raw_meta
                        else:
                            meta = {}
                        meta['request_id'] = request_id
                        supabase.table('raw_dumps') \
                            .update({"metadata": meta}) \
                            .eq('id', d['id']) \
                            .execute()
                    except Exception:
                        pass
            
            supabase.table('raw_dumps') \
                .update({"status": "processing"}) \
                .in_('id', dump_ids) \
                .execute()
            
            audit_log_sync("pulse", "INFO", f"🔒 Locked {len(dump_ids)} dumps for processing.")

        active_tasks_res = supabase.table('tasks').select('id, title, project_id, organization_id, priority, created_at, reminder_at, google_event_id, direction, committed_to').eq('is_current', True).not_.in_('status', ['done', 'cancelled']).execute()
        active_tasks = active_tasks_res.data or []

        # --- 🗃️ STAGING AREA SORTER (Pre-Processor) ---
        if dumps:
            sort_prompt = f"""You are Danny's Rhodey. Pragmatic, loyal, and a professional friend. You are the grounding wire to Danny's vision. You don't coach or 'motivate.' Speak simply and punchy.

            PROHIBIT ACTION HALLUCINATION: You are a logging tool, not an agent. NEVER say 'I'll ping', 'I'll check', or 'I'll handle it'. You cannot contact people. Your only job is to confirm Danny's task is SECURED in his system.
            Categorize each input into one of five types:
            - TASK: Explicit action items, things to do, commitments, reminders.
            - PROJECT_UPDATE: Mixed content like status updates, team changes, finance/invoice mentions, decisions, or meeting fallout.
            - COMPLETION: Single-action past tense — "finished", "done", "sorted", "sent" (unambiguously closes one specific task)
            - NOTE: Ideas, insights, observations, learnings (not actionable)
            - NOISE: Casual conversation, acknowledgments, or low-value content
            Rhodey Rule: Be dismissive of NOISE. If it's low-value chatter, categorize it and keep the brief silent about it.
            If an input is 'Check with X,' categorize it as a TASK for Danny, never as something for the system to do.

            Return ONLY a valid JSON array (no markdown, no explanation):
            [{{"id": {dumps[0]['id']}, "category": "TASK|COMPLETION|NOTE|PROJECT_UPDATE|NOISE"}}, ...]

            Inputs:
            {json.dumps([{"id": d['id'], "content": d['content'][:500]} for d in dumps], indent=2)}"""
            
            try:
                sort_response = await generate_content_with_fallback(
                    prompt=sort_prompt,
                    workload=WorkloadProfile.SYNTHESIS,
                    primary_model=CLASSIFICATION_MODEL,
                    config={'response_mime_type': 'application/json'},
                    require_json=True
                )
                
                sort_result = sort_response.parse_json()
                
                task_dump_ids = []
                note_dump_ids = []
                completion_dump_ids = []
                
                for item in sort_result:
                    dump_id = item.get('id')
                    raw_dump = next((d for d in dumps if d['id'] == dump_id), None)
                    if raw_dump is None:
                        audit_log_sync("pulse", "WARNING", f"⚠️ Sorter: dump_id {dump_id} not found in dumps, skipping.")
                        continue
                    metadata = {}
                    try:
                        raw_meta = raw_dump.get('metadata')
                        if isinstance(raw_meta, str):
                            metadata = json.loads(raw_meta)
                        elif isinstance(raw_meta, dict):
                            metadata = raw_meta
                    except Exception as e:
                        audit_log_sync("pulse", "WARNING", f"⚠️ Metadata parse error for dump {dump_id}: {e}")

                    gemini_category = item.get('category', '').upper()
                    
                    dump_content = raw_dump.get('content', '')
                    if is_url_text(dump_content):
                        gemini_category = 'NOTE'
                        
                    category = gemini_category if gemini_category in ['TASK', 'NOTE', 'NOISE', 'COMPLETION', 'PROJECT_UPDATE'] else metadata.get('intent', 'NOISE').upper()
                    
                    if category in ('NOTE', 'PROJECT_UPDATE'):
                        dump_content = raw_dump.get('content')
                        if dump_content:
                            from core.pulse.tools import create_note_direct
                            result = await create_note_direct(content=dump_content, source="pulse_note")
                            if result.get("memory_id"):
                                note_dump_ids.append(dump_id)
                                audit_log_sync("pulse", "INFO", f"📝 Note filed to memory: {dump_content[:50]}...")
                    
                    elif category == 'NOISE':
                        note_dump_ids.append(dump_id)
                    
                    elif category == 'TASK':
                        task_dump_ids.append(dump_id)
                    
                    elif category == 'COMPLETION':
                        task_dump_ids.append(dump_id)
                        completion_dump_ids.append(dump_id)
                
                if note_dump_ids:
                    supabase.table('raw_dumps').update({"status": "completed", "is_processed": True}).in_('id', note_dump_ids).execute()
                    audit_log_sync("pulse", "INFO", f"🗃️ Staging Area: {len(task_dump_ids)} tasks, {len(note_dump_ids)} notes/noise")
                
                dumps = [d for d in dumps if d['id'] in task_dump_ids]
            
            except Exception as e:
                audit_log_sync("pulse", "ERROR", f"Staging Area Sort error: {e}")

        # 💡 Only silence the tool if BOTH new dumps AND open tasks are empty
        if not dumps and not active_tasks:
            await complete_pulse_run(supabase, run_id, status="completed",
                dumps_processed=0, tasks_created=0,
                metadata={"reason": "nothing_to_process"})
            return {"message": "Nothing to process, nothing to nag about. Silence is golden."}

        audit_log_sync("pulse", "INFO", f"🚀 PULSE START: Processing {len(dumps)} new dumps and {len(active_tasks)} active tasks.")
        audit_log_sync("pulse", "INFO", "📦 Step 1: Fetching metadata...")

        # Fetch supporting metadata
        core_res = supabase.table('core_config').select('key, content').execute()
        core = core_res.data or []

        # Fetch business context from graph
        graph_projects_res = supabase.table('graph_nodes').select('id', 'label', 'metadata').eq('type', 'project').eq('is_current', True).execute()
        graph_projects = graph_projects_res.data or []

        projects = []
        for gp in graph_projects:
            raw_meta = gp.get('metadata')
            if isinstance(raw_meta, str):
                try:
                    metadata = json.loads(raw_meta)
                except Exception:
                    metadata = {}
            elif isinstance(raw_meta, dict):
                metadata = raw_meta
            else:
                metadata = {}
            projects.append({
                'id': gp['id'],
                'name': gp['label'],
                'organization_name': metadata.get('organization_name', 'INBOX'),
                'description': metadata.get('description', ''),
                'legacy_id': metadata.get('legacy_id')
            })

        audit_log_sync("pulse", "INFO", "📦 Step 2: Fetching projects...")
        legacy_projects = await context_provider.get_projects()

        audit_log_sync("pulse", "INFO", "📦 Step 3: Fetching people...")
        people = await context_provider.get_people()
        
        orgs_list = await context_provider.get_organizations()
        org_map = {o['id']: o['name'] for o in orgs_list}

        audit_log_sync("pulse", "INFO", "📦 Step 4: Fetching clusters (skipped, unused)...")
        # --- 🕒 1.2 UNIFIED TIME & DAY INTELLIGENCE (IST) ---
        ist_offset = timezone(timedelta(hours=5, minutes=30))
        now = datetime.now(ist_offset)
        day = now.isoweekday()  # Monday=1, Sunday=7
        hour = now.hour

        # B5: Nuanced weekend filtering — transitions instead of binary
        # Friday 7PM+ and Saturday/Sunday = weekend mode
        # Sunday 7PM+ = pre-Monday (weekday mode resumes)
        is_weekend = (day == 6 or day == 7) or (day == 5 and hour >= 19)
        is_pre_monday = (day == 7 and hour >= 19)  # Sunday evening preloads Monday
        is_monday_morning = (day == 1 and hour < 11)

        if is_weekend and not is_pre_monday:
            briefing_mode = "⚪ CHORES & 💡 IDEAS (Weekend Rest)"
            system_persona = "Focus ONLY on Home, Family, and Chores. Explicitly hide Work tasks. Be relaxed."
        elif is_pre_monday:
            briefing_mode = "🌙 Pre-Monday: Loading the board."
            system_persona = "Pre-load Monday. Show Work tasks that start tomorrow. Keep Home visible but deprioritized. Be direct."
        else:
            # 🌅 MORNING: Extended to Noon to catch your first run
            if hour < 12:
                briefing_mode = "Morning Status: We're cleared."
                system_persona = "Cut through the noise and focus Danny on what moves the needle today. No coaching, no motivation—just what needs doing."
            # ☀️ AFTERNOON: Focused execution window (Noon to 3:30 PM)
            elif hour < 15 or (hour == 15 and now.minute < 30):
                briefing_mode = "Afternoon Check: Moving the needle."
                system_persona = "Focused on the main effort. Keep Danny building toward the goal. Be direct."
            # 🌇 CLOSING LOOP: Gear shift to family (3:30 PM to 6:30 PM)
            elif hour < 19:
                if day == 5:
                    briefing_mode = "Closing the loop: Friday sign off."
                    system_persona = "Push Danny to close work tasks so he can transition to weekend. Log pending items. Be dry."
                else:
                    briefing_mode = "Closing the loop: Sign off."
                    system_persona = "Push Danny to close work tasks so he can transition to family. Log pending items. Be dry."
            # 🌙 NIGHT: Secure the board (After 7:00 PM)
            else:
                briefing_mode = "Intel: Vaulted."
                system_persona = "Focus on closure and transition. Secure the board. Highlight what was ✅ Done today and what matters on the 🏠 Home front. Keep work loops minimal but visible. Maintain the 'Grid'—vertical sections are mandatory."

        # --- 1.3 BANDWIDTH & BUFFER CHECK ---
        is_overloaded = len(active_tasks) > 15

        # --- 1.3 PRIORITY DECAY (T1) ---
        # Auto-downgrade stale 'urgent' tasks to 'high' after 7 days
        try:
            seven_days_ago_iso = (now - timedelta(days=7)).isoformat()
            stale_urgent = supabase.table('tasks')\
                .select('id, title, created_at')\
                .eq('is_current', True)\
                .eq('status', 'todo')\
                .eq('priority', 'urgent')\
                .lt('created_at', seven_days_ago_iso)\
                .execute()
            for st in (stale_urgent.data or []):
                supabase.table('tasks').update({'priority': 'high'}).eq('id', st['id']).execute()
                try:
                    record_decision(
                        decision_type="priority_decay",
                        title=f"Priority decay: '{st['title']}' urgent → high",
                        context="Task was 'urgent' for more than 7 days without progress. Auto-downgraded.",
                        entity_type="task",
                        entity_id=str(st['id']),
                        confidence=1.0,
                        source="pulse_engine",
                        auto_decided=True,
                    )
                except Exception as dec_err:
                    audit_log_sync("pulse", "WARNING", f"Failed to record priority decay decision: {dec_err}")
                audit_log_sync("pulse", "INFO", f"📉 Priority decay: '{st['title']}' urgent → high (>{7}d stale)")
        except Exception as dec_err:
            audit_log_sync("pulse", "WARNING", f"Priority decay check failed: {dec_err}")

        # --- 1.3.1 STRATEGIC TASK FILTERING (Robust Horizon Guard) ---
        filtered_tasks = []
        horizon_cutoff = now + timedelta(days=2)

        for t in active_tasks:
            raw_reminder = t.get('reminder_at')
            
            if raw_reminder:
                try:
                    # 🛡️ THE CLEANER: Replace space with 'T' and 'Z' with UTC offset
                    clean_reminder = str(raw_reminder).replace(' ', 'T').replace('Z', '+00:00')
                    task_date = datetime.fromisoformat(clean_reminder)
                    
                    # 🛡️ TIMEZONE AWARENESS: Ensure we are comparing Apples to Apples (IST)
                    if task_date.tzinfo is None:
                        task_date = task_date.replace(tzinfo=ist_offset)
                    
                    # 🛡️ THE HORIZON CHECK: If task is > 2 days away, SKIP IT.
                    if task_date > horizon_cutoff:
                        continue 
                except Exception as e:
                    # If it still fails, we log it but keep the task visible for safety
                    audit_log_sync("pulse", "WARNING", f"⚠️ Horizon Guard bypassed for '{t.get('title')}': {e}")

            # --- Existing Category Logic ---
            if t.get('priority') == 'urgent':
                filtered_tasks.append(t)
                continue

            project = next((p for p in legacy_projects if p.get('id') == t.get('project_id')), None)
            o_id = t.get('organization_id') or (project.get('organization_id') if project else None)
            o_name = org_map.get(o_id, 'INBOX')

            personal_orgs = ['Personal', 'Ashraya', 'Ashraya Chennai', 'Chennai North', 'Chennai Central', 'Chennai India']

            if is_weekend:
                if any(po in o_name for po in personal_orgs):
                    filtered_tasks.append(t)
            elif hour < 19:
                if not any(po in o_name for po in personal_orgs) or o_name == 'INBOX':
                    filtered_tasks.append(t)
            else:
                if any(po in o_name for po in personal_orgs):
                    filtered_tasks.append(t)

        # --- 1.4 CONTEXT COMPRESSION & PRUNING ---
        # 🛡️ THE HORIZON GATE (Rule 2)
        horizon_cutoff = now + timedelta(days=2)
        # 🛡️ THE NAG GATE (Rule 1)
        two_weeks_ago = now - timedelta(days=14)
        
        recent_tasks = []
        for t in active_tasks:
            try:
                # 🛡️ RULE 2: If the reminder is more than 48 hours away, HIDE IT FROM THE AI
                raw_remind = t.get('reminder_at')
                if raw_remind:
                    clean_remind = str(raw_remind).replace(' ', 'T').replace('Z', '+00:00')
                    remind_dt = datetime.fromisoformat(clean_remind)
                    if remind_dt > horizon_cutoff:
                        continue # Dawn (May 7) is skipped here!

                # 🛡️ RULE 1: Only show recently created tasks for background context
                created_dt = datetime.fromisoformat(t['created_at'].replace('Z', '+00:00'))
                if created_dt > two_weeks_ago:
                    recent_tasks.append(t)
            except Exception:
                recent_tasks.append(t) # Safety fallback

        # Universal task map is now handled by context_provider

        # B. BUILD COMPRESSED LIST (For the Briefing Context)
        # 🛡️ FIX: Defining 'compressed_tasks' so the prompt builder doesn't crash!
        compressed_tasks_list = []
            
        for t in filtered_tasks:
            project = next((p for p in legacy_projects if p.get('id') == t.get('project_id')), None)
            p_name = project.get('name') if project else "General"
            
            o_id = t.get('organization_id') or (project.get('organization_id') if project else None)
            o_name = org_map.get(o_id, 'INBOX')
            loc = f"{o_name} · {p_name}" if p_name != "General" else o_name
                
            dir_str = ""
            if t.get('direction') == 'waiting_on':
                dir_str = f" [WAITING ON: {t.get('committed_to', 'someone')}]"
            elif t.get('direction') == 'outbound':
                dir_str = f" [OWED TO: {t.get('committed_to', 'someone')}]"
            
            compressed_tasks_list.append(f"[{loc}] {t.get('title')} ({t.get('priority')}){dir_str} [ID:{t.get('id')}]")

        # --- 1.5 SEASON EXPIRY LOGIC ---
        season_row = next((c for c in core if c.get('key') == 'current_season'), None)
        season_config = season_row.get('content') if season_row else ''

        expiry_match = re.search(r'\[EXPIRY:\s*(\d{4}-\d{2}-\d{2})\]', season_config)
        system_context = "OPERATIONAL"
        if expiry_match:
            expiry_date_str = expiry_match.group(1)
            expiry_date = datetime.strptime(expiry_date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            if now > expiry_date:
                system_context = "CRITICAL: Season Context EXPIRED."

        # --- 🛡️ 1.6 THE NAG LOGIC (STAGNANT TASK GUARD) ---
        overdue_tasks = []
        for t in filtered_tasks:
            try:
                raw_created = t.get('created_at')
                if raw_created:
                    # Normalize and compare hours
                    created_date = datetime.fromisoformat(raw_created.replace("Z", "+00:00"))
                    hours_old = (now - created_date).total_seconds() / 3600
                    if t.get('priority') == 'urgent' and hours_old > 48:
                        overdue_tasks.append(t.get('title'))
            except Exception as e:
                audit_log_sync("pulse", "WARNING", f"⚠️ Nag Logic skipped for task '{t.get('title')}': {e}")

        # --- 🕒 1.7 STALE TASK ALERT ---
        sevendays_ago = (now - timedelta(days=7)).isoformat()
        stale_tasks = [
            t for t in active_tasks
            if t.get('status') == 'todo'
            and t.get('created_at', '') < sevendays_ago
            and t.get('title') not in overdue_tasks
        ]
        stale_tasks = sorted(stale_tasks, key=lambda t: t.get('created_at', ''))[:5]

        if stale_tasks:
            stale_lines = []
            for t in stale_tasks:
                try:
                    created = datetime.fromisoformat(t.get('created_at', '').replace('Z', '+00:00'))
                    days_old = (now - created).days
                    stale_lines.append(f"- {t.get('title', '')} (stale {days_old}d)")
                except Exception:
                    pass
            stale_context = "\n".join(stale_lines)
        else:
            stale_context = None

        def _enrich(d: dict) -> str:
            content = d.get('content', '')
            meta = d.get('metadata') or {}
            if isinstance(meta, str):
                try:
                    meta = json.loads(meta)
                except Exception:
                    meta = {}
            tid = meta.get('task_update_id')
            return f"⚠️ TASK UPDATE (task #{tid}): {content}" if tid else content

        # --- 🕒 1.8 INPUT PREP ---
        new_inputs_text = "\n---\n".join([_enrich(d) for d in dumps]) if dumps else "None"
        
        # --- 🧠 DRIFT DETECTION (Temporal Lineage) ---
        drift_alerts = []
        for proj in (legacy_projects or []):
            proj_name = get_project_name(proj)
            try:
                drift = detect_drift(proj_name, hours_window=48)
                if drift and drift.get('update_count', 0) >= 3:
                    drift_alerts.append(f"⚠️ DRIFT ALERT: Project '{proj_name}' changed {drift['update_count']} times in 48h. Bottleneck?")
            except Exception as e:
                audit_log_sync("pulse", "WARNING", f"Drift detection failed for {proj_name}: {e}")
        
        drift_context = "\n".join(drift_alerts) if drift_alerts else "None"
        
        # --- 🧭 LAYER 3: SMART PATTERN CONTEXT (Last 30 Days) ---
        # Look back 30 days so patterns can form over time, not just items
        thirty_days_ago = (now - timedelta(days=30)).isoformat()

        # --- 🧠 HIGH-RES HINDSIGHT RETRIEVAL (Hybrid Graph + Vector) ---
        hindsight_context = "None"
        task_inputs = [d['content'] for d in dumps] if dumps else []

        # 🕸️ ADD-ON: Graph-aware person→task context (non-blocking)
        # Reusing people and graph_projects fetched earlier
        graph_node_projects = graph_projects
        if people and active_tasks:
            graph_task_context = await fetch_graph_task_context(people, active_tasks)
        else:
            graph_task_context = ""

        # --- 📦 HINDSIGHT: Graph-first, then vector ---
        # Extract entity terms from people + projects for seeded vector search
        all_entity_terms = [p['name'] for p in people] + [p['label'] for p in graph_node_projects]

        hindsight_memories, hindsight_timestamp = await retrieve_hindsight_memories(
            task_inputs,
            active_tasks,
            entity_terms=all_entity_terms
        )

        memory_lines = []
        memory_lines.extend(hindsight_memories)
        hindsight_block = "\n".join(memory_lines)

        if hindsight_memories:
            hindsight_context = hindsight_block
            audit_log_sync("pulse", "INFO", f"🧠 Hindsight found {len(hindsight_memories)} relevant memories")

        is_hindsight_stale = False
        hindsight_empty = False
        if hindsight_timestamp:
            last_seen = datetime.fromisoformat(hindsight_timestamp.replace('Z', '+00:00'))
            if (now - last_seen).total_seconds() > (72 * 3600):
                is_hindsight_stale = True
        else:
            hindsight_empty = True

        recent_lib = supabase.table('resources')\
            .select('id, url, category, title, summary, strategic_note, created_at')\
            .gt('created_at', thirty_days_ago)\
            .eq('is_current', True)\
            .order('created_at', desc=True)\
            .limit(50)\
            .execute()

        if recent_lib.data:
            enriched_items = []
            for r in recent_lib.data:
                note = r.get('strategic_note') or ""
                enriched_items.append(f"[ID:{r['id']}] [{r['category']}] {r['title']} | {note}".strip())
            pattern_context = " | ".join(enriched_items)
        else:
            pattern_context = "None"
        
        newly_enriched_context = "None"
        if batch_enrich_results:
            newly_enriched_lines = [f"[ID:{r.get('id', '?')}] [{r.get('category', 'LINK')}] {r.get('title', 'Unknown')} | {r.get('strategic_note', '')}" for r in batch_enrich_results]
            newly_enriched_context = " | ".join(newly_enriched_lines)
        
        # Re-use recent_lib data for URLs instead of querying again
        if recent_lib.data:
            url_lines = []
            for r in recent_lib.data[:30]:  # Limit to 30 as before
                label = r.get('title') or r.get('url', 'Unknown')
                cat = r.get('category') or 'RAW'
                note = r.get('strategic_note') or ''
                url_lines.append(f"[ID:{r['id']}] [{cat}] {label} | {note}".strip().rstrip('| '))
            recent_urls_context = "\n".join(url_lines)
        else:
            recent_urls_context = "None"
        
        # 🧠 RECENT MEMORIES & GRAPH CONTEXT (Cross-Referenced Hybrid Search)
        mem_query = " | ".join([t.get('title', '') for t in filtered_tasks[:5]])
        recent_memories_context = await context_provider.get_cross_referenced_context(
            mem_query, 
            task_inputs, 
            people, 
            graph_node_projects, 
            match_count=5
        )

        active_clusters_res = supabase.table('clusters').select('title, description').eq('status', 'active').execute()
        if active_clusters_res.data:
            active_clusters_context = "\n".join([f"- {c['title']}: {c.get('description', '')}" for c in active_clusters_res.data])
        else:
            active_clusters_context = "None"
        
        # 🤖 AGENT 1: DEPENDENCY AGENT (uses graph_edges for task dependencies)
        dependency_context = await check_task_dependencies(active_tasks)
        
        # 👥 AGENT 2: SOCIAL GRAPH OPTIMIZER (communication patterns)
        social_graph_context = await analyze_communication_patterns(people)
        
        # 📅 AGENT 3: TEMPORAL PATTERN DETECTOR (on this day insights)
        temporal_context = await detect_temporal_patterns()
        
        # 🤖 AGENT 4: SERENDIPITY ENGINE (cross-domain connections)
        # S6: Inject weekly patterns for cross-domain insight
        weekly_patterns_str = ""
        try:
            wp_row = next((c for c in core if c.get('key') == 'weekly_patterns'), None)
            if wp_row and wp_row.get('content'):
                weekly_patterns_str = wp_row['content']
        except Exception:
            pass
        serendipity_context = await serendipity_engine(active_tasks, people, recent_lib.data or [], pattern_context=weekly_patterns_str or None)
        
        # 🕸️ AGENT 4.5: GRAPH CENTRALITY (hub detection)
        centrality_context = await get_graph_centrality_context()
        
        # 🤖 AGENT 5: ADAPTIVE BRIEFING LEARNER (learns from briefing patterns)
        adaptive_context = "None"
        if now.weekday() == 6:  # Run only on Sundays
            adaptive_context = await adaptive_briefing_learner()
        
        # 🧠 SESSION MEMORY: Fetch the summary of the last pulse
        try:
            last_pulse_row = next((c for c in core if c.get('key') == 'last_pulse_summary'), None)
            session_memory = last_pulse_row['content'] if last_pulse_row else "None"
        except Exception:
            session_memory = "None"

        # --- 📊 DELTA BRIEFING: Compare with briefing history (last 5 snapshots) ---
        delta_context = "None"
        try:
            hist_row = next((c for c in core if c.get('key') == 'briefing_history'), None)
            history = json.loads(hist_row['content']) if hist_row and hist_row.get('content') else []

            curr_task_ids = set(str(t.get('id')) for t in filtered_tasks)

            # Compare against the most recent snapshot
            if history:
                prev = history[0]
                prev_task_ids = set(prev.get('task_ids', []))
                prev_completed_ids = set(prev.get('completed_ids', []))
                new_ids = curr_task_ids - prev_task_ids
                dropped_ids = prev_task_ids - curr_task_ids - prev_completed_ids
                delta_lines = []
                if new_ids:
                    new_titles = [t.get('title', '') for t in filtered_tasks if str(t.get('id')) in new_ids]
                    delta_lines.append(f"🆕 NEW since last briefing: {', '.join(new_titles[:5])}")
                if dropped_ids:
                    delta_lines.append(f"📍 {len(dropped_ids)} task(s) moved off the active board")
                if delta_lines:
                    delta_context = "\n".join(delta_lines)
                else:
                    delta_context = "No significant changes since last briefing."

                # Multi-briefing pattern detection (if 3+ snapshots)
                if len(history) >= 3:
                    recurring_drops = set()
                    for prev_snap in history[:5]:
                        prev_ids = set(prev_snap.get('task_ids', []))
                        dropped_ids_prev = prev_ids - curr_task_ids
                        recurring_drops.update(dropped_ids_prev)
                    if recurring_drops and len(recurring_drops) >= 2:
                        delta_lines.append(f"🔄 {len(recurring_drops)} task(s) appeared and dropped across multiple briefings — review?")
                        delta_context = "\n".join(delta_lines)
            else:
                delta_context = "First briefing — no history to compare."

            # Store current snapshot (prepend, keep last 5)
            curr_snapshot = {
                'task_ids': list(curr_task_ids),
                'completed_ids': [],
                'timestamp': now.isoformat()
            }
            history.insert(0, curr_snapshot)
            history = history[:5]  # Keep last 5
            chk = supabase.table('core_config').select('id').eq('key', 'briefing_history').execute()
            if chk.data:
                supabase.table('core_config').update({"content": json.dumps(history)}).eq('key', 'briefing_history').execute()
            else:
                supabase.table('core_config').insert({"key": "briefing_history", "content": json.dumps(history)}).execute()
        except Exception as e:
            audit_log_sync("pulse", "WARNING", f"Delta briefing history error: {e}")
        
        audit_log_sync("pulse", "INFO", "📦 Step 5: Building context...")
        # --- 2. THINK Phase ---
        audit_log_sync("pulse", "INFO", '🤖 Building prompt...')

        # Phase 2: Context Hydration Engine
        query_focus = f"Briefing for {briefing_mode}"
        compressed_tasks_final, universal_task_map = await context_provider.hydrate_tasks_context(query_focus)
        new_inputs_text = "\n---\n".join([_enrich(d) for d in dumps])
        new_input_summary = " | ".join([_enrich(d) for d in dumps[:5]])

        # Removed synced_inputs_text completely to prevent LLM from double processing inline updates
        current_time_str = now.strftime("%A, %B %d, %Y at %I:%M %p IST")

        # --- 🧭 LAYER 4: CANONICAL SYNTHESIS (The Master Pages) ---
        master_page_context = ""
        relevant_project_names = list(set([
            next((p.get('name') for p in legacy_projects if p.get('id') == t.get('project_id') and p.get('status') == 'active'), "General")
            for t in filtered_tasks
        ]))

        if relevant_project_names:
            or_string = ",".join([f"title.ilike.%{name}%" for name in relevant_project_names])
            pages_res = supabase.table('canonical_pages').select('title, content').eq('is_current', True).or_(or_string).execute()
            if pages_res.data:
                page_entries = [f"[CANONICAL CONTEXT ONLY — DO NOT LIST IN BRIEFING]\n### MASTER PAGE: {p['title']}\n{p['content']}" for p in pages_res.data]
                master_page_context = "\n\n".join(page_entries)
                audit_log_sync("pulse", "INFO", f"🧠 Canonical: Loaded {len(pages_res.data)} Master Pages for context.")

        # --- 🏃 PRACTICE DETECTION (Weekends only, before brief) ---
        new_practice_ids = {}
        new_practice_labels = []
        correlation_insights = []
        if is_weekend:
            # Practice detection runs once a week — Saturday before 2PM IST (accounts for GH Actions delay)
            is_discovery_pulse = now.weekday() == 5 and now.hour < 14
            if is_discovery_pulse:
                audit_log_sync("pulse", "INFO", "📍 Weekend pulse: Running practice detection...")
                before_labels = set()
                before_res = supabase.table('graph_nodes').select('label').eq('type', 'practice').eq('is_current', True).execute()
                for r in (before_res.data or []):
                    before_labels.add(r['label'])
                new_practice_ids = await detect_practices() or {}
                after_res = supabase.table('graph_nodes').select('label').eq('type', 'practice').eq('is_current', True).execute()
                after_labels = set(r['label'] for r in (after_res.data or []))
                new_practice_labels = sorted(after_labels - before_labels)
                if new_practice_labels:
                    audit_log_sync("pulse", "INFO", f"📍 New practices detected: {new_practice_labels}")

            # 🕸️ Build PRECEDES/FOLLOWED_BY edges between practices
            await build_practice_edges()

            # 📊 Build task-practice correlations
            correlation_insights = await build_practice_correlations()
            if correlation_insights:
                audit_log_sync("pulse", "INFO", f"📍 Practice correlations: {len(correlation_insights)} insights")

            # 📝 Sync canonical pages for practices
            await sync_practice_canonical_pages()

        # 📅 Fetch calendar context (Google + Outlook) for today
        target_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
        calendar_context = await context_provider.get_calendar_context_formatted(target_day)

        # --- 🧭 LAYER 4: MORNING PULSE NARRATIVE ---
        from core.pulse.context_salience import generate_morning_pulse
        morning_pulse_narrative = ""
        if "Morning Status" in briefing_mode and relevant_project_names:
            morning_pulse_narrative = generate_morning_pulse(relevant_project_names)

        from core.prompts.briefing import build_pulse_briefing_prompt, build_pulse_system_instruction

        # --- MAP VARIABLES FOR PROMPT BUILDER ---
        canonical_context = master_page_context
        cluster_task_list = compressed_tasks_final
        new_inputs = new_inputs_text
        session_memory_context = session_memory

        practices_parts = []
        if new_practice_labels:
            practices_parts.append(f"New practices detected: {', '.join(new_practice_labels)}")
        if correlation_insights:
            practices_parts.extend(correlation_insights)
        practices_context = "\n".join(practices_parts) if practices_parts else "None"

        urgency_parts = []
        if overdue_tasks:
            urgency_parts.append("⚠️ OVERDUE URGENT:")
            for ot in overdue_tasks:
                urgency_parts.append(f"  - {ot}")
        if stale_context:
            urgency_parts.append("⏳ STALE LOOPS:")
            urgency_parts.append(stale_context)
        urgency_lists = "\n".join(urgency_parts) if urgency_parts else "None"

        new_input_tags = " | ".join([d.get('status', '') for d in dumps[:5]]) if dumps else "None"

        people_names = ", ".join([p['name'] for p in people])

        prompt = build_pulse_briefing_prompt(
            conversation_history, season_config, briefing_mode, current_time_str,
            is_overloaded, is_monday_morning, json.dumps(overdue_tasks), stale_context, system_context,
            is_hindsight_stale, hindsight_empty, calendar_context, recent_memories_context,
            hindsight_context, weekly_patterns_str, graph_task_context, morning_pulse_narrative,
            serendipity_context, canonical_context, delta_context, practices_context,
            cluster_task_list, urgency_lists, new_inputs, new_input_tags, session_memory_context,
            pattern_context=pattern_context,
            newly_enriched_context=newly_enriched_context,
            recent_urls_context=recent_urls_context,
            active_clusters_context=active_clusters_context,
            dependency_context=dependency_context,
            social_graph_context=social_graph_context,
            temporal_context=temporal_context,
            centrality_context=centrality_context,
            adaptive_context=adaptive_context,
            people_names=str(people_names),
            universal_task_map=str(universal_task_map[:3000]),
            core=json.dumps(core),
        )

        # --- PROJECT ROUTING (for system instruction) ---
        project_routing_logic = f"""
        PROJECT AND ORGANIZATION ROUTING LOGIC:
        Match each task to the MOST SPECIFIC active project using the hierarchy below.
        If a task belongs to an organization but has no specific project, use the organization name instead.
        Never default client or business work to Inbox.

        Active Organization Hierarchy and Projects:
        {build_routing_context(legacy_projects, orgs_list)}

        Routing rules:
        1. Use project name or organization name EXACTLY as shown above.
        2. If a task mentions a keyword, person, or topic from a project's description/keywords, use that project.
        3. For client work, assign to the client organization. For internal work, assign to the organization doing the work.
        
        NEW PROJECT CREATION CRITERIA:
        - Create new projects ONLY when there is a clear commanding instruction to start a new project or engagement.
        - Provide "organization_name" (the primary owning or client organization).
        - Provide "client_organization_name" if different from the primary organization.
        - Provide "description" (one-sentence summary).
        - Provide "keywords" (array of relevant names/topics).
        - Do not invent domains.
        """

        # B4: Load briefing history to avoid repetition
        briefing_history_context = _get_recent_briefings_context()

        # --- BUILD SYSTEM INSTRUCTION (via briefing.py) ---
        system_instruction_text = build_pulse_system_instruction(
            system_persona=system_persona,
            briefing_history_context=briefing_history_context,
            routing_logic=project_routing_logic,
            drift_context=drift_context,
        )

        # --- AI GENERATION ---
        # 🛡️ Step 1: Initialize variables to prevent "UnboundLocalError"
        ai_data = {
            "briefing": f"⚠️ FALLBACK MODE\n\n{len(dumps)} new inputs:\n{new_input_summary[:200]}",
            "new_tasks": [], "logs": [], "completed_task_ids": [], "new_projects": [], "new_people": [],
            "resources": []
        }

        try:
            # 🛡️ Step 2: Agent Loop (Tool Registry + Fallback)
            from core.pulse.agent import run_agent_loop
            from core.pulse.tools import rhodey_tools
            
            config = {
                'system_instruction': system_instruction_text,
                'tools': rhodey_tools.get_tools_list()
            }
            
            briefing_text = await run_agent_loop(
                prompt=prompt,
                model=SYNTHESIS_MODEL,
                config=config,
                max_steps=10
            )
            
            audit_log_sync("pulse", "INFO", "✅ Agent loop completed successfully.")

            # Formatting cleanup for Telegram
            if briefing_text:
                # Provide breathing room for section headers
                headers = ['🚀 Work', '🏠 Home', '⛪ Church', '💡 Ideas', '✅ Done', '🛡️ WEEKEND RECON']
                for header in headers:
                    if header in briefing_text:
                        briefing_text = briefing_text.replace(header, f"\n\n{header}\n")

                briefing_text = briefing_text.replace('\\n', '\n').replace(' - ', '\n- ')
                briefing_text = re.sub(r'\[?ID:\s*\d+\]?', '', briefing_text, flags=re.IGNORECASE).strip()
                briefing_text = re.sub(r'\b(\d{2,})\s+(?:is the|task|loop|item|#|ref|id)\b', r'\1', briefing_text, flags=re.IGNORECASE)
                briefing_text = re.sub(r'\n{3,}', '\n\n', briefing_text)

        except Exception as e:
            audit_log_sync("pulse", "ERROR", f"Agent Execution Error: {e}")
            briefing_text = f"Pulse failed during execution: {e}"

        # --- 🏃 RHYTHMS SECTION (Weekends only) ---
        if is_weekend:
            try:
                rhythms_text = await build_rhythms_section(new_practice_labels=new_practice_labels, new_practice_ids=new_practice_ids, correlations=correlation_insights)
                if rhythms_text:
                    if briefing_text:
                        briefing_text += "\n\n" + rhythms_text
                    else:
                        briefing_text = rhythms_text
            except Exception as rhythms_err:
                audit_log_sync("pulse", "WARNING", f"⚠️ Rhythms section failed: {rhythms_err}")

        # --- 🧠 WHAT I LEARNED THIS WEEK (Sunday only) ---
        if day == 7 and not is_pre_monday:
            try:
                from core.lib.pattern_extractor import build_transparency_report
                learned_text = await build_transparency_report()
                if learned_text:
                    if briefing_text:
                        briefing_text += "\n\n" + learned_text
                    else:
                        briefing_text = learned_text
            except Exception as learn_err:
                audit_log_sync("pulse", "WARNING", f"⚠️ Transparency report failed: {learn_err}")

        # Append error summary to briefing if any failures occurred
        if error_log:
            briefing_text += "\n\n⚠️ " + str(len(error_log)) + " item(s) need attention — check logs."
        
        telegram_chat_id = os.getenv("TELEGRAM_CHAT_ID")

        send_success = False
        if telegram_chat_id and briefing_text:
            send_success = await send_telegram(
                chat_id=int(telegram_chat_id),
                message_text=briefing_text,
                show_keyboard=False
            )
        
        # B4: Store briefing history for future context
        if send_success and briefing_text:
            _store_briefing_to_history(briefing_text)

        # --- P4: Push notification for briefing ---
        if briefing_text:
            try:
                push_title = "Rhodey Briefing"
                # Truncate body to ~120 chars for lock-screen preview
                push_body = briefing_text.strip().replace('*', '').replace('_', '').replace('\n', ' ')[:120].strip()
                await send_push_notification(
                    title=push_title,
                    body=push_body,
                    data={"type": "briefing"},
                )
            except Exception as push_err:
                audit_log_sync("pulse", "WARNING", f"Push notification failed (non-critical): {push_err}")

        # Log Pulse briefing to raw_dumps so it appears in web UI
        if send_success and briefing_text:
            try:
                supabase.table('raw_dumps').insert([{
                    "content": briefing_text,
                    "status": "completed",
                    "is_processed": True,
                    "direction": "incoming",
                    "sender": "system",
                    "message_type": "briefing",
                    "metadata": {"source": "pulse", "hour": hour}
                }]).execute()
                
                # Update Session Memory
                summary_prompt = f"Summarize this briefing in 1-2 sentences. Focus on what was assigned, recommended, or asked. Briefing:\n{briefing_text}"
                summary_res = await generate_content_with_fallback(
                    prompt=summary_prompt, 
                    workload=WorkloadProfile.SYNTHESIS,
                    require_json=False
                )
                if summary_res and summary_res.text:
                    # check if row exists
                    chk = supabase.table('core_config').select('id').eq('key', 'last_pulse_summary').execute()
                    if chk.data:
                        supabase.table('core_config').update({"content": summary_res.text.strip()}).eq('key', 'last_pulse_summary').execute()
                    else:
                        supabase.table('core_config').insert({"key": "last_pulse_summary", "content": summary_res.text.strip()}).execute()
            except Exception as log_err:
                audit_log_sync("pulse", "WARNING", f"Failed to log/summarize briefing: {log_err}")

        # --- 📝 AFTER-ACTION REPORT ---
        if hour >= 20 or hour < 4:
            await generate_after_action_report()

        # ✅ COMPLETION DUMP CLOSER — seal the raw dumps that were completion signals
        # Also ensure matching tasks are actually marked done in the DB
        if completion_dump_ids:
            if ai_data.get('completed_task_ids'):
                from core.pulse.tools import update_task_status
                for ct in ai_data['completed_task_ids']:
                    try:
                        result = update_task_status(task_id=ct.id, status=ct.status)
                        audit_log_sync("pulse", "INFO", f"Completion closer: update_task_status({ct.id}, {ct.status}) → {result[:100]}")
                    except Exception as close_err:
                        audit_log_sync("pulse", "ERROR", f"Completion closer failed for task {ct.id}: {close_err}")
                supabase.table('raw_dumps').update({"status": "completed", "is_processed": True}).in_('id', completion_dump_ids).execute()
                audit_log_sync("pulse", "INFO", f"✅ Sealed {len(completion_dump_ids)} completion dumps.")
            else:
                audit_log_sync("pulse", "INFO", f"Skipped sealing {len(completion_dump_ids)} completion dumps — no tasks matched.")

        # --- AUTO-EXPIRY: End recurring tasks whose RRULE UNTIL has passed ---
        _auto_expire_recurring_tasks()

        # --- PHASE 3: Processed Gate ---
        if dumps:
            dump_ids = [d['id'] for d in dumps]
            supabase.table('raw_dumps').update({
                "status": "completed",
                "is_processed": True 
            }).in_('id', dump_ids).execute()
            audit_log_sync("pulse", "INFO", f"✅ Phase 3: Marked {len(dump_ids)} dumps as completed.")

        if synced_dumps:
            synced_ids = [d['id'] for d in synced_dumps]
            supabase.table('raw_dumps').update({
                "status": "completed",
                "is_processed": True
            }).in_('id', synced_ids).execute()
            audit_log_sync("pulse", "INFO", f"✅ Sealed {len(synced_ids)} synced dumps after briefing.")

        tasks_created = len(ai_data.get("new_tasks", [])) if ai_data else 0
        await complete_pulse_run(supabase, run_id, status="completed",
            dumps_processed=len(dumps) if dumps else 0,
            tasks_created=tasks_created)
        release_lock(lock_key)
        return {"success": True, "briefing": briefing_text}

    except Exception as e:
        import traceback
        audit_log_sync("pulse", "CRITICAL", f"Pulse Critical Error: {e}")
        traceback.print_exc()
        await complete_pulse_run(supabase, run_id, status="failed", error_message=str(e))
        release_lock(lock_key)
        return {"error": str(e)}