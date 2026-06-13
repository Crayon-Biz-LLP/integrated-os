import os
import json
import re
from datetime import datetime, timezone, timedelta
from core.lib.audit_logger import audit_log_sync
from core.lib.conversation import get_or_create_session, log_exchange, format_history_for_prompt
from core.webhook.telegram import send_telegram, download_telegram_file, answer_callback_query
from core.webhook.classify import classify_intent, detect_opportunity_language, check_task_overlap_for_update, UPDATE_TRIGGER_WORDS
from core.webhook.utils import supabase, trigger_github_pulse, get_recent_context
from core.webhook.email import process_email_pending_decision, handle_ed_command
from core.webhook.call import process_call_pending_decision
from core.webhook.whatsapp import process_whatsapp_pending_decision
from core.webhook.teams import process_teams_pending_decision
from core.pulse.graph import process_graph_pending_decision, VALID_ORG_TAGS
from core.webhook.graph import interpret_graph_corrections, apply_graph_actions, active_sessions, get_active_session, clear_session
from core.webhook.dispatch import route_by_intent, ask_task_update_confirmation, resolve_task_update_confirmation, ask_intent_disambiguation, resolve_disambiguation, ask_task_or_note_confirmation, resolve_task_note_confirmation, handle_daily_brief, interrogate_brain, handle_confident_note, handle_clarification
from core.webhook.commands import handle_command, handle_undo_command
from core.webhook.multimodal import process_multimodal_content

# Pending graph clarification state for org_tag/context collection
pending_graph_clarifications = {}

async def resolve_graph_org_tag(chat_id: int, org_tag: str, pending_id: int, label: str):
    org_tag_upper = org_tag.upper().strip()
    if org_tag_upper not in VALID_ORG_TAGS:
        await send_telegram(chat_id, f"Invalid org tag. Use one of: {', '.join(sorted(VALID_ORG_TAGS))}")
        return
    result = await process_graph_pending_decision(
        pending_id=pending_id, decision='approve', org_tag=org_tag_upper
    )
    if result.get('success'):
        msg = f"✅ {result.get('message', 'Done')}"
        inferred = result.get('inferred_edges', [])
        if inferred:
            msg += "\n🔗 " + "\n🔗 ".join(inferred)
        await send_telegram(chat_id, msg)
    else:
        await send_telegram(chat_id, f"⚠️ {result.get('message', 'Error')}")
    if chat_id in pending_graph_clarifications:
        del pending_graph_clarifications[chat_id]

async def resolve_graph_person_context(chat_id: int, context_text: str, pending_id: int, label: str):
    ctx = context_text.strip() if context_text and context_text.strip() else None
    result = await process_graph_pending_decision(
        pending_id=pending_id, decision='approve', context=ctx
    )
    if result.get('success'):
        msg = f"✅ Approved person '{label}'"
        if ctx:
            msg += f" ({ctx})"
        inferred = result.get('inferred_edges', [])
        if inferred:
            msg += "\n🔗 " + "\n🔗 ".join(inferred)
        await send_telegram(chat_id, msg)
    else:
        await send_telegram(chat_id, f"⚠️ {result.get('message', 'Error')}")
    if chat_id in pending_graph_clarifications:
        del pending_graph_clarifications[chat_id]

async def process_callback_query(callback_query: dict):
    from core.lib.audit_logger import audit_log_sync
    callback_id = callback_query.get('id')
    data = callback_query.get('data', '')
    message = callback_query.get('message', {})
    chat_id = message.get('chat', {}).get('id')
    
    await answer_callback_query(callback_id)
    
    if not chat_id:
        return {"success": True}

    owner_id = os.getenv("TELEGRAM_CHAT_ID")
    if not owner_id or str(chat_id) != str(owner_id):
        print(f"Unauthorized callback from Chat ID: {chat_id}")
        return {"success": True}
        
    try:
        import re

        # Check for org tag selection callback
        orgtag_match = re.match(r'^orgtag_(\w+)_g(\d+)$', data)
        if orgtag_match:
            org_tag = orgtag_match.group(1)
            pending_id = int(orgtag_match.group(2))
            pending_item = supabase.table('pending_graph_nodes').select('label').eq('id', pending_id).maybe_single().execute()
            label = pending_item.data.get('label', 'Unknown') if pending_item and pending_item.data else 'Unknown'
            await resolve_graph_org_tag(chat_id, org_tag, pending_id, label)
            return {"success": True}

        # Check for person context skip callback
        persontag_match = re.match(r'^persontag_skip_g(\d+)$', data)
        if persontag_match:
            pending_id = int(persontag_match.group(1))
            pending_item = supabase.table('pending_graph_nodes').select('label').eq('id', pending_id).maybe_single().execute()
            label = pending_item.data.get('label', 'Unknown') if pending_item and pending_item.data else 'Unknown'
            await resolve_graph_person_context(chat_id, None, pending_id, label)
            return {"success": True}

        # Check for clarification cancel — reverts status to pending without approving/rejecting
        cancel_clar_match = re.match(r'^cancel_clarification_g(\d+)$', data)
        if cancel_clar_match:
            pending_id = int(cancel_clar_match.group(1))
            if chat_id in pending_graph_clarifications:
                del pending_graph_clarifications[chat_id]
            supabase.table('pending_graph_nodes').update({'status': 'pending'}).eq('id', pending_id).eq('status', 'awaiting_details').execute()
            await send_telegram(chat_id, "Cancelled. Node stays pending for next Decision Pulse.")
            return {"success": True}

        # Merge proposal callback: "merge_accept_123" or "merge_reject_123"
        merge_match = re.match(r'^merge_(accept|reject)_(\d+)$', data)
        if merge_match:
            merge_action = merge_match.group(1)
            pending_id = int(merge_match.group(2))
            pending_row = supabase.table('pending_graph_nodes').select('*').eq('id', pending_id).maybe_single().execute()
            if not pending_row or not pending_row.data:
                await send_telegram(chat_id, "Merge proposal not found.")
                return {"success": True}
            pr = pending_row.data
            if pr.get('status') != 'merge_proposed':
                await send_telegram(chat_id, "Merge proposal already processed.")
                return {"success": True}
            if merge_action == 'reject':
                supabase.table('pending_graph_nodes').update({'status': 'rejected'}).eq('id', pending_id).execute()
                await send_telegram(chat_id, f"Merge rejected for '{pr['label']}'.")
                return {"success": True}
            target_id = pr.get('merge_candidate_id')
            if not target_id:
                await send_telegram(chat_id, "Merge candidate not found in proposal.")
                return {"success": True}
            from core.lib.graph_rules import get_canonical_id
            target_canonical = get_canonical_id(target_id)
            source_node_res = supabase.table('graph_nodes').select('id').eq('label', pr['label']).maybe_single().execute()
            source_node_id = source_node_res.data['id'] if source_node_res and source_node_res.data else None
            if source_node_id:
                supabase.table('graph_nodes').update({'canonical_id': target_canonical}).eq('id', source_node_id).execute()
                supabase.table('graph_edges').update({'source_node_id': target_canonical}).eq('source_node_id', source_node_id).execute()
                supabase.table('graph_edges').update({'target_node_id': target_canonical}).eq('target_node_id', source_node_id).execute()
            supabase.table('pending_graph_nodes').update({'status': 'approved'}).eq('id', pending_id).execute()
            await send_telegram(chat_id, f"✅ Merged '{pr['label']}' → {target_canonical[:8]}... Edges reassigned.")
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
                result = await process_call_pending_decision(sc_int, 'approve' if is_approve else 'reject')
            elif prefix == 'w':
                result = await process_whatsapp_pending_decision(sc_int, 'approve' if is_approve else 'reject')
            elif prefix == 'pe':
                if action == 'edit':
                    pending_graph_clarifications[chat_id] = {
                        "pending_id": sc_int,
                        "step": "awaiting_edge_edit",
                        "type": "edge",
                        "expires_at": datetime.now() + timedelta(minutes=15)
                    }
                    pe = supabase.table('pending_graph_edges').select('source_label, relationship, target_label').eq('id', sc_int).maybe_single().execute().data
                    await send_telegram(chat_id, f"Editing edge: {pe['source_label']} → {pe['relationship']} → {pe['target_label']}\nReply with the corrected edge, e.g. `pe{sc_int} Danny KNOWS Alice` or `pe{sc_int} KNOWS`")
                    return {"success": True}
                else:
                    from core.pulse.graph import process_pending_edge_decision
                    result = await process_pending_edge_decision(sc_int, 'approve' if is_approve else 'reject')
            elif prefix == 'g':
                if not is_approve:
                    if chat_id in pending_graph_clarifications:
                        del pending_graph_clarifications[chat_id]
                    result = await process_graph_pending_decision(sc_int, 'reject')
                else:
                    pending_item = supabase.table('pending_graph_nodes').select('id, label, type').eq('id', sc_int).maybe_single().execute()
                    if pending_item and pending_item.data:
                        ptype = pending_item.data.get('type')
                        label = pending_item.data.get('label')
                        if ptype == 'project':
                            supabase.table('pending_graph_nodes').update({'status': 'awaiting_details'}).eq('id', sc_int).execute()
                            keyboard = [
                                [{"text": tag, "callback_data": f"orgtag_{tag}_g{sc_int}"}]
                                for tag in sorted(VALID_ORG_TAGS)
                            ]
                            keyboard.append([{"text": "❌ Cancel", "callback_data": f"cancel_clarification_g{sc_int}"}])
                            await send_telegram(
                                chat_id,
                                f"Pick an org tag for project '{label}':",
                                inline_keyboard=keyboard
                            )
                            return {"success": True}
                        elif ptype == 'person':
                            supabase.table('pending_graph_nodes').update({'status': 'awaiting_details'}).eq('id', sc_int).execute()
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
                    result = await process_call_pending_decision(sc_int, 'approve' if is_approve else 'reject')
                    if result.get('action') == 'not_found':
                        result = await process_whatsapp_pending_decision(sc_int, 'approve' if is_approve else 'reject')
            
            if result and result.get('success'):
                await send_telegram(chat_id, f"✅ {result.get('message', 'Done')}")
            elif result:
                if result.get('action') != 'not_found':
                    await send_telegram(chat_id, f"⚠️ {result.get('message', 'Error')}")
                else:
                    await send_telegram(chat_id, f"⚠️ No pending item found matching [{shortcode}].")
            return {"success": True}
            
        # If it didn't match the approve/reject regex, it's a state machine reply (e.g. "t", "n", "u", "1")
        return {"fallback_text": data}
        
    except Exception as e:
        audit_log_sync("webhook", "ERROR", f"Callback query processing failed: {e}")
        await send_telegram(chat_id, "Something went wrong processing your button tap.")
        
    return {"success": True}

async def process_webhook(update: dict):
    try:
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
                    print(f"Telegram retry detected for update {update_id}. Skipping.")
                    return {"success": True, "message": "Already processed"}
                else:
                    audit_log_sync("webhook", "WARNING", f"Deduplication check error: {error_msg}")
                    # Fail open if it's a random DB timeout so we don't drop the message
                    pass

        ist_offset = timezone(timedelta(hours=5, minutes=30))
        now = datetime.now(ist_offset)

        intent_signal = update.get('intent')
        auth_secret = update.get('auth_secret')

        if intent_signal == 'JOURNAL_SYNC':
            if auth_secret != os.getenv("PULSE_SECRET"):
                print("Unauthorized Journal Sync attempt.")
                return {"status": "unauthorized", "message": "Invalid Secret"}
            print("JOURNAL_SYNC signal received from Google Sheets.")
            triggered = await trigger_github_pulse()
            if triggered:
                owner_id = os.getenv("TELEGRAM_CHAT_ID")
                if owner_id:
                    await send_telegram(owner_id, "Journal signal received. Synchronizing archive and re-wiring graph...")
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
            core_res = supabase.table('core_config').select('key, content').execute()
            core_json = json.dumps(core_res.data or [])
        except Exception as e:
            audit_log_sync("webhook", "WARNING", f"core_config fetch failed: {e}")
            core_json = "[]"

        if not chat_id:
            return {"success": True}

        owner_id = os.getenv("TELEGRAM_CHAT_ID")
        if not owner_id or str(chat_id) != str(owner_id):
            print(f"Unauthorized access from Chat ID: {chat_id}")
            return {"message": "Unauthorized"}

        if not text:
            photo = message.get('photo')
            voice = message.get('voice')
            audio = message.get('audio')
            document = message.get('document')

            if photo:
                file_id = photo[-1].get('file_id')
                await send_telegram(chat_id, "Processing image...")
                file_bytes, mime = await download_telegram_file(file_id)
                await process_multimodal_content(file_bytes, mime, chat_id, ist_hour=now.hour, core_json=core_json)
                return {"success": True}

            elif voice or audio:
                file_id = voice.get('file_id') or audio.get('file_id')
                await send_telegram(chat_id, "Processing audio...")
                file_bytes, mime = await download_telegram_file(file_id)
                await process_multimodal_content(file_bytes, mime, chat_id, ist_hour=now.hour, core_json=core_json)
                return {"success": True}

            elif document:
                file_id = document.get('file_id')
                mime = document.get('mime_type', '')

                if mime in ['application/pdf', 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'] or mime.startswith('text/'):
                    await send_telegram(chat_id, "Processing document...")
                    file_bytes, mime = await download_telegram_file(file_id)
                    await process_multimodal_content(file_bytes, mime, chat_id, ist_hour=now.hour, core_json=core_json)
                    return {"success": True}
                else:
                    await send_telegram(chat_id, "Unsupported file type. Send as PDF, DOCX, or text.")
                    return {"success": True}

            await send_telegram(chat_id, "I can only process text, images, audio, and documents.")
            return {"success": True}

        MAX_TEXT_LENGTH = 10000
        if len(text) > MAX_TEXT_LENGTH:
            await send_telegram(chat_id, f"Message too long ({len(text)} chars). Please send shorter messages (max {MAX_TEXT_LENGTH} chars).")
            return {"success": True}

        _email_approve_match = re.match(r'^[eE](\d+)\s+(yes|approve|do it|yep|add it)$', text.strip(), re.IGNORECASE)
        _email_reject_match = re.match(r'^[eE](\d+)\s+(drop|no|reject|skip|dismiss)$', text.strip(), re.IGNORECASE)
        _call_approve_match = re.match(r'^[cC](\d+)\s+(yes|approve|do it|yep|add it)$', text.strip(), re.IGNORECASE)
        _call_reject_match = re.match(r'^[cC](\d+)\s+(drop|no|reject|skip|dismiss)$', text.strip(), re.IGNORECASE)
        _whatsapp_approve_match = re.match(r'^[wW](\d+)\s+(yes|approve|do it|yep|add it)$', text.strip(), re.IGNORECASE)
        _whatsapp_reject_match = re.match(r'^[wW](\d+)\s+(drop|no|reject|skip|dismiss)$', text.strip(), re.IGNORECASE)
        _teams_approve_match = re.match(r'^[tT](\d+)\s+(yes|approve|do it|yep|add it)$', text.strip(), re.IGNORECASE)
        _teams_reject_match = re.match(r'^[tT](\d+)\s+(drop|no|reject|skip|dismiss)$', text.strip(), re.IGNORECASE)
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
                await send_telegram(chat_id, "⏳ Applying corrections...")
                results = await apply_graph_actions(session['actions'], session['original_items_map'])
                clear_session(chat_id)
                summary_text = f"Applied: {results['applied']} | Failed: {results['failed']}\n" + "\n".join(results['details'])
                await send_telegram(chat_id, summary_text)
                return {"success": True}
            
            # Did they cancel the session?
            if text.strip().lower() in ('no', 'cancel', 'stop', 'drop', 'n'):
                clear_session(chat_id)
                await send_telegram(chat_id, "Session cancelled. Items remain pending.")
                return {"success": True}
            
            # If they sent something else, assume it's a modification to the proposal.
            # It will fall through to the NLP check below.

        # ---------------------------------------------------------
        # PENDING GRAPH CLARIFICATION CHECK (org_tag/context text replies)
        # ---------------------------------------------------------
        if chat_id in pending_graph_clarifications:
            clar = pending_graph_clarifications[chat_id]
            if 'expires_at' in clar and datetime.now() > clar['expires_at']:
                del pending_graph_clarifications[chat_id]
            else:
                step = clar.get('step')
                if text.strip().lower() in ('cancel',):
                    supabase.table('pending_graph_nodes').update({'status': 'pending'}).eq('id', clar['pending_id']).eq('status', 'awaiting_details').execute()
                    del pending_graph_clarifications[chat_id]
                    await send_telegram(chat_id, "Cancelled. Node stays pending for next Decision Pulse.")
                    return {"success": True}
                if step == 'awaiting_person_context':
                    if text.strip().lower() in ('skip', 'no', 'none', 'n/a'):
                        await resolve_graph_person_context(chat_id, None, clar['pending_id'], clar['label'])
                    else:
                        await resolve_graph_person_context(chat_id, text, clar['pending_id'], clar['label'])
                    return {"success": True}
                elif step == 'awaiting_org_tag':
                    await resolve_graph_org_tag(chat_id, text, clar['pending_id'], clar['label'])
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
                        
                    from core.pulse.graph import process_pending_edge_decision
                    result = await process_pending_edge_decision(
                        pending_id=_sc, decision='approve',
                        new_source=new_source, new_target=new_target, new_rel=new_rel
                    )
                    if result.get('success'):
                        await send_telegram(chat_id, f"✅ {result['message']}")
                    else:
                        await send_telegram(chat_id, f"⚠️ {result.get('message', 'Error')}")
                    del pending_graph_clarifications[chat_id]
                    return {"success": True}

        # DB recovery: if in-memory state was lost (restart/cold start), check awaiting_details items directly
        text_clean = text.strip().lower()
        org_tag_upper = text_clean.upper().strip()
        if org_tag_upper in VALID_ORG_TAGS:
            awaiting_projects = supabase.table('pending_graph_nodes').select('id, label').eq('status', 'awaiting_details').eq('type', 'project').limit(2).execute().data or []
            if len(awaiting_projects) == 1:
                item = awaiting_projects[0]
                pending_graph_clarifications[chat_id] = {
                    "pending_id": item['id'], "step": "awaiting_org_tag",
                    "type": "project", "label": item['label'],
                    "expires_at": datetime.now() + timedelta(minutes=5)
                }
                await resolve_graph_org_tag(chat_id, org_tag_upper, item['id'], item['label'])
                return {"success": True}
        else:
            awaiting_people = supabase.table('pending_graph_nodes').select('id, label').eq('status', 'awaiting_details').eq('type', 'person').limit(2).execute().data or []
            if len(awaiting_people) == 1 and text_clean not in ('yes', 'no', 'approve', 'reject', 'drop', 'skip'):
                item = awaiting_people[0]
                pending_graph_clarifications[chat_id] = {
                    "pending_id": item['id'], "step": "awaiting_person_context",
                    "type": "person", "label": item['label'],
                    "expires_at": datetime.now() + timedelta(minutes=5)
                }
                if text_clean in ('skip', 'no', 'none', 'n/a'):
                    await resolve_graph_person_context(chat_id, None, item['id'], item['label'])
                else:
                    await resolve_graph_person_context(chat_id, text_clean, item['id'], item['label'])
                return {"success": True}

        # ---------------------------------------------------------
        # QUICK DECISION ROUTES (Binary Approve/Reject)
        # ---------------------------------------------------------

        # g-prefix: direct to pending_graph_nodes
        if _graph_approve_match:
            try:
                _sc = _graph_approve_match.group(1)
                pending_item = supabase.table('pending_graph_nodes').select('id, label, type').eq('id', int(_sc)).maybe_single().execute()
                if pending_item and pending_item.data:
                    ptype = pending_item.data.get('type')
                    label = pending_item.data.get('label')
                    if ptype == 'project':
                        supabase.table('pending_graph_nodes').update({'status': 'awaiting_details'}).eq('id', int(_sc)).execute()
                        pending_graph_clarifications[chat_id] = {
                            "pending_id": int(_sc),
                            "step": "awaiting_org_tag",
                            "type": "project",
                            "label": label,
                            "expires_at": datetime.now() + timedelta(minutes=5)
                        }
                        await send_telegram(chat_id, f"Project '{label}' needs an org tag. Reply with one: {', '.join(sorted(VALID_ORG_TAGS))} (or 'cancel' to abort)")
                        clear_session(chat_id)
                        return {"success": True}
                    elif ptype == 'person':
                        supabase.table('pending_graph_nodes').update({'status': 'awaiting_details'}).eq('id', int(_sc)).execute()
                        pending_graph_clarifications[chat_id] = {
                            "pending_id": int(_sc),
                            "step": "awaiting_person_context",
                            "type": "person",
                            "label": label,
                            "expires_at": datetime.now() + timedelta(minutes=5)
                        }
                        await send_telegram(chat_id, f"Any context for '{label}'? (role, relationship) Reply 'skip' to approve without context.")
                        clear_session(chat_id)
                        return {"success": True}
                    else:
                        result = await process_graph_pending_decision(pending_id=int(_sc), decision='approve')
                else:
                    result = await process_graph_pending_decision(pending_id=int(_sc), decision='approve')
                
                if result and result.get('success'):
                    msg = f"✅ {result.get('message', 'Done')}"
                    inferred = result.get('inferred_edges', [])
                    if inferred:
                        msg += "\n🔗 " + "\n🔗 ".join(inferred)
                    await send_telegram(chat_id, msg)
                elif result:
                    await send_telegram(chat_id, f"⚠️ {result.get('message', 'Error')}")
                
                clear_session(chat_id)
                return {"success": True}
            except Exception as _sc_err:
                audit_log_sync("webhook", "WARNING", f"Graph prefix shortcode error: {_sc_err}")
                await send_telegram(chat_id, "Something went wrong. Try again.")
                return {"success": True}

        if _graph_reject_match:
            try:
                _sc = _graph_reject_match.group(1)
                result = await process_graph_pending_decision(pending_id=int(_sc), decision='reject')
                if result.get('success'):
                    await send_telegram(chat_id, f"✅ {result['message']}")
                else:
                    await send_telegram(chat_id, f"⚠️ {result['message']}")
                clear_session(chat_id)
                return {"success": True}
            except Exception as _sc_err:
                audit_log_sync("webhook", "WARNING", f"Graph prefix shortcode error: {_sc_err}")
                await send_telegram(chat_id, "Something went wrong. Try again.")
                return {"success": True}

        if _graph_direct_match:
            try:
                _sc = int(_graph_direct_match.group(1))
                _value = _graph_direct_match.group(2)
                pending_item = supabase.table('pending_graph_nodes').select('id, label, type').eq('id', _sc).maybe_single().execute()
                if not pending_item or not pending_item.data:
                    await send_telegram(chat_id, "⚠️ Pending item not found.")
                    clear_session(chat_id)
                    return {"success": True}
                ptype = pending_item.data.get('type')
                label = pending_item.data.get('label')
                if ptype == 'project':
                    parts = _value.strip().split(None, 1)
                    first_word = parts[0].upper()
                    rest = parts[1].strip() if len(parts) > 1 else None
                    if first_word in VALID_ORG_TAGS:
                        result = await process_graph_pending_decision(
                            pending_id=_sc, decision='approve', org_tag=first_word, context=rest
                        )
                    else:
                        await send_telegram(chat_id,
                            f"⚠️ Couldn't parse an org tag from '{first_word}'.\n"
                            f"Valid tags: {', '.join(sorted(VALID_ORG_TAGS))}\n"
                            f"Reply 'g{_sc} yes' for the keyboard, or retry: g{_sc} QHORD <note>"
                        )
                        clear_session(chat_id)
                        return {"success": True}
                elif ptype == 'person':
                    result = await process_graph_pending_decision(
                        pending_id=_sc, decision='approve', context=_value.strip()
                    )
                else:
                    result = await process_graph_pending_decision(pending_id=_sc, decision='approve')
                
                if result.get('success'):
                    msg = f"✅ {result['message']}"
                    inferred = result.get('inferred_edges', [])
                    if inferred:
                        msg += "\n🔗 " + "\n🔗 ".join(inferred)
                    await send_telegram(chat_id, msg)
                else:
                    await send_telegram(chat_id, f"⚠️ {result.get('message', 'Error')}")
                    
                clear_session(chat_id)
                return {"success": True}
            except Exception as _sc_err:
                audit_log_sync("webhook", "WARNING", f"Graph direct shortcode error: {_sc_err}")
                await send_telegram(chat_id, "Something went wrong. Try again.")
                return {"success": True}

        # pe-prefix: direct to pending_graph_edges
        if _pe_approve_match or _pe_reject_match:
            try:
                _sc = (_pe_approve_match or _pe_reject_match).group(1)
                _is_approve = bool(_pe_approve_match)
                from core.pulse.graph import process_pending_edge_decision
                result = await process_pending_edge_decision(
                    pending_id=int(_sc),
                    decision='approve' if _is_approve else 'reject'
                )
                if result.get('success'):
                    await send_telegram(chat_id, f"✅ {result['message']}")
                else:
                    await send_telegram(chat_id, f"⚠️ {result.get('message', 'Error')}")
                return {"success": True}
            except Exception as _sc_err:
                audit_log_sync("webhook", "WARNING", f"Pending edge shortcode error: {_sc_err}")
                await send_telegram(chat_id, "Something went wrong. Try again.")
                return {"success": True}
                
        if _pe_edit_match:
            try:
                _sc = int(_pe_edit_match.group(1))
                _value = _pe_edit_match.group(2).strip()
                
                # Try to parse the edit value.
                # Format: "Danny KNOWS Alice" or just "KNOWS"
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
                    
                from core.pulse.graph import process_pending_edge_decision
                result = await process_pending_edge_decision(
                    pending_id=_sc, decision='approve',
                    new_source=new_source, new_target=new_target, new_rel=new_rel
                )
                if result.get('success'):
                    await send_telegram(chat_id, f"✅ {result['message']}")
                else:
                    await send_telegram(chat_id, f"⚠️ {result.get('message', 'Error')}")
                return {"success": True}
            except Exception as _sc_err:
                audit_log_sync("webhook", "WARNING", f"Pending edge edit error: {_sc_err}")
                await send_telegram(chat_id, "Something went wrong. Try again.")
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
                    await send_telegram(chat_id, f"✅ {result['message']}")
                else:
                    await send_telegram(chat_id, f"⚠️ {result['message']}")
                    if result['action'] in ('staging_failed',):
                        raise Exception(result['message'])
                return {"success": True}
            except Exception as _sc_err:
                audit_log_sync("webhook", "WARNING", f"Email prefix shortcode error: {_sc_err}")
                await send_telegram(chat_id, "Something went wrong. Try again.")
                return {"success": True}

        # c-prefix: direct to messages(call)
        if _call_approve_match or _call_reject_match:
            try:
                _sc = (_call_approve_match or _call_reject_match).group(1)
                _is_approve = bool(_call_approve_match)
                result = await process_call_pending_decision(
                    pending_id=int(_sc),
                    decision='approve' if _is_approve else 'reject'
                )
                if result['success']:
                    await send_telegram(chat_id, f"✅ {result['message']}")
                else:
                    await send_telegram(chat_id, f"⚠️ {result['message']}")
                    if result['action'] in ('staging_failed',):
                        raise Exception(result['message'])
                return {"success": True}
            except Exception as _sc_err:
                audit_log_sync("webhook", "WARNING", f"Call prefix shortcode error: {_sc_err}")
                await send_telegram(chat_id, "Something went wrong. Try again.")
                return {"success": True}

        # w-prefix: direct to messages(whatsapp)
        if _whatsapp_approve_match or _whatsapp_reject_match:
            try:
                _sc = (_whatsapp_approve_match or _whatsapp_reject_match).group(1)
                _is_approve = bool(_whatsapp_approve_match)
                result = await process_whatsapp_pending_decision(
                    pending_id=int(_sc),
                    decision='approve' if _is_approve else 'reject'
                )
                if result['success']:
                    await send_telegram(chat_id, f"✅ {result['message']}")
                else:
                    await send_telegram(chat_id, f"⚠️ {result['message']}")
                    if result['action'] in ('staging_failed',):
                        raise Exception(result['message'])
                return {"success": True}
            except Exception as _sc_err:
                audit_log_sync("webhook", "WARNING", f"WhatsApp prefix shortcode error: {_sc_err}")
                await send_telegram(chat_id, "Something went wrong. Try again.")
                return {"success": True}

        # t-prefix: direct to messages(teams)
        if _teams_approve_match or _teams_reject_match:
            try:
                _sc = (_teams_approve_match or _teams_reject_match).group(1)
                _is_approve = bool(_teams_approve_match)
                result = await process_teams_pending_decision(
                    pending_id=int(_sc),
                    decision='approve' if _is_approve else 'reject'
                )
                if result['success']:
                    await send_telegram(chat_id, f"✅ {result['message']}")
                else:
                    await send_telegram(chat_id, f"⚠️ {result['message']}")
                    if result['action'] in ('staging_failed',):
                        raise Exception(result['message'])
                return {"success": True}
            except Exception as _sc_err:
                audit_log_sync("webhook", "WARNING", f"Teams prefix shortcode error: {_sc_err}")
                await send_telegram(chat_id, "Something went wrong. Try again.")
                return {"success": True}

        # ---------------------------------------------------------
        # NLP GRAPH CORRECTIONS ROUTE (Catch-all for g{id} free-text)
        # ---------------------------------------------------------
        if re.search(r'[gG]\d+', text):
            try:
                # Fetch pending items
                pending_res = supabase.table('pending_graph_nodes').select('id, label, type, source_text').eq('status', 'pending').execute()
                pending_items = pending_res.data or []
                
                if pending_items:
                    await send_telegram(chat_id, "⏳ Interpreting your corrections...")
                    
                    # Call Gemini
                    actions = await interpret_graph_corrections(text, pending_items)
                    
                    if not actions:
                        await send_telegram(chat_id, "⚠️ Couldn't parse any structured actions from that. Try again?")
                        return {"success": True}
                    
                    # Store in session cache
                    original_map = {item['id']: item for item in pending_items}
                    active_sessions[chat_id] = {
                        "actions": actions,
                        "original_items_map": original_map,
                        "expires_at": datetime.now() + timedelta(minutes=5)
                    }
                    
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
                await send_telegram(chat_id, "⚠️ Failed to process graph corrections.")
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
                    await send_telegram(chat_id, f"✅ {result['message']}")
                    return {"success": True}

                if result['action'] == 'not_found':
                    call_result = await process_call_pending_decision(
                        pending_id=int(_shortcode),
                        decision='approve' if _is_approve else 'reject'
                    )
                    if call_result['success']:
                        await send_telegram(chat_id, f"✅ {call_result['message']}")
                        return {"success": True}
                    if call_result['action'] != 'not_found':
                        await send_telegram(chat_id, f"⚠️ {call_result['message']}")
                        if call_result['action'] in ('staging_failed',):
                            raise Exception(call_result['message'])
                        return {"success": True}

                    whatsapp_result = await process_whatsapp_pending_decision(
                        pending_id=int(_shortcode),
                        decision='approve' if _is_approve else 'reject'
                    )
                    if whatsapp_result['success']:
                        await send_telegram(chat_id, f"✅ {whatsapp_result['message']}")
                        return {"success": True}
                    if whatsapp_result['action'] != 'not_found':
                        await send_telegram(chat_id, f"⚠️ {whatsapp_result['message']}")
                        if whatsapp_result['action'] in ('staging_failed',):
                            raise Exception(whatsapp_result['message'])
                        return {"success": True}

                    # Not found in email, call, or WhatsApp — try practice dismissal (reject only)
                    if not _is_approve:
                        try:
                            _node_res = supabase.table('graph_nodes') \
                                .select('id, label, metadata') \
                                .eq('type', 'practice') \
                                .eq('metadata->>shortcode', str(_shortcode)) \
                                .limit(1) \
                                .maybe_single() \
                                .execute()
                            if _node_res.data:
                                _n = _node_res.data
                                _rm = _n.get('metadata') or {}
                                if isinstance(_rm, str):
                                    _rm = json.loads(_rm)
                                _rm['status'] = 'dismissed'
                                _rm['dismissed_at'] = datetime.now(timezone(timedelta(hours=5, minutes=30))).strftime('%Y-%m-%d')
                                supabase.table('graph_nodes').update({'metadata': _rm}).eq('id', _n['id']).execute()
                                _variants = _rm.get('variants', [_n.get('label', '')])
                                _excl = supabase.table('core_config').select('content').eq('key', 'dismissed_practice_variants').maybe_single().execute()
                                _existing = json.loads(_excl.data.get('content') or '[]') if _excl.data else []
                                _existing_lower = set(v.lower() for v in _existing)
                                _new_entries = [v for v in _variants if v.lower() not in _existing_lower]
                                if _new_entries:
                                    supabase.table('core_config').update({'content': json.dumps(_existing + _new_entries)}).eq('key', 'dismissed_practice_variants').execute()
                                await send_telegram(chat_id, f"Dismissed: {_n.get('label', '')}")
                                print(f"SHORTCODE DROP: Dismissed practice '{_n.get('label', '')}' via shortcode.")
                                return {"success": True}
                        except Exception as _sc_practice_err:
                            audit_log_sync("webhook", "WARNING", f"Shortcode practice fallback error: {_sc_practice_err}")

                    await send_telegram(chat_id, f"⚠️ No pending item found matching [{_shortcode}].")
                    return {"success": True}

                await send_telegram(chat_id, f"⚠️ {result['message']}")
                if result['action'] in ('staging_failed',):
                    raise Exception(result['message'])
                return {"success": True}

            except Exception as _sc_err:
                audit_log_sync("webhook", "WARNING", f"Shortcode handler error: {_sc_err}")
                await send_telegram(chat_id, "Something went wrong. Try again or use /ep to retry.")
                return {"success": True}

        if text.strip().startswith('ed '):
            await handle_ed_command(text, chat_id)
            return {"success": True}

        session_id, history, active_anchor = get_or_create_session(chat_id)

        try:
            # Check for empty /note continuation state
            last_msg_res = supabase.table('conversations') \
                .select('id, intent, created_at') \
                .eq('session_id', session_id) \
                .eq('role', 'bot') \
                .order('created_at', desc=True) \
                .limit(1) \
                .execute()
            if last_msg_res.data:
                last_msg = last_msg_res.data[0]
                if last_msg.get('intent') == 'WAITING_FOR_NOTE':
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

        CLARIFICATION_REPLY_WORDS = {'u', 'update', 'n', 'new', 'create', 't', 'task', 'note',
                                      'q', 'query', 'b', 'daily_brief', 'r', 'delegate', 'p', 'declare_practice', 'x', 'noise', 'none'}
        if text.strip().lower() in CLARIFICATION_REPLY_WORDS or text.strip().isdigit():
            try:
                last_clar = supabase.table('conversations') \
                    .select('content') \
                    .eq('session_id', session_id) \
                    .eq('role', 'bot') \
                    .eq('intent', 'CLARIFICATION') \
                    .order('created_at', desc=True) \
                    .limit(1) \
                    .execute()
                if last_clar.data:
                    meta = json.loads(last_clar.data[0]['content'])
                    if isinstance(meta, dict):
                        if meta.get('confirmation') == 'task_update':
                            if await resolve_task_update_confirmation(text, chat_id, session_id, meta):
                                return {"success": True}
                        elif meta.get('confirmation') == 'task_or_note':
                            if await resolve_task_note_confirmation(text, chat_id, session_id, meta):
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

        if text.strip().lower() in ('/today', '/brief', '/day'):
            history_text = format_history_for_prompt(history)
            log_exchange(session_id, 'user', 'DAILY_BRIEF', text, chat_id, metadata={"active_anchor": active_anchor} if active_anchor else None)
            await handle_daily_brief(text, chat_id, session_id=session_id, conversation_history=history_text)
            return {"success": True}

        if text.startswith('?'):
            query = text[1:].strip()
            if query:
                history_text = format_history_for_prompt(history)
                log_exchange(session_id, 'user', 'QUERY', text, chat_id, metadata={"active_anchor": active_anchor} if active_anchor else None)
                await interrogate_brain(query, chat_id, session_id=session_id, conversation_history=history_text, active_anchor=active_anchor)
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
            history_text = format_history_for_prompt(history)
            classification = await classify_intent(note_content, context, ist_hour=now.hour, core_json=core_json, conversation_history=history_text)
            
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
                raw_meta['dismissed_at'] = datetime.now(timezone(timedelta(hours=5, minutes=30))).strftime('%Y-%m-%d')

                supabase.table('graph_nodes') \
                    .update({'metadata': raw_meta}) \
                    .eq('id', node['id']) \
                    .execute()

                variants = raw_meta.get('variants', [node.get('label', practice_name)])
                exclusion_res = supabase.table('core_config') \
                    .select('content') \
                    .eq('key', 'dismissed_practice_variants') \
                    .maybe_single() \
                    .execute()
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
                print(f"DROP: Dismissed practice '{label}' — {len(new_entries)} variants excluded.")

            except Exception as _drop_err:
                audit_log_sync("webhook", "WARNING", f"/drop error: {_drop_err}")
                await send_telegram(chat_id, "Failed to dismiss practice. Try again.")
            return {"success": True}

        history_text = format_history_for_prompt(history)

        context = await get_recent_context(limit=2)
        classification = await classify_intent(text, context, ist_hour=now.hour, core_json=core_json, conversation_history=history_text)

        intent = classification.get('intent', 'TASK')
        confidence = classification.get('confidence', 0.5)

        print(f"Intent: {intent} ({confidence:.0%}) - {text[:50]}...")

        user_meta = {}
        if active_anchor:
            user_meta["active_anchor"] = active_anchor
        log_exchange(session_id, 'user', intent, text, chat_id, metadata=user_meta)

        is_web_source = update.get('update_id') and str(update.get('update_id')).startswith('web_')
        source = "web" if is_web_source else "telegram"
        sender = "user"

        if text.startswith('/') or text in ['Urgent', 'Brief', 'Season Context', 'Vault', 'Library', 'Status']:
            return await handle_command(text, chat_id)

        if text.startswith('N:') or text.startswith('Note:'):
            note_content = text[2:].strip() if text.startswith('N:') else text[5:].strip()
            if note_content:
                receipt = "Note vaulted."
                await handle_confident_note(note_content, chat_id, receipt, source=source)
            return {"success": True}

        if re.match(r'^undo\s+(n(?:ote)?|t(?:ask)?|d(?:elete)?)\s*$', text.strip(), re.IGNORECASE):
            return await handle_undo_command(text, chat_id)

        receipt = classification.get('receipt')

        CONFIDENCE_HIGH = 0.8
        CONFIDENCE_LOW = 0.5
        possible_intents = classification.get('possible_intents', [])

        if intent == 'TASK' and confidence >= CONFIDENCE_HIGH and detect_opportunity_language(text):
            print(f"Opportunity language detected — asking confirmation for: {text[:50]}...")
            await ask_task_or_note_confirmation(text, classification, chat_id, session_id)
            return {"success": True}

        if intent == 'TASK' and confidence >= CONFIDENCE_HIGH:
            first_word = text.strip().lower().split()[0] if text.strip() else ''
            if first_word in UPDATE_TRIGGER_WORDS:
                matched = check_task_overlap_for_update(text)
                if matched:
                    print(f"Task update overlap detected — asking: {text[:50]}...")
                    await ask_task_update_confirmation(text, classification, chat_id, session_id, matched)
                    return {"success": True}

        if confidence >= CONFIDENCE_HIGH:
            await route_by_intent(intent, text, chat_id, session_id, classification=classification, source=source, sender=sender, active_anchor=active_anchor)
        elif possible_intents and len(possible_intents) >= 2 and confidence >= CONFIDENCE_LOW:
            print(f"Ambiguous ({possible_intents}) — asking user")
            await ask_intent_disambiguation(text, possible_intents, chat_id, session_id)
        elif intent == 'CLARIFICATION_NEEDED':
            await handle_clarification(
                text,
                classification.get('clarification_question', 'Could you provide more details?'),
                chat_id,
                session_id=session_id,
                receipt=receipt
            )
        elif confidence >= CONFIDENCE_LOW:
            await route_by_intent(intent, text, chat_id, session_id, classification=classification, source=source, sender=sender, active_anchor=active_anchor)
        else:
            await handle_clarification(
                text,
                classification.get('clarification_question', 'Could you provide more details?'),
                chat_id,
                session_id=session_id,
                receipt=receipt
            )

        return {"success": True}

    except Exception as e:
        audit_log_sync("webhook", "ERROR", f"Webhook Error: {e}")
        try:
            if chat_id:
                await send_telegram(chat_id, "Something went wrong. Try again or report this.")
        except Exception:
            pass
        return {"error": str(e), "status": 500}
