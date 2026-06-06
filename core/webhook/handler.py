import os
import json
import re
from datetime import datetime, timezone, timedelta
from core.lib.audit_logger import audit_log_sync
from core.lib.conversation import get_or_create_session, log_exchange, format_history_for_prompt
from core.webhook.telegram import send_telegram, download_telegram_file, answer_callback_query

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
        # Example data: "approve_e123" or "reject_w45"
        import re
        match = re.match(r'^(approve|reject)_([ecwECW]?)(\d+)$', data)
        if match:
            action, prefix, shortcode = match.groups()
            is_approve = (action == 'approve')
            sc_int = int(shortcode)
            
            prefix = prefix.lower()
            if prefix == 'e':
                result = await process_email_pending_decision(sc_int, 'approve' if is_approve else 'reject')
            elif prefix == 'c':
                result = await process_call_pending_decision(sc_int, 'approve' if is_approve else 'reject')
            elif prefix == 'w':
                result = await process_whatsapp_pending_decision(sc_int, 'approve' if is_approve else 'reject')
            else:
                # Unprefixed, try email then call then whatsapp
                result = await process_email_pending_decision(sc_int, 'approve' if is_approve else 'reject')
                if result.get('action') == 'not_found':
                    result = await process_call_pending_decision(sc_int, 'approve' if is_approve else 'reject')
                    if result.get('action') == 'not_found':
                        result = await process_whatsapp_pending_decision(sc_int, 'approve' if is_approve else 'reject')
            
            if result.get('success'):
                await send_telegram(chat_id, f"✅ {result.get('message', 'Done')}")
            else:
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
                if "23505" in error_msg or "already exists" in error_msg.lower():
                    print(f"Telegram retry detected for update {update_id}. Skipping.")
                    return {"success": True, "message": "Already processed"}
                else:
                    audit_log_sync("webhook", "WARNING", f"Deduplication check error: {error_msg}")
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

        core_res = supabase.table('core_config').select('key, content').execute()
        core_json = json.dumps(core_res.data or [])

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
        _approve_match = re.match(r'^(\d+)\s+(yes|approve|do it|yep|add it)$', text.strip(), re.IGNORECASE)
        _reject_match = re.match(r'^(\d+)\s+(drop|no|reject|skip|dismiss)$', text.strip(), re.IGNORECASE)

        # e-prefix: direct to email_pending_tasks
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

        # c-prefix: direct to call_pending_items
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

        # w-prefix: direct to whatsapp_messages
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

        session_id, history = get_or_create_session(chat_id)

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
            log_exchange(session_id, 'user', 'DAILY_BRIEF', text, chat_id)
            await handle_daily_brief(text, chat_id, session_id=session_id, conversation_history=history_text)
            return {"success": True}

        if text.startswith('?'):
            query = text[1:].strip()
            if query:
                history_text = format_history_for_prompt(history)
                log_exchange(session_id, 'user', 'QUERY', text, chat_id)
                await interrogate_brain(query, chat_id, session_id=session_id, conversation_history=history_text)
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

        log_exchange(session_id, 'user', intent, text, chat_id)

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
            await route_by_intent(intent, text, chat_id, session_id, classification=classification, source=source, sender=sender)
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
            await route_by_intent(intent, text, chat_id, session_id, classification=classification, source=source, sender=sender)
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
