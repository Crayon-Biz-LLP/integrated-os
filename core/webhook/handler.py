import os
import json
import re
from datetime import datetime, timezone, timedelta
from core.lib.audit_logger import audit_log_sync
from core.lib.conversation import get_or_create_session, log_exchange, format_history_for_prompt
from core.webhook.telegram import send_telegram, download_telegram_file
from core.webhook.classify import classify_intent, detect_opportunity_language, check_task_overlap_for_update, UPDATE_TRIGGER_WORDS
from core.webhook.utils import supabase, trigger_github_pulse, get_recent_context
from core.webhook.email import process_email_pending_decision, handle_ed_command
from core.webhook.dispatch import route_by_intent, ask_task_update_confirmation, resolve_task_update_confirmation, ask_intent_disambiguation, resolve_disambiguation, ask_task_or_note_confirmation, resolve_task_note_confirmation, handle_daily_brief, interrogate_brain, handle_confident_note, handle_clarification
from core.webhook.commands import handle_command, handle_undo_command
from core.webhook.multimodal import process_multimodal_content


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

        if not update or 'message' not in update:
            return {"message": "No message"}

        message = update.get('message', {})
        chat = message.get('chat', {})
        chat_id = chat.get('id')
        text = message.get('text', '')

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

        _approve_match = re.match(r'^(\d+)\s+(yes|approve|do it|yep|add it)$', text.strip(), re.IGNORECASE)
        _reject_match = re.match(r'^(\d+)\s+(drop|no|reject|skip|dismiss)$', text.strip(), re.IGNORECASE)

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

                if not _is_approve and result['action'] == 'not_found':
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
                            _rm = _n.get('metadata', {})
                            if isinstance(_rm, str):
                                _rm = json.loads(_rm)
                            _rm['status'] = 'dismissed'
                            _rm['dismissed_at'] = datetime.now(timezone(timedelta(hours=5, minutes=30))).strftime('%Y-%m-%d')
                            supabase.table('graph_nodes').update({'metadata': _rm}).eq('id', _n['id']).execute()
                            _variants = _rm.get('variants', [_n.get('label', '')])
                            _excl = supabase.table('core_config').select('content').eq('key', 'dismissed_practice_variants').maybe_single().execute()
                            _existing = json.loads(_excl.data.get('content', '[]')) if _excl.data else []
                            _existing_lower = set(v.lower() for v in _existing)
                            _new_entries = [v for v in _variants if v.lower() not in _existing_lower]
                            if _new_entries:
                                supabase.table('core_config').update({'content': json.dumps(_existing + _new_entries)}).eq('key', 'dismissed_practice_variants').execute()
                            await send_telegram(chat_id, f"Dismissed: {_n.get('label', '')}")
                            print(f"SHORTCODE DROP: Dismissed practice '{_n.get('label', '')}' via shortcode.")
                            return {"success": True}
                    except Exception as _sc_practice_err:
                        audit_log_sync("webhook", "WARNING", f"Shortcode practice fallback error: {_sc_practice_err}")

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
                                      'q', 'query', 'b', 'daily_brief', 'r', 'delegate', 'p', 'declare_practice', 'x', 'noise'}
        if text.strip().lower() in CLARIFICATION_REPLY_WORDS:
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
                raw_meta = node.get('metadata', {})
                if isinstance(raw_meta, str):
                    try:
                        raw_meta = json.loads(raw_meta)
                    except:
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
                existing_exclusion = json.loads(exclusion_res.data.get('content', '[]')) if exclusion_res.data else []
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
