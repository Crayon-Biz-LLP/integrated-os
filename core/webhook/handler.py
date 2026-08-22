import os
import json
import re
import uuid
import asyncio
from datetime import datetime, timezone, timedelta
from core.lib.time_utils import now_ist, IST_TIMEZONE
from core.lib.audit_logger import trace_id_var, audit_log_sync
from core.lib.telemetry import emit_observation
from core.lib.decision_audit import set_decision_chain_id, log_decision, DecisionStage
from core.lib.query_timer import start_timer, mark, report
from core.lib.conversation import get_or_create_session, get_history, log_exchange, format_classify_context, _fresh_anchor
from core.actions import capture_session_id, capture_response
from core.webhook.telegram import send_telegram, download_telegram_file, answer_callback_query
from core.lib.rhodey_voice import ok, fail, ack_merged, ack_rejected, ack_undone, ack_verified
from core.webhook.classify import classify_intent, check_task_overlap_for_update, UPDATE_TRIGGER_WORDS, INTENT_THRESHOLDS
from core.webhook.utils import supabase, trigger_github_pulse, get_recent_context
from core.services.db import maybe_single_safe
try:
    from core.services.async_db import async_select, async_select_one
except Exception:
    async_select = None
    async_select_one = None
from core.webhook.email import process_email_pending_decision, handle_ed_command
from core.webhook.workflows import check_and_resume_workflow
from core.webhook.utils import process_channel_pending_decision



from core.pulse.graph import process_graph_pending_decision, process_pending_edge_decision
from core.webhook.graph import interpret_graph_corrections, apply_graph_actions
from core.lib.clarification_state import (
    get_active_clarification, get_active_session, set_clarification, set_session_state,
    resolve_clarification, clear_session
)
from core.webhook.dispatch import route_by_intent, ask_task_update_confirmation, resolve_task_update_confirmation, resolve_disambiguation, handle_daily_brief, interrogate_brain, handle_clarification, resolve_anaphora
from core.webhook.commands import handle_command, handle_undo_command
from core.webhook.multimodal import process_multimodal_content


# ── Chat authorization (plans/75 §7: webhook test-chat bypass) ─────────────
# Production truth: ONLY the owner chat (env TELEGRAM_CHAT_ID) is authorized.
# TEST_CHAT_IDS is a DEFAULT-OFF allow-list for the automated UAT suite — when
# unset/empty, behavior is byte-identical to the legacy single-chat gate
# (fail-closed: no test chat, no bypass). When set, each listed chat id is
# additionally accepted. Prod never sets it; only CI/nightly UAT runs do.
def _chat_authorized(chat_id) -> bool:
    owner = os.getenv("TELEGRAM_CHAT_ID")
    if owner and str(chat_id) == str(owner):
        return True
    allowlist = os.getenv("TEST_CHAT_IDS", "").strip()
    if not allowlist:
        return False
    allowed = {c.strip() for c in allowlist.split(",") if c.strip()}
    return str(chat_id) in allowed


async def handle_confident_note(text: str, chat_id: int, receipt: str = None, source: str = "telegram", sender: str = "user", entity: str = None, extraction_method: str = None, session_id: str = None, active_anchor: dict = None, exclude_signal_types: list = None) -> str | None:
    """NOTE handler: routes bare URLs and /note shortcuts to the processing pipeline.

    Used by bare URL /note path.
    Does NOT insert raw_dumps — callers own the audit trail.
    Does NOT do enrichment — enrichment is the planner's job.
    """
    try:
        from core.lib.url_filter import check_and_quarantine_url

        # URL quarantine at ingress: URL-bearing text is a resource, never a memory
        url_result = check_and_quarantine_url(text, source=source)
        if url_result.is_url:
            if url_result.action == "dismissed":
                await send_telegram(chat_id, "Already seen that link and dismissed it — skipping.")
                return receipt or "\u2705"
            final = url_result.message if url_result.action == "inserted" else (receipt or "\u2705")
            await send_telegram(chat_id, final)
            return final

        from core.pulse.tools import create_note_direct

        # Pass active_anchor context for org routing (N:/Note: shortcuts)
        organization_id = None
        if active_anchor:
            organization_id = active_anchor.get("last_org_id")

        await create_note_direct(content=text, source=source or "telegram", organization_id=organization_id)

        final = receipt or "\u2705"
        await send_telegram(chat_id, final)
        return final
    except Exception as e:
        audit_log_sync("webhook", "WARNING", f"handle_confident_note failed: {e}")
        # Honest failure — never echo a success receipt for a save that didn't
        # happen (the "✅ Captured." lie: failure logged, success shown).
        await send_telegram(chat_id, "⚠️ Couldn't save that note — it didn't reach your vault. Try again?")
        return None


# Pending graph clarification state — now DB-backed via pending_graph_clarifications table
# See core/lib/clarification_state.py for helpers

async def resolve_graph_person_context(chat_id: int, context_text: str, pending_id: int, label: str):
    ctx = context_text.strip() if context_text and context_text.strip() else None
    result = await process_graph_pending_decision(
        pending_id=pending_id, decision='approve', context=ctx
    )
    if result.get('success'):
        msg = f"'{label}' is approved."
        if ctx:
            msg += f" ({ctx})"
        inferred = result.get('inferred_edges', [])
        if inferred:
            msg += "\n🔗 " + "\n🔗 ".join(inferred)
        await send_telegram(chat_id, msg)
    else:
        await send_telegram(chat_id, fail(result.get('message', 'Error')))
    resolve_clarification(chat_id, 'node')

async def process_callback_query(callback_query: dict):
    trace_id_var.set(str(uuid.uuid4())[:12])
    callback_id = callback_query.get('id')
    data = callback_query.get('data', '')
    message = callback_query.get('message', {})
    chat_id = message.get('chat', {}).get('id')
    
    await answer_callback_query(callback_id)
    
    if not chat_id:
        return {"success": True}

    if not _chat_authorized(chat_id):
        audit_log_sync("webhook", "WARNING", f"Unauthorized callback from Chat ID: {chat_id}")
        return {"success": True}
        
    try:
        import re

        # Check for person context skip callback
        persontag_match = re.match(r'^persontag_skip_g(\d+)$', data)
        if persontag_match:
            pending_id = int(persontag_match.group(1))
            pending_item = maybe_single_safe(supabase.table('pending_nodes').select('label').eq('id', pending_id))
            label = pending_item.data.get('label', 'Unknown') if pending_item and pending_item.data else 'Unknown'
            await resolve_graph_person_context(chat_id, None, pending_id, label)
            return {"success": True}

        # Check for clarification cancel — reverts status to pending without approving/rejecting
        cancel_clar_match = re.match(r'^cancel_clarification_g(\d+)$', data)
        if cancel_clar_match:
            pending_id = int(cancel_clar_match.group(1))
            resolve_clarification(chat_id, 'node')
            supabase.table('pending_nodes').update({'status': 'pending'}).eq('id', pending_id).eq('status', 'awaiting_details').execute()
            await send_telegram(chat_id, "Cancelled — it stays pending.")
            return {"success": True}

        # Confirm auto-decisions callback: "confirm_auto_all"
        # Sets verified_at on auto-decisions and reinforces pattern confidence
        if data == "confirm_auto_all":
            try:
                now = datetime.now(timezone.utc)
                cutoff = (now - timedelta(minutes=30)).isoformat()

                # Find unverified auto-decisions from the last 30 minutes
                decision_res = supabase.table('decisions')\
                    .select('id, decision_type, source, metadata')\
                    .eq('auto_decided', True)\
                    .eq('status', 'active')\
                    .is_('verified_at', None)\
                    .gte('decided_at', cutoff)\
                    .execute()

                confirmed_count = 0
                trained_count = 0
                for row in (decision_res.data or []):
                    decision_id = row['id']

                    # Set verified_at on the decision
                    supabase.table('decisions').update({
                        'verified_at': now.isoformat(),
                    }).eq('id', decision_id).execute()

                    # Vision #4: confirming is a learning signal — per-item,
                    # against the decision's REAL subsystem and its EXACT
                    # decision-time features (ledger X3: the old single
                    # 'auto_decisions' observation was decorative).
                    from core.webhook.utils import emit_confirmed_observation
                    if await emit_confirmed_observation(row, source_tag='confirm_auto_all'):
                        trained_count += 1

                    confirmed_count += 1

                if confirmed_count > 0:
                    await send_telegram(
                        chat_id,
                        ack_verified(confirmed_count)
                    )
                    audit_log_sync("webhook", "INFO",
                        f"User confirmed {confirmed_count} auto-decisions "
                        f"({trained_count} emitted learning signals against their source subsystems)")
                else:
                    await send_telegram(
                        chat_id,
                        "Nothing unverified in the last 30 minutes."
                    )
            except Exception as confirm_err:
                audit_log_sync("webhook", "WARNING", f"Confirm auto-processed failed: {confirm_err}")
                await send_telegram(chat_id, "Couldn't verify auto-decisions — try again in a moment.")
            return {"success": True}

        # Undo auto-processed items callback: "undo_auto_channels", "undo_auto_graph", "undo_auto_edge"
        undo_match = re.match(r'^undo_auto_(channels|graph|edge)$', data)
        if undo_match:
            undo_target = undo_match.group(1)
            try:
                from core.decisions import reverse_decision
                # Find auto-decisions from the last 30 minutes
                now = datetime.now(timezone.utc)
                cutoff = (now - timedelta(minutes=30)).isoformat()
                
                # Map target to decision types and entity types
                if undo_target == "channels":
                    # Channel items use decision_type='channel_approval' (utils.py line 99)
                    decision_res = supabase.table('decisions').select('id, entity_id, decision_type, metadata')\
                        .eq('auto_decided', True)\
                        .eq('status', 'active')\
                        .is_('verified_at', None)\
                        .gte('decided_at', cutoff)\
                        .eq('decision_type', 'channel_approval')\
                        .execute()
                elif undo_target == "graph":
                    # Graph nodes use decision_type='graph_node_approval' (graph.py line 468)
                    decision_res = supabase.table('decisions').select('id, entity_id, decision_type, metadata')\
                        .eq('auto_decided', True)\
                        .eq('status', 'active')\
                        .is_('verified_at', None)\
                        .gte('decided_at', cutoff)\
                        .eq('decision_type', 'graph_node_approval')\
                        .execute()
                else:  # edges
                    # Graph edges use decision_type='graph_edge_approval' (graph.py line 596)
                    decision_res = supabase.table('decisions').select('id, entity_id, decision_type, metadata')\
                        .eq('auto_decided', True)\
                        .eq('status', 'active')\
                        .is_('verified_at', None)\
                        .gte('decided_at', cutoff)\
                        .eq('decision_type', 'graph_edge_approval')\
                        .execute()
                
                undone_count = 0
                for row in (decision_res.data or []):
                    decision_id = row['id']
                    entity_id = row.get('entity_id')
                    
                    # Reverse the decision record
                    reverse_decision(decision_id, rationale="User undid auto-approve via Telegram undo button")
                    # Vision #4: the undo is a learning signal — emit the
                    # inverse observation so the pattern that caused the wrong
                    # auto-approve demotes (see emit_undo_correction).
                    from core.webhook.utils import emit_undo_correction
                    await emit_undo_correction(row)
                    
                    # Attempt to undo the actual DB action
                    if undo_target == "channels" and entity_id and entity_id.isdigit():
                        try:
                            # Revert message back to pending for re-review
                            supabase.table('messages').update({'danny_decision': None}).eq('id', int(entity_id)).execute()
                            undone_count += 1
                        except Exception as e:
                            audit_log_sync("webhook", "ERROR", f"Undo channels failed: {e}")
                    elif undo_target == "graph" and entity_id:
                        try:
                            # Look up the pending_nodes row by id
                            # Auto-approved graph nodes have entity_id = node UUID
                            # Move them back to pending
                            node_check = supabase.table('pending_nodes').select('id')\
                                .eq('id', int(entity_id))\
                                .execute()
                            if node_check.data:
                                supabase.table('pending_nodes').update({'status': 'pending'})\
                                    .eq('id', int(entity_id))\
                                    .execute()
                                undone_count += 1
                            else:
                                # Try by label — find matching pending node
                                node_res = supabase.table('pending_nodes').select('id, status')\
                                    .eq('status', 'approved')\
                                    .execute()
                                for n in node_res.data or []:
                                    supabase.table('pending_nodes').update({'status': 'pending'})\
                                        .eq('id', n['id'])\
                                        .execute()
                                    undone_count += 1
                        except Exception as e:
                            audit_log_sync("webhook", "ERROR", f"Undo graph failed: {e}")
                    elif undo_target == "edge" and entity_id:
                        try:
                            supabase.table('pending_graph_edges').update({'status': 'pending'})\
                                .eq('id', int(entity_id))\
                                .execute()
                            undone_count += 1
                        except Exception as e:
                            audit_log_sync("webhook", "ERROR", f"Undo edge failed: {e}")
                
                if undone_count > 0:
                    await send_telegram(chat_id, ack_undone(undone_count, undo_target))
                else:
                    await send_telegram(chat_id, "Nothing to undo in the last 30 minutes — likely already verified or reversed.")
            except Exception as undo_err:
                audit_log_sync("webhook", "WARNING", f"Undo auto-processed failed: {undo_err}")
                await send_telegram(chat_id, "Couldn't undo those — try again in a moment.")
            return {"success": True}

        # Suggest mode pattern callback: "pattern_approve_{subsystem}_{hash}" or "pattern_skip_{subsystem}_{hash}"
        pattern_suggest_match = re.match(r'^pattern_(approve|skip)_(.+)$', data)
        if pattern_suggest_match:
            pattern_action = pattern_suggest_match.group(1)
            payload = pattern_suggest_match.group(2)
            # payload format: {subsystem}_{feature_hash}
            sep = payload.rfind('_')
            if sep <= 0:
                await send_telegram(chat_id, "That pattern didn't register — try again.")
                return {"success": True}
            subsystem = payload[:sep]
            feature_hash = payload[sep+1:]

            if pattern_action == 'approve':
                try:
                    # M3: core_config PK is now (owner_id, key) — the upsert
                    # conflict target must include owner_id (db/78).
                    supabase.table('core_config').upsert({
                        'key': f'suggest_approved:{subsystem}:{feature_hash}',
                        'content': datetime.now(timezone.utc).isoformat(),
                    }, on_conflict='owner_id,key').execute()

                    pattern_row = maybe_single_safe(
                        supabase.table('subsystem_patterns')
                        .select('id, soft_accepted_count')
                        .eq('subsystem', subsystem)
                        .eq('feature_hash', feature_hash)
                    )
                    current_count = pattern_row.data.get('soft_accepted_count', 0) or 0 if pattern_row.data else 0
                    supabase.table('subsystem_patterns').update({
                        'soft_accepted_count': current_count + 1,
                    }).eq('subsystem', subsystem).eq('feature_hash', feature_hash).execute()

                    await send_telegram(chat_id, f"{subsystem} will auto-approve from now on.")
                    audit_log_sync("decision_pulse", "INFO", f"Suggest mode approve: {subsystem}:{feature_hash} pattern promoted to auto-approve")
                except Exception as e:
                    await send_telegram(chat_id, "Couldn't approve that pattern — try again.")
                    audit_log_sync("decision_pulse", "WARNING", f"Suggest mode approve failed: {e}")
            else:
                await send_telegram(chat_id, "Skipped that pattern — you can pick it up again later.")

            return {"success": True}

        # Merge proposal callback: "merge_accept_123" or "merge_reject_123"
        merge_match = re.match(r'^merge_(accept|reject)_(\d+)$', data)
        if merge_match:
            merge_action = merge_match.group(1)
            pending_id = int(merge_match.group(2))
            pending_row = maybe_single_safe(supabase.table('pending_nodes').select('*').eq('id', pending_id))
            if not pending_row or not pending_row.data:
                await send_telegram(chat_id, "That merge proposal's gone.")
                return {"success": True}
            pr = pending_row.data
            if pr.get('status') != 'merge_proposed':
                await send_telegram(chat_id, "Already handled that one.")
                return {"success": True}
            if merge_action == 'reject':
                supabase.table('pending_nodes').update({'status': 'rejected'}).eq('id', pending_id).execute()
                await send_telegram(chat_id, ack_rejected(pr['label']))
                return {"success": True}
            target_id = pr.get('merge_candidate_id')
            if not target_id:
                await send_telegram(chat_id, "Couldn't find the merge target in that proposal.")
                return {"success": True}
            from core.lib.graph_rules import get_canonical_id
            target_canonical = get_canonical_id(target_id)
            source_node_res = maybe_single_safe(supabase.table('graph_nodes').select('id').eq('label', pr['label']).eq('is_current', True))
            source_node_id = source_node_res.data['id'] if source_node_res and source_node_res.data else None
            if source_node_id:
                supabase.table('graph_nodes').update({'canonical_id': target_canonical}).eq('id', source_node_id).execute()
                supabase.table('graph_edges').update({'source_node_id': target_canonical}).eq('source_node_id', source_node_id).execute()
                supabase.table('graph_edges').update({'target_node_id': target_canonical}).eq('target_node_id', source_node_id).execute()
            supabase.table('pending_nodes').update({'status': 'approved'}).eq('id', pending_id).execute()
            await send_telegram(chat_id, ack_merged(pr['label'], target_canonical[:8]))
            return {"success": True}

        # Batch approve/reject all items of a type
        batch_match = re.match(r'^(approve|reject)_all_(emails|calls|whatsapp|teams|nodes|edges)$', data)
        if batch_match:
            action = batch_match.group(1)
            target = batch_match.group(2)
            is_approve = (action == 'approve')
            results = {"success": 0, "failure": 0}

            if target in ('emails', 'calls', 'whatsapp', 'teams'):
                channel_map = {'emails': 'email', 'calls': 'call', 'whatsapp': 'whatsapp', 'teams': 'teams'}
                channel = channel_map[target]
                items_res = supabase.table('messages').select('id').eq('channel', channel).is_('danny_decision', 'null').eq('direction', 'incoming').eq('classification', 'actionable').execute()
                for item in (items_res.data or []):
                    if target == 'emails':
                        result = await process_email_pending_decision(item['id'], 'approve' if is_approve else 'reject')
                    else:
                        result = await process_channel_pending_decision(channel, item['id'], 'approve' if is_approve else 'reject')
                    if result.get('success'):
                        results["success"] += 1
                    else:
                        results["failure"] += 1
            elif target == 'nodes':
                items_res = supabase.table('pending_nodes').select('id').eq('status', 'pending').execute()
                for item in (items_res.data or []):
                    result = await process_graph_pending_decision(item['id'], 'approve' if is_approve else 'reject')
                    if result.get('success'):
                        results["success"] += 1
                    else:
                        results["failure"] += 1
            elif target == 'edges':
                items_res = supabase.table('pending_graph_edges').select('id').eq('status', 'pending').execute()
                for item in (items_res.data or []):
                    result = await process_pending_edge_decision(item['id'], 'approve' if is_approve else 'reject')
                    if result.get('success'):
                        results["success"] += 1
                    else:
                        results["failure"] += 1

            verb = "Approved" if is_approve else "Rejected"
            msg = f"{verb} {results['success']} {target}"
            if results['failure']:
                msg += f", {results['failure']} failed"
            await send_telegram(chat_id, f"{ok(msg)}.")
            return {"success": True}

        # Example data: "approve_e123" or "reject_w45" or "edit_pe12"
        match = re.match(r'^(approve|reject|edit)_([ecwgpECWGP]+)?(\d+)$', data)
        if match:
            action, prefix, shortcode = match.groups()
            is_approve = (action == 'approve')
            sc_int = int(shortcode)
            
            prefix = (prefix or "").lower()
            if prefix == 'e':
                result = await process_email_pending_decision(sc_int, 'approve' if is_approve else 'reject')
            elif prefix == 'c':
                result = await process_channel_pending_decision('call', sc_int, 'approve' if is_approve else 'reject')
            elif prefix == 'w':
                result = await process_channel_pending_decision('whatsapp', sc_int, 'approve' if is_approve else 'reject')
            elif prefix == 'pe':
                if action == 'edit':
                    pe_res = maybe_single_safe(supabase.table('pending_graph_edges').select('source_label, relationship, target_label').eq('id', sc_int))
                    if not getattr(pe_res, 'data', None):
                        await send_telegram(chat_id, "That edge's gone or already handled.")
                        return {"success": True}
                        
                    pe = pe_res.data
                    set_clarification(
                        chat_id, sc_int,
                        pending_type='edge', step='awaiting_edge_edit',
                        label='', expires_minutes=15
                    )
                    from core.services.user_settings import resolve_user_name
                    _uname = resolve_user_name()
                    await send_telegram(chat_id, f"Editing edge: {pe['source_label']} → {pe['relationship']} → {pe['target_label']}\nReply with the corrected edge, e.g. `pe{sc_int} {_uname} KNOWS Alice` or `pe{sc_int} KNOWS`")
                    return {"success": True}
                else:
                    result = await process_pending_edge_decision(sc_int, 'approve' if is_approve else 'reject')
            elif prefix == 'g':
                if not is_approve:
                    resolve_clarification(chat_id, 'node')
                    result = await process_graph_pending_decision(sc_int, 'reject')
                else:
                    pending_item = maybe_single_safe(supabase.table('pending_nodes').select('id, label, type:node_type').eq('id', sc_int))
                    if pending_item and pending_item.data:
                        ptype = pending_item.data.get('type')
                        label = pending_item.data.get('label')
                        if ptype == 'person':
                            supabase.table('pending_nodes').update({'status': 'awaiting_details'}).eq('id', sc_int).execute()
                            keyboard = [
                                [{"text": "⏭️ Skip", "callback_data": f"persontag_skip_g{sc_int}"}],
                                [{"text": "❌ Cancel", "callback_data": f"cancel_clarification_g{sc_int}"}]
                            ]
                            await send_telegram(
                                chat_id,
                                f"Any context for '{label}'? (role, relationship, organization)",
                                inline_keyboard=keyboard
                            )
                            return {"success": True}
                        else:
                            result = await process_graph_pending_decision(sc_int, 'approve')
                    else:
                        result = await process_graph_pending_decision(sc_int, 'approve')
            else:
                # Unprefixed, try email then call then whatsapp
                result = await process_email_pending_decision(sc_int, 'approve' if is_approve else 'reject')
                if result.get('action') == 'not_found':
                    result = await process_channel_pending_decision('call', sc_int, 'approve' if is_approve else 'reject')
                    if result.get('action') == 'not_found':
                        result = await process_channel_pending_decision('whatsapp', sc_int, 'approve' if is_approve else 'reject')
            
            if result and result.get('success'):
                await send_telegram(chat_id, ok(result.get('message', 'Done')))
            elif result:
                if result.get('action') != 'not_found':
                    await send_telegram(chat_id, fail(result.get('message', 'Error')))
                else:
                    await send_telegram(chat_id, f"No pending item matches [{shortcode}].")
            return {"success": True}
            
        # If it didn't match the approve/reject regex, it's a state machine reply (e.g. "t", "n", "u", "1")
        return {"fallback_text": data}
        
    except Exception as e:
        audit_log_sync("webhook", "ERROR", f"Callback query processing failed: {e}")
        await send_telegram(chat_id, "That tap didn't go through — try again?")
        
    return {"success": True}

async def process_webhook(update: dict):
    """Process an incoming Telegram (or app-simulated) update.
    (M3: wrapped in the channel tenant scope — Telegram traffic carries no
    API key, so the tenant resolves via resolve_channel_tenant().)
    """
    from core.webhook.utils import webhook_tenant_scope
    with webhook_tenant_scope():
        return await _process_webhook(update)


async def _process_webhook(update: dict):
    """Inner implementation (M3: tenant scope applied by the public wrapper)."""
    # Generate correlation IDs for this request
    req_trace_id = str(uuid.uuid4())[:12]
    trace_id_var.set(req_trace_id)
    set_decision_chain_id()
    start_timer(req_trace_id)
    
    try:
        from core.services.db import tenant_aware_client
        supabase = tenant_aware_client()
        update_id = update.get('update_id')
        if update_id and isinstance(update_id, (int, float)):
            try:
                supabase.table('processed_updates').insert({"update_id": int(update_id)}).execute()
                try:
                    cutoff = (datetime.now(timezone.utc) - timedelta(hours=72)).isoformat()
                    supabase.table('processed_updates').delete().lt('processed_at', cutoff).execute()
                except Exception as cleanup_e:
                    audit_log_sync("webhook", "WARNING", f"Dedup cleanup failed (non-critical): {cleanup_e}")
            except Exception as e:
                error_msg = str(e)
                if "23505" in error_msg or "already exists" in error_msg.lower() or "duplicate key" in error_msg.lower():
                    audit_log_sync("webhook", "INFO", f"Telegram retry detected for update {update_id}. Skipping.")
                    return {"success": True, "message": "Already processed"}
                else:
                    audit_log_sync("webhook", "WARNING", f"Deduplication check error: {error_msg}")
                    # Fail open if it's a random DB timeout so we don't drop the message
                    pass

        ist_offset = IST_TIMEZONE
        now = datetime.now(ist_offset)

        intent_signal = update.get('intent')
        auth_secret = update.get('auth_secret')

        if intent_signal == 'JOURNAL_SYNC':
            if auth_secret != os.getenv("PULSE_SECRET"):
                audit_log_sync("webhook", "WARNING", "Unauthorized Journal Sync attempt.")
                return {"status": "unauthorized", "message": "Invalid Secret"}
            audit_log_sync("webhook", "INFO", "JOURNAL_SYNC signal received from Google Sheets.")
            triggered = await trigger_github_pulse()
            if triggered:
                owner_id = os.getenv("TELEGRAM_CHAT_ID")
                if owner_id:
                    await send_telegram(owner_id, "Got the journal — syncing it into your archive now.")
                return {"success": True, "message": "Sync pipeline triggered"}
            else:
                return {"success": False, "message": "GitHub trigger failed"}

        message = update.get('message', {})
        text = message.get('text', '')
        chat_id = message.get('chat', {}).get('id')
        
        if 'callback_query' in update:
            cb_result = await process_callback_query(update['callback_query'])
            if cb_result.get("fallback_text"):
                text = cb_result["fallback_text"]
                message = update['callback_query'].get('message', {})
                chat_id = message.get('chat', {}).get('id')
            else:
                return cb_result

        if not text and not message.get('photo') and not message.get('voice') and not message.get('audio') and not message.get('document'):
            return {"message": "No message"}

        try:
            _NOISE_KEYS = {'latest_briefing', 'briefing_history', 'last_pulse_summary'}
            core_res = supabase.table('core_config').select('key, content').execute()
            filtered = [r for r in (core_res.data or []) if r.get('key') not in _NOISE_KEYS]
            core_json = json.dumps(filtered)
        except Exception as e:
            audit_log_sync("webhook", "WARNING", f"core_config fetch failed: {e}")
            core_json = "[]"

        mark(req_trace_id, "core_config")

        if not chat_id:
            return {"success": True}

        if not _chat_authorized(chat_id):
            audit_log_sync("webhook", "WARNING", f"Unauthorized access from Chat ID: {chat_id}")
            return {"message": "Unauthorized"}

        if not text:
            photo = message.get('photo')
            voice = message.get('voice')
            audio = message.get('audio')
            document = message.get('document')

            if photo:
                file_id = photo[-1].get('file_id')
                await send_telegram(chat_id, "Looking at that image...")
                file_bytes, mime = await download_telegram_file(file_id)
                await process_multimodal_content(file_bytes, mime, chat_id, ist_hour=now.hour, core_json=core_json)
                return {"success": True}

            elif voice or audio:
                file_id = voice.get('file_id') or audio.get('file_id')
                await send_telegram(chat_id, "Listening to that audio...")
                file_bytes, mime = await download_telegram_file(file_id)
                await process_multimodal_content(file_bytes, mime, chat_id, ist_hour=now.hour, core_json=core_json)
                return {"success": True}

            elif document:
                file_id = document.get('file_id')
                mime = document.get('mime_type', '')

                if mime in ['application/pdf', 'application/vnd.openxmlformats-officedocument.wordprocessingml.document', 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', 'application/vnd.openxmlformats-officedocument.presentationml.presentation'] or mime.startswith('text/'):
                    await send_telegram(chat_id, "Reading that document...")
                    file_bytes, mime = await download_telegram_file(file_id)
                    await process_multimodal_content(file_bytes, mime, chat_id, ist_hour=now.hour, core_json=core_json)
                    return {"success": True}
                else:
                    await send_telegram(chat_id, "That file type won't work — PDF, DOCX, or text only.")
                    return {"success": True}

            await send_telegram(chat_id, "I can handle text, images, audio, and documents.")
            return {"success": True}

        MAX_TEXT_LENGTH = 10000
        if len(text) > MAX_TEXT_LENGTH:
            await send_telegram(chat_id, f"That's a long one ({len(text)} chars) — keep it under {MAX_TEXT_LENGTH}.")
            return {"success": True}

        _email_approve_match = re.match(r'^[eE](\d+)\s+(yes|approve|do it|yep|add it)$', text.strip(), re.IGNORECASE)
        _email_reject_match = re.match(r'^[eE](\d+)\s+(drop|no|reject|skip|dismiss)(?:[,\s]+(.+))?$', text.strip(), re.IGNORECASE)
        _call_approve_match = re.match(r'^[cC](\d+)\s+(yes|approve|do it|yep|add it)$', text.strip(), re.IGNORECASE)
        _call_reject_match = re.match(r'^[cC](\d+)\s+(drop|no|reject|skip|dismiss)(?:[,\s]+(.+))?$', text.strip(), re.IGNORECASE)
        _whatsapp_approve_match = re.match(r'^[wW](\d+)\s+(yes|approve|do it|yep|add it)$', text.strip(), re.IGNORECASE)
        _whatsapp_reject_match = re.match(r'^[wW](\d+)\s+(drop|no|reject|skip|dismiss)(?:[,\s]+(.+))?$', text.strip(), re.IGNORECASE)
        _teams_approve_match = re.match(r'^[tT](\d+)\s+(yes|approve|do it|yep|add it)$', text.strip(), re.IGNORECASE)
        _teams_reject_match = re.match(r'^[tT](\d+)\s+(drop|no|reject|skip|dismiss)(?:[,\s]+(.+))?$', text.strip(), re.IGNORECASE)
        _graph_approve_match = re.match(r'^[gG](\d+)\s+(yes|approve|do it|yep|add it)$', text.strip(), re.IGNORECASE)
        _graph_reject_match = re.match(r'^[gG](\d+)\s+(drop|no|reject|skip|dismiss)$', text.strip(), re.IGNORECASE)
        _graph_direct_match = re.match(r'^[gG](\d+)\s+(?!(?:yes|approve|do it|yep|add it|drop|no|reject|skip|dismiss|cancel)\b)(.+)$', text.strip(), re.IGNORECASE | re.DOTALL)
        _pe_approve_match = re.match(r'^pe(\d+)\s+(yes|approve|do it|yep|add it)$', text.strip(), re.IGNORECASE)
        _pe_reject_match = re.match(r'^pe(\d+)\s+(drop|no|reject|skip|dismiss)$', text.strip(), re.IGNORECASE)
        _pe_edit_match = re.match(r'^pe(\d+)\s+(?!(?:yes|approve|do it|yep|add it|drop|no|reject|skip|dismiss|cancel)\b)(.+)$', text.strip(), re.IGNORECASE | re.DOTALL)
        
        _approve_match = re.match(r'^(\d+)\s+(yes|approve|do it|yep|add it)$', text.strip(), re.IGNORECASE)
        _reject_match = re.match(r'^(\d+)\s+(drop|no|reject|skip|dismiss)$', text.strip(), re.IGNORECASE)

        # ---------------------------------------------------------
        # SESSION CONFIRMATION GUARD (NLP Graph Corrections)
        # ---------------------------------------------------------
        session = get_active_session(chat_id)
        if session:
            # Did they say yes to the proposal?
            if text.strip().lower() in ('yes', 'confirm', 'looks good', 'do it', 'approve', 'y'):
                await send_telegram(chat_id, "Applying those corrections...")
                results = await apply_graph_actions(session['actions'], session['original_items_map'])
                clear_session(chat_id)
                summary_text = f"Applied: {results['applied']} | Failed: {results['failed']}\n" + "\n".join(results['details'])
                await send_telegram(chat_id, summary_text)
                return {"success": True}
            
            # Did they cancel the session?
            if text.strip().lower() in ('no', 'cancel', 'stop', 'drop', 'n'):
                clear_session(chat_id)
                await send_telegram(chat_id, "Cancelled — those items stay pending.")
                return {"success": True}
            
            # If they sent something else, assume it's a modification to the proposal.
            # It will fall through to the NLP check below.

        # ---------------------------------------------------------
        # PENDING GRAPH CLARIFICATION CHECK (context text replies)
        # ---------------------------------------------------------
        clar = get_active_clarification(chat_id, 'node') or get_active_clarification(chat_id, 'edge')
        if clar:
            step = clar.get('step')
            pending_type = clar.get('pending_type', 'node')
            if text.strip().lower() in ('cancel',):
                supabase.table('pending_nodes').update({'status': 'pending'}).eq('id', clar['pending_id']).eq('status', 'awaiting_details').execute()
                resolve_clarification(chat_id, pending_type)
                await send_telegram(chat_id, "Cancelled — it stays pending.")
                return {"success": True}
            if step == 'awaiting_person_context':
                if text.strip().lower() in ('skip', 'no', 'none', 'n/a'):
                    await resolve_graph_person_context(chat_id, None, clar['pending_id'], clar['label'])
                else:
                    await resolve_graph_person_context(chat_id, text, clar['pending_id'], clar['label'])
                return {"success": True}
            elif step == 'awaiting_edge_edit':
                _sc = clar['pending_id']
                _value = text.strip()
                parts = _value.split()
                if len(parts) == 1:
                    new_rel = parts[0]
                    new_source, new_target = None, None
                elif len(parts) >= 3:
                    rel_idx = -1
                    for i, p in enumerate(parts):
                        if p.isupper() and len(p) > 1:
                            rel_idx = i
                            break
                    if rel_idx > 0 and rel_idx < len(parts) - 1:
                        new_source = " ".join(parts[:rel_idx])
                        new_rel = parts[rel_idx]
                        new_target = " ".join(parts[rel_idx+1:])
                    else:
                        new_rel = parts[1]
                        new_source = parts[0]
                        new_target = " ".join(parts[2:])
                else:
                    new_source, new_rel, new_target = parts[0], parts[1] if len(parts) > 1 else None, None
                    
                result = await process_pending_edge_decision(
                    pending_id=_sc, decision='approve',
                    new_source=new_source, new_target=new_target, new_rel=new_rel
                )
                if result.get('success'):
                    await send_telegram(chat_id, ok(result['message']))
                else:
                    await send_telegram(chat_id, fail(result.get('message', 'Error')))
                resolve_clarification(chat_id, 'edge')
                return {"success": True}

        # DB recovery: if in-memory state was lost (restart/cold start), check awaiting_details items directly
        text_clean = text.strip().lower()
        awaiting_people = supabase.table('pending_nodes').select('id, label').eq('status', 'awaiting_details').eq('node_type', 'person').limit(2).execute().data or []
        if len(awaiting_people) == 1 and text_clean not in ('yes', 'no', 'approve', 'reject', 'drop', 'skip'):
            item = awaiting_people[0]
            set_clarification(
                chat_id, item['id'],
                pending_type='node', step='awaiting_person_context',
                label=item['label'], expires_minutes=5
            )
            if text_clean in ('skip', 'no', 'none', 'n/a'):
                await resolve_graph_person_context(chat_id, None, item['id'], item['label'])
            else:
                await resolve_graph_person_context(chat_id, text_clean, item['id'], item['label'])
            return {"success": True}

        # ---------------------------------------------------------
        # QUICK DECISION ROUTES (Binary Approve/Reject)
        # ---------------------------------------------------------

        # g-prefix: direct to pending_nodes
        if _graph_approve_match:
            try:
                _sc = _graph_approve_match.group(1)
                pending_item = maybe_single_safe(supabase.table('pending_nodes').select('id, label, type:node_type').eq('id', int(_sc)))
                if pending_item and pending_item.data:
                    ptype = pending_item.data.get('type')
                    label = pending_item.data.get('label')
                    if ptype == 'person':
                        supabase.table('pending_nodes').update({'status': 'awaiting_details'}).eq('id', int(_sc)).execute()
                        set_clarification(
                            chat_id, int(_sc),
                            pending_type='node', step='awaiting_person_context',
                            label=label, expires_minutes=5
                        )
                        await send_telegram(chat_id, f"Any context for '{label}'? (role, relationship) Reply 'skip' to approve without context.")
                        clear_session(chat_id)
                        return {"success": True}
                    else:
                        result = await process_graph_pending_decision(pending_id=int(_sc), decision='approve')
                else:
                    result = await process_graph_pending_decision(pending_id=int(_sc), decision='approve')
                
                if result and result.get('success'):
                    msg = ok(result.get('message', 'Done'))
                    inferred = result.get('inferred_edges', [])
                    if inferred:
                        msg += "\n🔗 " + "\n🔗 ".join(inferred)
                    await send_telegram(chat_id, msg)
                elif result:
                    await send_telegram(chat_id, fail(result.get('message', 'Error')))
                
                clear_session(chat_id)
                return {"success": True}
            except Exception as _sc_err:
                audit_log_sync("webhook", "WARNING", f"Graph prefix shortcode error: {_sc_err}")
                await send_telegram(chat_id, "That didn't go through — mind trying again?")
                return {"success": True}

        if _graph_reject_match:
            try:
                _sc = _graph_reject_match.group(1)
                result = await process_graph_pending_decision(pending_id=int(_sc), decision='reject')
                if result.get('success'):
                    await send_telegram(chat_id, ok(result['message']))
                else:
                    await send_telegram(chat_id, fail(result['message']))
                clear_session(chat_id)
                return {"success": True}
            except Exception as _sc_err:
                audit_log_sync("webhook", "WARNING", f"Graph prefix shortcode error: {_sc_err}")
                await send_telegram(chat_id, "That didn't go through — mind trying again?")
                return {"success": True}

        if _graph_direct_match:
            try:
                _sc = int(_graph_direct_match.group(1))
                _value = _graph_direct_match.group(2)
                pending_item = maybe_single_safe(supabase.table('pending_nodes').select('id, label, type:node_type').eq('id', _sc))
                if not pending_item or not pending_item.data:
                    await send_telegram(chat_id, "Couldn't find that pending item.")
                    clear_session(chat_id)
                    return {"success": True}
                ptype = pending_item.data.get('type')
                label = pending_item.data.get('label')
                if ptype == 'person':
                    result = await process_graph_pending_decision(
                        pending_id=_sc, decision='approve', context=_value.strip()
                    )
                else:
                    result = await process_graph_pending_decision(pending_id=_sc, decision='approve')
                
                if result.get('success'):
                    msg = ok(result['message'])
                    inferred = result.get('inferred_edges', [])
                    if inferred:
                        msg += "\n🔗 " + "\n🔗 ".join(inferred)
                    await send_telegram(chat_id, msg)
                else:
                    await send_telegram(chat_id, fail(result.get('message', 'Error')))
                    
                clear_session(chat_id)
                return {"success": True}
            except Exception as _sc_err:
                audit_log_sync("webhook", "WARNING", f"Graph direct shortcode error: {_sc_err}")
                await send_telegram(chat_id, "That didn't go through — mind trying again?")
                return {"success": True}

        # pe-prefix: direct to pending_graph_edges
        if _pe_approve_match or _pe_reject_match:
            try:
                _sc = (_pe_approve_match or _pe_reject_match).group(1)
                _is_approve = bool(_pe_approve_match)
                result = await process_pending_edge_decision(
                    pending_id=int(_sc),
                    decision='approve' if _is_approve else 'reject'
                )
                if result.get('success'):
                    await send_telegram(chat_id, ok(result['message']))
                else:
                    await send_telegram(chat_id, fail(result.get('message', 'Error')))
                return {"success": True}
            except Exception as _sc_err:
                audit_log_sync("webhook", "WARNING", f"Pending edge shortcode error: {_sc_err}")
                await send_telegram(chat_id, "That didn't go through — mind trying again?")
                return {"success": True}
                
        if _pe_edit_match:
            try:
                _sc = int(_pe_edit_match.group(1))
                _value = _pe_edit_match.group(2).strip()
                
                # Try to parse the edit value.
                # Format: "{user} KNOWS Alice" or just "KNOWS"
                parts = _value.split()
                if len(parts) == 1:
                    new_rel = parts[0]
                    new_source, new_target = None, None
                elif len(parts) >= 3:
                    # Find relationship (all caps word)
                    rel_idx = -1
                    for i, p in enumerate(parts):
                        if p.isupper() and len(p) > 1:
                            rel_idx = i
                            break
                            
                    if rel_idx > 0 and rel_idx < len(parts) - 1:
                        new_source = " ".join(parts[:rel_idx])
                        new_rel = parts[rel_idx]
                        new_target = " ".join(parts[rel_idx+1:])
                    else:
                        new_rel = parts[1]
                        new_source = parts[0]
                        new_target = " ".join(parts[2:])
                else:
                    new_source, new_rel, new_target = parts[0], parts[1], None
                    
                result = await process_pending_edge_decision(
                    pending_id=_sc, decision='approve',
                    new_source=new_source, new_target=new_target, new_rel=new_rel
                )
                if result.get('success'):
                    await send_telegram(chat_id, ok(result['message']))
                else:
                    await send_telegram(chat_id, fail(result.get('message', 'Error')))
                return {"success": True}
            except Exception as _sc_err:
                audit_log_sync("webhook", "WARNING", f"Pending edge edit error: {_sc_err}")
                await send_telegram(chat_id, "That didn't go through — mind trying again?")
                return {"success": True}

        # e-prefix: direct to messages(email)
        if _email_approve_match or _email_reject_match:
            try:
                _sc = (_email_approve_match or _email_reject_match).group(1)
                _is_approve = bool(_email_approve_match)
                result = await process_email_pending_decision(
                    pending_id=int(_sc),
                    decision='approve' if _is_approve else 'reject'
                )
                if result['success']:
                    await send_telegram(chat_id, ok(result['message']))
                else:
                    await send_telegram(chat_id, fail(result['message']))
                    if result['action'] in ('staging_failed',):
                        raise Exception(result['message'])
                return {"success": True}
            except Exception as _sc_err:
                audit_log_sync("webhook", "WARNING", f"Email prefix shortcode error: {_sc_err}")
                await send_telegram(chat_id, "That didn't go through — mind trying again?")
                return {"success": True}

        # c-prefix: direct to messages(call)
        if _call_approve_match or _call_reject_match:
            try:
                _sc = (_call_approve_match or _call_reject_match).group(1)
                _is_approve = bool(_call_approve_match)
                _ctx = (_call_reject_match.group(3) if _call_reject_match and not _is_approve else None)
                result = await process_channel_pending_decision('call', 
                    pending_id=int(_sc),
                    decision='approve' if _is_approve else 'reject',
                    rejection_context=_ctx
                )
                if result['success']:
                    await send_telegram(chat_id, ok(result['message']))
                else:
                    await send_telegram(chat_id, fail(result['message']))
                    if result['action'] in ('staging_failed',):
                        raise Exception(result['message'])
                return {"success": True}
            except Exception as _sc_err:
                audit_log_sync("webhook", "WARNING", f"Call prefix shortcode error: {_sc_err}")
                await send_telegram(chat_id, "That didn't go through — mind trying again?")
                return {"success": True}

        # w-prefix: direct to messages(whatsapp)
        if _whatsapp_approve_match or _whatsapp_reject_match:
            try:
                _sc = (_whatsapp_approve_match or _whatsapp_reject_match).group(1)
                _is_approve = bool(_whatsapp_approve_match)
                _ctx = (_whatsapp_reject_match.group(3) if _whatsapp_reject_match and not _is_approve else None)
                result = await process_channel_pending_decision('whatsapp', 
                    pending_id=int(_sc),
                    decision='approve' if _is_approve else 'reject',
                    rejection_context=_ctx
                )
                if result['success']:
                    await send_telegram(chat_id, ok(result['message']))
                else:
                    await send_telegram(chat_id, fail(result['message']))
                    if result['action'] in ('staging_failed',):
                        raise Exception(result['message'])
                return {"success": True}
            except Exception as _sc_err:
                audit_log_sync("webhook", "WARNING", f"WhatsApp prefix shortcode error: {_sc_err}")
                await send_telegram(chat_id, "That didn't go through — mind trying again?")
                return {"success": True}

        # t-prefix: direct to messages(teams)
        if _teams_approve_match or _teams_reject_match:
            try:
                _sc = (_teams_approve_match or _teams_reject_match).group(1)
                _is_approve = bool(_teams_approve_match)
                _ctx = (_teams_reject_match.group(3) if _teams_reject_match and not _is_approve else None)
                result = await process_channel_pending_decision('teams', 
                    pending_id=int(_sc),
                    decision='approve' if _is_approve else 'reject',
                    rejection_context=_ctx
                )
                if result['success']:
                    await send_telegram(chat_id, ok(result['message']))
                else:
                    await send_telegram(chat_id, fail(result['message']))
                    if result['action'] in ('staging_failed',):
                        raise Exception(result['message'])
                return {"success": True}
            except Exception as _sc_err:
                audit_log_sync("webhook", "WARNING", f"Teams prefix shortcode error: {_sc_err}")
                await send_telegram(chat_id, "That didn't go through — mind trying again?")
                return {"success": True}

        # ---------------------------------------------------------
        # NLP GRAPH CORRECTIONS ROUTE (Catch-all for g{id} free-text)
        # ---------------------------------------------------------
        if re.search(r'[gG]\d+', text):
            try:
                # Fetch pending items
                pending_res = supabase.table('pending_nodes').select('id, label, type:node_type, source_text').eq('status', 'pending').execute()
                pending_items = pending_res.data or []
                
                if pending_items:
                    await send_telegram(chat_id, "Working through those corrections...")
                    
                    # Call Gemini
                    actions = await interpret_graph_corrections(text, pending_items)
                    
                    if not actions:
                        await send_telegram(chat_id, "Couldn't parse structured actions from that — try again?")
                        return {"success": True}
                    
                    # Store in session cache
                    original_map = {item['id']: item for item in pending_items}
                    set_session_state(chat_id, actions, original_map, expires_minutes=5)
                    
                    # Format proposed actions for confirmation
                    proposal_lines = ["*Here is what I understood:*"]
                    for action in actions:
                        node_id = action.get('id')
                        orig = original_map.get(node_id)
                        if not orig:
                            continue
                            
                        act = action.get('action', '').upper()
                        if act == 'APPROVE':
                            new_label = action.get('corrected_label')
                            if not new_label or not new_label.strip():
                                new_label = orig['label']
                                
                            new_type = action.get('corrected_type')
                            if not new_type or not new_type.strip():
                                new_type = orig['type']
                                
                            proposal_lines.append(f"• g{node_id} ({orig['label']}) → {act} as \"{new_label}\" ({new_type})")
                        elif act == 'REJECT':
                            reason = action.get('reason', 'no reason provided')
                            proposal_lines.append(f"• g{node_id} ({orig['label']}) → {act} ({reason})")
                            
                    proposal_lines.append("\nReply **yes** to confirm, or send modifications.")
                    
                    full_message = "\n".join(proposal_lines)
                    # Protect against Telegram message length limits
                    if len(full_message) > 4000:
                        full_message = full_message[:3900] + "\n... [truncated due to length] ...\nReply **yes** to confirm."
                        
                    await send_telegram(chat_id, full_message)
                    return {"success": True}
                    
            except Exception as e:
                audit_log_sync("webhook", "ERROR", f"Graph NLP route error: {e}")
                await send_telegram(chat_id, "Couldn't process those graph corrections.")
                return {"success": True}

        # Unprefixed: backward-compatible — email first, then calls, then practice dismissal
        if _approve_match or _reject_match:
            try:
                _shortcode = (_approve_match or _reject_match).group(1)
                _is_approve = bool(_approve_match)

                result = await process_email_pending_decision(
                    pending_id=int(_shortcode),
                    decision='approve' if _is_approve else 'reject'
                )

                if result['success']:
                    await send_telegram(chat_id, ok(result['message']))
                    return {"success": True}

                if result['action'] == 'not_found':
                    call_result = await process_channel_pending_decision('call', 
                        pending_id=int(_shortcode),
                        decision='approve' if _is_approve else 'reject'
                    )
                    if call_result['success']:
                        await send_telegram(chat_id, ok(call_result['message']))
                        return {"success": True}
                    if call_result['action'] != 'not_found':
                        await send_telegram(chat_id, fail(call_result['message']))
                        if call_result['action'] in ('staging_failed',):
                            raise Exception(call_result['message'])
                        return {"success": True}

                    whatsapp_result = await process_channel_pending_decision('whatsapp', 
                        pending_id=int(_shortcode),
                        decision='approve' if _is_approve else 'reject'
                    )
                    if whatsapp_result['success']:
                        await send_telegram(chat_id, ok(whatsapp_result['message']))
                        return {"success": True}
                    if whatsapp_result['action'] != 'not_found':
                        await send_telegram(chat_id, fail(whatsapp_result['message']))
                        if whatsapp_result['action'] in ('staging_failed',):
                            raise Exception(whatsapp_result['message'])
                        return {"success": True}

                    # Not found in email, call, or WhatsApp — try practice dismissal (reject only)
                    if not _is_approve:
                        try:
                            _node_res = maybe_single_safe(
                                supabase.table('graph_nodes')
                                .select('id, label, metadata')
                                .eq('type', 'practice')
                                .eq('metadata->>shortcode', str(_shortcode))
                                .eq('is_current', True)
                            )
                            if _node_res.data:
                                _n = _node_res.data
                                _rm = _n.get('metadata') or {}
                                if isinstance(_rm, str):
                                    _rm = json.loads(_rm)
                                _rm['status'] = 'dismissed'
                                _rm['dismissed_at'] = now_ist().strftime('%Y-%m-%d')
                                supabase.table('graph_nodes').update({'metadata': _rm}).eq('id', _n['id']).execute()
                                _variants = _rm.get('variants', [_n.get('label', '')])
                                _excl = maybe_single_safe(supabase.table('core_config').select('content').eq('key', 'dismissed_practice_variants'))
                                _existing = json.loads(_excl.data.get('content') or '[]') if _excl.data else []
                                _existing_lower = set(v.lower() for v in _existing)
                                _new_entries = [v for v in _variants if v.lower() not in _existing_lower]
                                if _new_entries:
                                    supabase.table('core_config').update({'content': json.dumps(_existing + _new_entries)}).eq('key', 'dismissed_practice_variants').execute()
                                await send_telegram(chat_id, f"Dismissed: {_n.get('label', '')}")
                                audit_log_sync("webhook", "INFO", f"SHORTCODE DROP: Dismissed practice '{_n.get('label', '')}' via shortcode.")
                                return {"success": True}
                        except Exception as _sc_practice_err:
                            audit_log_sync("webhook", "WARNING", f"Shortcode practice fallback error: {_sc_practice_err}")

                    await send_telegram(chat_id, f"⚠️ No pending item found matching [{_shortcode}].")
                    return {"success": True}

                await send_telegram(chat_id, fail(result['message']))
                if result['action'] in ('staging_failed',):
                    raise Exception(result['message'])
                return {"success": True}

            except Exception as _sc_err:
                audit_log_sync("webhook", "WARNING", f"Shortcode handler error: {_sc_err}")
                await send_telegram(chat_id, "That didn't go through — try again or use /ep to retry.")
                return {"success": True}

        if text.strip().startswith('ed '):
            await handle_ed_command(text, chat_id)
            return {"success": True}

        # Support explicit session_id from metadata (app thread continuity)
        metadata = update.get('metadata') or {}
        explicit_session_id = metadata.get('session_id') if metadata else None

        if explicit_session_id and isinstance(explicit_session_id, str) and len(explicit_session_id) > 8:
            # Resume existing thread directly
            session_id = explicit_session_id
            history = get_history(session_id)
            active_anchor = None
            try:
                t_res = supabase.table('conversation_threads').select('active_anchor').eq('id', session_id).execute()
                if t_res.data:
                    active_anchor = _fresh_anchor(t_res.data[0].get('active_anchor'))
            except Exception:
                pass
        else:
            session_id, history, active_anchor = get_or_create_session(chat_id, message_text=text)
        capture_session_id(session_id)

        # ── CONSUMER PRECEDENCE: Check active workflow before normal routing ──
        try:
            result = await check_and_resume_workflow(chat_id, text, session_id)
            workflow_handled, ancillary_text = result if isinstance(result, tuple) else (result, None)
            if workflow_handled:
                if ancillary_text:
                    text = ancillary_text
                else:
                    return {"success": True}
        except Exception as e:
            audit_log_sync("workflow", "ERROR", f"Workflow check failed, falling open: {e}")

        try:
            # Check for empty /note continuation state
            last_msg_res = supabase.table('conversations') \
                .select('id, intent, created_at') \
                .eq('session_id', session_id) \
                .eq('role', 'bot') \
                .order('created_at', desc=True) \
                .limit(1) \
                .execute()
            last_msg_data = last_msg_res.data[0] if last_msg_res.data else None
            last_msg = last_msg_data
            if last_msg and last_msg.get('intent') == 'WAITING_FOR_NOTE':
                    # Check 5 min timeout
                    msg_time_str = last_msg.get('created_at', '')
                    if msg_time_str:
                        if msg_time_str.endswith('Z'):
                            msg_time_str = msg_time_str[:-1] + '+00:00'
                        msg_time = datetime.fromisoformat(msg_time_str)
                        if datetime.now(timezone.utc) - msg_time < timedelta(minutes=5):
                            text = f"/note {text}"
                            try:
                                supabase.table('conversations').update({'intent': 'WAITING_FOR_NOTE_CONSUMED'}).eq('id', last_msg['id']).execute()
                            except Exception:
                                pass
        except Exception as e:
            audit_log_sync("webhook", "WARNING", f"Error checking waiting_for_note state: {e}")

        # Dynamic CLARIFICATION_REPLY_WORDS derived from INTENT_OPTIONS
        # to avoid hardcoded drift between handler.py and classify.py
        _intent_option_words = set()
        from core.webhook.classify import INTENT_OPTIONS
        for _sc, (_intent_name, _intent_label) in INTENT_OPTIONS.items():
            _intent_option_words.add(_sc)
            _intent_option_words.add(_intent_name.lower().replace('_', ''))
        _clar_reply_words = _intent_option_words | {'u', 'update', 'new', 'create', 'none'}
        if text.strip().lower() in _clar_reply_words or text.strip().isdigit():
            try:
                last_clar = supabase.table('conversations') \
                    .select('content') \
                    .eq('session_id', session_id) \
                    .eq('role', 'bot') \
                    .eq('intent', 'CLARIFICATION') \
                    .order('created_at', desc=True) \
                    .limit(1) \
                    .execute()
                last_clar_data = last_clar.data[0] if last_clar.data else None
                if last_clar_data:
                    meta = json.loads(last_clar_data['content'])
                    if isinstance(meta, dict):
                        if meta.get('confirmation') == 'task_update':
                            if await resolve_task_update_confirmation(text, chat_id, session_id, meta):
                                return {"success": True}
                        elif meta.get('confirmation') == 'completion_disambiguation':
                            from core.webhook.completion_handler import resolve_completion_disambiguation
                            if await resolve_completion_disambiguation(text, chat_id, session_id, meta):
                                return {"success": True}
                        elif meta.get('possible_intents'):
                            if await resolve_disambiguation(text, chat_id, session_id, meta):
                                return {"success": True}
            except Exception:
                pass

        # ── "Why" conversational short-circuit ──
        _why_phrases = ('why did', 'why didn\'t', 'why was', 'why wasn\'t', 'why is', 'why does',
                         'how come', 'explain why', 'why did you', 'why wasn\'t that', 'why not')
        _tl = text.strip().lower()
        if _tl == '/why' or any(_tl.startswith(p) for p in _why_phrases):
            from core.webhook.why_handler import handle_why
            await handle_why(chat_id, session_id)
            return {"success": True}

        if text.strip().lower() in ('/today', '/brief', '/day'):
            log_exchange(session_id, 'user', 'DAILY_BRIEF', text, chat_id, metadata={"active_anchor": active_anchor} if active_anchor else None)
            reply = await handle_daily_brief(text, chat_id, session_id=session_id)
            if reply:
                capture_response(reply)
            return {"success": True}

        if text.startswith('?'):
            query = text[1:].strip()
            if query:
                log_exchange(session_id, 'user', 'QUERY', text, chat_id, metadata={"active_anchor": active_anchor} if active_anchor else None)
                reply = await interrogate_brain(query, chat_id, session_id=session_id, active_anchor=active_anchor)
                if reply:
                    capture_response(reply)
                return {"success": True}

        if text.strip().lower() == '/note':
            await send_telegram(chat_id, "What's on your mind?")
            log_exchange(session_id, 'bot', 'WAITING_FOR_NOTE', "What's on your mind?", chat_id)
            return {"success": True}

        _note_match = re.match(r'^/note\s+(.+)$', text.strip(), re.IGNORECASE | re.DOTALL)
        if _note_match:
            note_content = _note_match.group(1).strip()
            
            # 1. Run classifier to get entity extraction
            context = await get_recent_context(limit=2)
            classify_context_text = format_classify_context(history, active_anchor=active_anchor)
            classification = await classify_intent(note_content, context, ist_hour=now.hour, core_json=core_json, conversation_history=classify_context_text)
            
            # 2. Lock intent and confidence
            classification['intent'] = 'NOTE'
            classification['confidence'] = 1.0
            classification['receipt'] = '🧠'
            
            # 3. Pass to route_by_intent
            is_web_source = update.get('update_id') and str(update.get('update_id')).startswith('web_')
            source = "web" if is_web_source else "telegram"
            sender = "user"
            
            log_exchange(session_id, 'user', 'NOTE', text, chat_id, metadata={"active_anchor": active_anchor} if active_anchor else None)
            
            await route_by_intent('NOTE', note_content, chat_id, session_id, classification=classification, source=source, sender=sender, active_anchor=active_anchor)
            return {"success": True}

        _drop_match = re.match(r'^/drop-(.+)$', text.strip(), re.IGNORECASE)
        if _drop_match:
            practice_name = _drop_match.group(1).strip().replace('-', ' ')
            try:
                node_res = supabase.table('graph_nodes') \
                    .select('id, label, metadata') \
                    .eq('type', 'practice') \
                    .ilike('label', practice_name) \
                    .eq('is_current', True) \
                    .limit(1) \
                    .execute()
                if not node_res.data:
                    await send_telegram(chat_id, f"No practice found matching '{practice_name}'.")
                    return {"success": True}

                node = node_res.data[0]
                raw_meta = node.get('metadata') or {}
                if isinstance(raw_meta, str):
                    try:
                        raw_meta = json.loads(raw_meta)
                    except Exception:
                        raw_meta = {}

                raw_meta['status'] = 'dismissed'
                raw_meta['dismissed_at'] = now_ist().strftime('%Y-%m-%d')

                supabase.table('graph_nodes') \
                    .update({'metadata': raw_meta}) \
                    .eq('id', node['id']) \
                    .execute()

                variants = raw_meta.get('variants', [node.get('label', practice_name)])
                exclusion_res = maybe_single_safe(
                    supabase.table('core_config')
                    .select('content')
                    .eq('key', 'dismissed_practice_variants')
                )
                existing_exclusion = json.loads(exclusion_res.data.get('content') or '[]') if exclusion_res.data else []
                existing_lower = set(v.lower() for v in existing_exclusion)
                new_entries = [v for v in variants if v.lower() not in existing_lower]
                if new_entries:
                    updated_exclusion = existing_exclusion + new_entries
                    supabase.table('core_config') \
                        .update({'content': json.dumps(updated_exclusion)}) \
                        .eq('key', 'dismissed_practice_variants') \
                        .execute()

                label = node.get('label', practice_name)
                await send_telegram(chat_id, f"Dismissed: {label}")
                audit_log_sync("webhook", "INFO", f"DROP: Dismissed practice '{label}' — {len(new_entries)} variants excluded.")

            except Exception as _drop_err:
                audit_log_sync("webhook", "WARNING", f"/drop error: {_drop_err}")
                await send_telegram(chat_id, "Failed to dismiss practice. Try again.")
            return {"success": True}

        classify_context_text = format_classify_context(history, active_anchor=active_anchor)

        # Start anaphora resolution NOW — it doesn't depend on classify result,
        # so it runs concurrently with classify + context assembly (~5s saved).
        _anaphora_task = asyncio.create_task(
            resolve_anaphora(text, active_anchor, classify_context_text, session_id)
        )
        # Guard: cancel anaphora_task on any early return before route_by_intent consumes it.
        # Prevents abandoned Gemini calls that burn API quota on Modal cold starts.
        # route_by_intent receives the task and consumes it (awaits) — all other paths must cancel.

        # Bare URL short-circuit: bypass LLM classification entirely
        stripped = text.strip()
        if re.match(r'^https?://\S+$', stripped):
            _anaphora_task.cancel()
            audit_log_sync("webhook", "INFO", f"Bare URL short-circuit — routing to NOTE: {stripped[:50]}...")
            await handle_confident_note(stripped, chat_id, "Repository link logged for the project vault.", source="telegram")
            return {"success": True}

        context = await get_recent_context(limit=2)
        classification = await classify_intent(text, context, ist_hour=now.hour, core_json=core_json, conversation_history=classify_context_text)

        intent = classification.get('intent', 'TASK')
        confidence = classification.get('confidence', 0.5)

        audit_log_sync("webhook", "INFO", f"Intent: {intent} ({confidence:.0%}) - {text[:50]}...")

        # ── Auto-execution feedback loop: check if previous Planner auto-execute
        # needs confirmation or correction based on user's actual intent ──
        _auto_exec = active_anchor and active_anchor.get('_last_auto_execution')
        if _auto_exec:
            _prev_intent = _auto_exec.get('intent')
            _auto_feat = {
                "auto_executed": _prev_intent,
                "user_intent": intent,
                "auto_confidence": _auto_exec.get('confidence'),
            }
            if _prev_intent and _prev_intent != intent:
                await emit_observation(
                    subsystem="classification",
                    event_type="auto_execute_feedback",
                    features=_auto_feat,
                    predicted=_prev_intent,
                    actual=intent,
                    outcome="corrected",
                    session_id=session_id,
                )
                audit_log_sync(
                    "webhook", "INFO",
                    f"Auto-execution corrected: auto={_prev_intent} user={intent}")
            elif _prev_intent and _prev_intent == intent:
                await emit_observation(
                    subsystem="classification",
                    event_type="auto_execute_feedback",
                    features=_auto_feat,
                    predicted=_prev_intent,
                    actual=intent,
                    outcome="confirmed",
                    session_id=session_id,
                )
                audit_log_sync(
                    "webhook", "INFO",
                    f"Auto-execution confirmed: intent={intent}")
            # Clear the pending auto-execution
            active_anchor.pop('_last_auto_execution', None)
            try:
                supabase.table('conversation_threads').update({
                    'active_anchor': active_anchor
                }).eq('id', session_id).execute()
            except Exception:
                pass

        # Log classification stage for "/why"
        _entity = classification.get('entity', '')
        await log_decision(
            stage=DecisionStage.CLASSIFICATION,
            query_text=text,
            resolved_entities=[_entity] if _entity else [],
            reason_codes=[],
            summary=f"Classified as {intent} ({confidence:.0%})"
        )

        user_meta = {}
        if active_anchor:
            user_meta["active_anchor"] = active_anchor
        log_exchange(session_id, 'user', intent, text, chat_id, metadata=user_meta)

        is_web_source = update.get('update_id') and str(update.get('update_id')).startswith('web_')
        source = "web" if is_web_source else "telegram"
        sender = "user"

        if text.startswith('/') or text in ['Urgent', 'Brief', 'Season Context', 'Vault', 'Library', 'Status']:
            _anaphora_task.cancel()
            return await handle_command(text, chat_id)

        if text.startswith('N:') or text.startswith('Note:'):
            _anaphora_task.cancel()
            note_content = text[2:].strip() if text.startswith('N:') else text[5:].strip()
            if note_content:
                receipt = "Note vaulted."
                await handle_confident_note(note_content, chat_id, receipt, source=source, session_id=session_id, active_anchor=active_anchor)
            return {"success": True}

        if re.match(r'^undo\s+(n(?:ote)?|t(?:ask)?|d(?:elete)?)\s*$', text.strip(), re.IGNORECASE):
            _anaphora_task.cancel()
            return await handle_undo_command(text, chat_id)

        receipt = classification.get('receipt')

        # C2: Dynamic per-intent confidence thresholds
        thresholds = INTENT_THRESHOLDS.get(intent, (0.8, 0.5))
        CONFIDENCE_HIGH = thresholds[0]
        CONFIDENCE_LOW = thresholds[1]
        if intent == 'TASK' and confidence >= CONFIDENCE_HIGH:
            first_word = text.strip().lower().split()[0] if text.strip() else ''
            if first_word in UPDATE_TRIGGER_WORDS:
                matched = check_task_overlap_for_update(text)
                if matched:
                    _anaphora_task.cancel()
                    audit_log_sync("webhook", "INFO", f"Task update overlap detected — asking: {text[:50]}...")
                    await ask_task_update_confirmation(text, classification, chat_id, session_id, matched)
                    return {"success": True}

        # COMPLETION intent no longer overridden by regex heuristic.
        # All intents go through the LLM classify; the `contains_hidden_action` 
        # field in classify output handles multi-intent detection.
        
        title = classification.get('title', text) if classification else text
        entity = classification.get('entity') if classification else None

        if confidence >= CONFIDENCE_HIGH:
            print(f"[HANDLER_DEBUG] Routing: intent={intent}, confidence={confidence}, text={text!r}", flush=True)
            
            # --- UNIFIED SUGGESTION EXTRACTION FOR MESSAGES ---
            # If the intent is TASK or NOTE and the message is from the app, check if it's rich enough for a suggestion card.
            if intent in ('TASK', 'NOTE') and source == 'web':
                from core.lib.suggestion_extractor import extract_suggestions
                from core.lib.entity_context import extract_context_from_source
                
                # 1. Deterministic extraction (fast)
                ctx = await extract_context_from_source(text, timing="card")
                
                # 2. Extract suggestions (absorbs planner)
                actions, suggestion_dict = await extract_suggestions(text, title=title, entity=entity, active_anchor=active_anchor, intent=intent)
                
                matched_task_id = suggestion_dict.get("matched_task_id") if suggestion_dict else None
                
                # 3. Compute new_entities
                entities = ctx.detected_entities
                from core.pulse.graph import match_existing_nodes
                from core.services.db import get_tenant, tenant_aware_client, channel_tenant_scope
                owner_id = get_tenant()
                
                if entities and owner_id:
                    entities = match_existing_nodes(entities, owner_id)
                    
                if suggestion_dict:
                    suggestion_dict["suggested_entities"] = entities

                new_entities = [e for e in entities if not e.get("existing_matches")]
                should_show_card = bool(new_entities)
                
                # 4. ALWAYS log inbound message for the App (web)
                msg_id = 0
                supabase = tenant_aware_client()
                with channel_tenant_scope():
                    try:
                        msg_dump_res = supabase.table('raw_dumps').insert({
                            'content': text,
                            'source': 'web',
                            'owner_id': owner_id,
                            'direction': 'inbound',
                            'message_type': 'text',
                            'status': 'processed',
                            'sender': 'user',
                        }).execute()
                        msg_id = msg_dump_res.data[0]['id'] if msg_dump_res.data else 0
                        if suggestion_dict:
                            suggestion_dict['message_id'] = msg_id
                    except Exception as e:
                        
                        audit_log_sync("webhook", "ERROR", f"Failed to insert inbound raw_dump: {e}")
                
                # 5. Execute actions immediately
                if actions:
                    if not matched_task_id and ctx.pending_org_id:
                        for a in actions:
                            if getattr(a, "operation", "") == "create_task" and not getattr(a, "organization_id", None):
                                a.organization_id = ctx.pending_org_id
                                if hasattr(a, "params"):
                                    a.params["organization_id"] = ctx.pending_org_id
                                
                    from core.actions.executor import execute_planned_actions
                    # If we are showing the card, suppress the normal executor success message (the card IS the reply).
                    # If we are NOT showing the card, let the executor send its normal success push!
                    await execute_planned_actions(
                        actions, chat_id, text=text, entity=entity, source=source, sender=sender,
                        session_id=session_id, intent=intent, suppress_telegram=should_show_card, active_anchor=active_anchor
                    )
                    
                    # Update matched_task_id if we created one
                    for act in actions:
                        if getattr(act, "operation", "") == "create_task" and "_created_task_id" in getattr(act, "params", {}):
                            matched_task_id = act.params["_created_task_id"]
                
                if suggestion_dict:
                    suggestion_dict["matched_task_id"] = matched_task_id

                if should_show_card:
                    from core.services.reply_delivery import deliver_outbound_reply
                    if _anaphora_task:
                        _anaphora_task.cancel()
                        
                    text_response = "I extracted a few items from your message. Please review:"
                    await deliver_outbound_reply(message_text=text_response)
                    
                    with channel_tenant_scope():
                        try:
                            supabase.table('raw_dumps').insert({
                                'content': "Suggestion Card",
                                'source': 'web',
                                'owner_id': owner_id,
                                'direction': 'outgoing',
                                'message_type': 'suggestion',
                                'status': 'completed',
                                'sender': 'system',
                                'metadata': {
                                    'suggestion_breakdown': suggestion_dict,
                                    'entity_context': ctx.to_dict()
                                }
                            }).execute()
                        except Exception as e:
                            
                            audit_log_sync("webhook", "ERROR", f"Failed to insert suggestion raw_dump: {e}")
                    
                    
                    report(req_trace_id)
                    return {"success": True}
                else:
                    if _anaphora_task:
                        _anaphora_task.cancel()
                    
                    report(req_trace_id)
                    return {"success": True}

            await route_by_intent(intent, text, chat_id, session_id, classification=classification, source=source, sender=sender, active_anchor=active_anchor, anaphora_task=_anaphora_task)
        elif intent == 'CLARIFICATION_NEEDED':
            _anaphora_task.cancel()
            await handle_clarification(
                text,
                classification.get('clarification_question', 'Could you provide more details?'),
                chat_id,
                session_id=session_id,
                receipt=receipt
            )
        elif confidence >= CONFIDENCE_LOW:
            await route_by_intent(intent, text, chat_id, session_id, classification=classification, source=source, sender=sender, active_anchor=active_anchor, anaphora_task=_anaphora_task)
        else:
            _anaphora_task.cancel()
            await handle_clarification(
                text,
                classification.get('clarification_question', 'Could you provide more details?'),
                chat_id,
                session_id=session_id,
                receipt=receipt
            )

        report(req_trace_id)
        return {"success": True}

    except Exception as e:
        audit_log_sync("webhook", "ERROR", f"Webhook Error: {e}")
        report(req_trace_id)
        try:
            if chat_id:
                await send_telegram(chat_id, "Something went wrong. Try again or report this.")
        except Exception:
            pass
        return {"error": str(e), "status": 500}
