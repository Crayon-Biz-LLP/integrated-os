import os
import json
import asyncio
from datetime import datetime, timezone, timedelta
from supabase import create_client, Client
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from core.lib.audit_logger import audit_log_sync
from core.services.outlook_service import get_outlook_calendar_events
from core.services.pipeline_service import add_to_failed_queue
from core.lib.conversation import get_history, log_exchange, format_history_for_prompt
from core.webhook.telegram import send_telegram
from core.webhook.classify import call_gemini_with_retry, CLASSIFICATION_MODEL, get_embedding, INTENT_OPTIONS, INTENT_BY_KEYWORD
from core.webhook.utils import is_recent_raw_dump, get_google_creds, MemoryCache, hybrid_search_graph, supabase

try:
    from core.agents.quick_process import process_single_dump, get_tasks_service
except ImportError:
    async def process_single_dump(text, metadata, tasks_service=None):
        return {"action": "skipped", "reason": "import_failed"}
    def get_tasks_service():
        return None


def _format_task_line(title: str, project_name: str, priority: str = None, suffix: str = "") -> str:
    """Format a task line with consistent [Project] bracket.
    Strips the project name from the end of the title if already embedded
    to avoid duplication like 'Qhord [Qhord]'."""
    title = title.rstrip()
    if project_name and title.lower().endswith(project_name.lower()):
        title = title[:-len(project_name)].rstrip()
    line = f"{title} [{project_name}]"
    if priority:
        line += f" ({priority})"
    if suffix:
        line += suffix
    return line

async def handle_daily_brief(text: str, chat_id: int, session_id: str = None, conversation_history: str = ""):
    """
    Handle DAILY_BRIEF intent — on-demand daily briefing.
    Parses whether the user asks about today or tomorrow, queries Google Calendar
    for that day's events, and fetches all active pending tasks + overdue items.
    """
    events_list = []
    active_tasks_list = []
    overdue_tasks = []
    recently_completed = []

    try:
        ist = timezone(timedelta(hours=5, minutes=30))
        now = datetime.now(ist)
        lowtext = text.lower()

        # Determine target day
        day_offset = 1 if 'tomorrow' in lowtext else 0
        target = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=day_offset)
        day_label = "Tomorrow" if day_offset else "Today"
        target_end = target + timedelta(days=1)
        now_utc = datetime.now(timezone.utc).isoformat()
        since_utc = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()

        # Google Calendar events for target day
        try:
            service = build('calendar', 'v3', credentials=get_google_creds(), cache=MemoryCache())
            events_res = service.events().list(
                calendarId='primary',
                timeMin=target.isoformat(),
                timeMax=target_end.isoformat(),
                singleEvents=True,
                orderBy='startTime'
            ).execute()
            for e in events_res.get('items', []):
                start = e.get('start', {})
                dt = start.get('dateTime') or start.get('date', '')
                summary = e.get('summary', 'Untitled')
                events_list.append({"time": dt, "title": summary})
        except Exception as cal_err:
            audit_log_sync("webhook", "WARNING", f"Brief calendar query failed: {cal_err}")

        # Outlook calendar events for target day
        try:
            outlook_events = get_outlook_calendar_events(target)
            for e in outlook_events:
                events_list.append({"time": e["time"], "title": e["title"]})
        except Exception as ol_err:
            audit_log_sync("webhook", "WARNING", f"Brief Outlook calendar query failed: {ol_err}")

        # All active pending tasks
        try:
            tasks_res = supabase.table('tasks') \
                .select('id, title, priority, project_id, status, reminder_at, created_at') \
                .eq('is_current', True) \
                .not_.in_('status', ['done', 'cancelled']) \
                .order('priority', desc=True) \
                .order('created_at', desc=True) \
                .execute()
            raw_tasks = tasks_res.data or []
            if raw_tasks:
                proj_ids = list(set(t.get('project_id') for t in raw_tasks if t.get('project_id')))
                proj_map = {}
                if proj_ids:
                    proj_res = supabase.table('projects').select('id, name, org_tag').in_('id', proj_ids).execute()
                    for p in (proj_res.data or []):
                        proj_map[p['id']] = p['name']
                for t in raw_tasks:
                    pn = proj_map.get(t.get('project_id'), 'INBOX')
                    ts = t.get('reminder_at')
                    due = ""
                    if ts:
                        try:
                            due_dt = datetime.fromisoformat(ts.replace('Z', '+00:00'))
                            if due_dt < target_end and due_dt >= target:
                                due = " 🔔 due today" if not day_offset else " 🔔 due tomorrow"
                        except:
                            pass
                    active_tasks_list.append(_format_task_line(t['title'], pn, t.get('priority','todo'), due))
                    reminder = t.get('reminder_at')
                    if reminder and reminder < now_utc:
                        overdue_tasks.append(_format_task_line(t['title'], pn))
        except Exception as t_err:
            audit_log_sync("webhook", "WARNING", f"Brief tasks query failed: {t_err}")

        # Recent completions
        try:
            comp_res = supabase.table('tasks') \
                .select('title, project_id') \
                .eq('is_current', False) \
                .eq('status', 'done') \
                .gte('updated_at', since_utc) \
                .order('updated_at', desc=True) \
                .limit(5) \
                .execute()
            completed_raw = comp_res.data or []
            if completed_raw:
                done_proj_ids = list(set(t.get('project_id') for t in completed_raw if t.get('project_id')))
                done_proj_map = {}
                if done_proj_ids:
                    done_proj_res = supabase.table('projects').select('id, name').in_('id', done_proj_ids).execute()
                    for p in (done_proj_res.data or []):
                        done_proj_map[p['id']] = p['name']
                for t in completed_raw:
                    pn = done_proj_map.get(t.get('project_id'), 'INBOX')
                    recently_completed.append(_format_task_line(t['title'], pn))
        except Exception:
            pass

        def fmt_list(items):
            if not items:
                return "None"
            return "\n".join(f"- {i}" for i in items)

        prompt = f"""You are Danny's Rhodey. Pragmatic, loyal, and a professional friend. You are the grounding wire to Danny's vision. You don't coach or 'motivate.' Speak simply and punchy.

Danny is asking about {day_label.lower()}. You have his calendar events for {day_label}, his full active task list, overdue items, and recent completions. Identify what matters and cut through the noise.

Answer only what Danny asked. Do not list unrelated tasks or extra context.
{conversation_history}

{day_label.upper()} — {target.strftime('%A, %d %B')}

CALENDAR EVENTS:
{fmt_list(e['title'] + (' at ' + e['time'][:16].replace('T', ' ')) if e.get('time') else e['title'] for e in events_list) if events_list else "None"}

ACTIVE TASKS:
{fmt_list(active_tasks_list) if active_tasks_list else "None"}

OVERDUE:
{fmt_list(overdue_tasks) if overdue_tasks else "None"}

RECENTLY COMPLETED (24h):
{fmt_list(recently_completed) if recently_completed else "None"}

Give a sharp, direct answer. If you spot a bottleneck or a pattern, call it out. If something is urgent, say so. If there's nothing useful, say that.

Formatting rules:
- Emoji goes at the **start** of each task line, not at the end
- Pick emojis naturally: 💰 money, 🏠 home, 📋 admin, 🛠️ work, 🏛️ ashraya/church, etc.
- Do NOT use `###` headers — use **bold** or just plain text for section breaks
- Do NOT prefix tasks with "TASK" — just list them cleanly. Do NOT include intent labels like TASK, NOTE, or QUERY anywhere in your response.
- Bullet points only, no numbered lists

Example:
**Focus here** — bottleneck callout.
* 💰 Task name [Project]
* 📋 Another task [Project]

Always use [MEMORY] or [RESOURCE] brackets when citing — never write MEMORY or RESOURCE without brackets. Preserve the [Project] bracket from the task data exactly as shown."""

        response = await call_gemini_with_retry(
            prompt=prompt,
            model=CLASSIFICATION_MODEL,
            config={'response_mime_type': 'text/plain'}
        )
        reply = response.text.strip()

    except Exception as e:
        audit_log_sync("webhook", "WARNING", f"Daily brief generation failed: {e}")
        reply = None

    if not reply:
        fallback_lines = [f"📋 *{day_label}'s Briefing*"]
        if events_list:
            fallback_lines.append("\n*Calendar:*")
            for e in events_list:
                fallback_lines.append(f"• {e['title']}")
        if active_tasks_list:
            fallback_lines.append("\n*Active Tasks:*")
            for t in active_tasks_list:
                fallback_lines.append(f"• {t}")
        if overdue_tasks:
            fallback_lines.append("\n*Overdue:*")
            for t in overdue_tasks:
                fallback_lines.append(f"• {t}")
        if not events_list and not active_tasks_list:
            fallback_lines.append(f"\nNothing on for {day_label.lower()}.")
        reply = "\n".join(fallback_lines)

    await send_telegram(chat_id, f"{reply}")

    if session_id:
        log_exchange(session_id, 'bot', 'DAILY_BRIEF', reply, chat_id)

    try:
        supabase.table('raw_dumps').insert([{
            "content": reply,
            "status": "completed",
            "is_processed": True,
            "direction": "outgoing",
            "sender": "system",
            "message_type": "briefing",
            "source": "pulse",
            "metadata": {"type": "daily_brief", "trigger": "on_demand"}
        }]).execute()
    except Exception as log_err:
        audit_log_sync("webhook", "WARNING", f"Failed to log daily brief: {log_err}")

async def handle_confident_task(text: str, title: str, time_context: str, chat_id: int, receipt: str = None, entity: str = None, source: str = "telegram", sender: str = "user", task_update_id: int = None):
    # ── Idempotency guard: skip if identical content+source inserted within 60s ──
    if is_recent_raw_dump(text, source):
        ack = receipt or "Logged."
        await send_telegram(chat_id, f"{ack}")
        return

    meta = {
        "intent": "TASK",
        "title": title,
        "time_context": time_context,
        "entity": entity
    }
    if task_update_id is not None:
        meta["task_update_id"] = task_update_id

    dump_id = None
    try:
        dump_res = supabase.table('raw_dumps').insert([{
            "content": text,
            "status": "pending",
            "direction": "incoming",
            "sender": sender,
            "message_type": "task",
            "source": source,
            "metadata": meta
        }]).execute()
        dump_id = dump_res.data[0]['id'] if dump_res.data else None
    except Exception as e:
        audit_log_sync("webhook", "ERROR", f"Failed to save task dump: {e}")

    ack = receipt or "Logged."
    await send_telegram(chat_id, f"{ack}")

    # Log acknowledgment to raw_dumps so it appears in web UI
    try:
        supabase.table('raw_dumps').insert([{
            "content": ack,
            "status": "completed",
            "is_processed": True,
            "direction": "incoming",
            "sender": "system",
            "message_type": "acknowledgment",
            "metadata": {"in_response_to": text, "type": "ack"}
        }]).execute()
    except Exception as ack_err:
        audit_log_sync("webhook", "WARNING", f"Failed to log ack to raw_dumps: {ack_err}")

    # Inline: process the dump immediately (fire-and-forget)
    if dump_id:
        try:
            tasks_service = get_tasks_service()
            result = await process_single_dump(text, meta, tasks_service)
            if result.get('action') in ('created', 'completed', 'filed'):
                supabase.table('raw_dumps').update({"status": "synced"}).eq('id', dump_id).execute()
                audit_log_sync("webhook", "INFO", f"Inline processed dump {dump_id}: {result['action']}")
        except Exception as e:
            audit_log_sync("webhook", "WARNING", f"Inline processing failed for dump {dump_id}: {e}")

async def handle_confident_note(text: str, chat_id: int, receipt: str = None, source: str = "telegram", sender: str = "user", entity: str = None):
    # ── Idempotency guard: skip if identical content+source inserted within 60s ──
    if is_recent_raw_dump(text, source):
        ack = receipt or "Note vaulted."
        await send_telegram(chat_id, f"{ack}")
        return

    # ── Step 1: Insert as staged (captured, pending processing) ──
    insert_data = {
        "content": text,
        "status": "staged",
        "direction": "incoming",
        "sender": sender,
        "message_type": "note",
        "source": source,
        "metadata": {"intent": "NOTE", "entity": entity}
    }
    try:
        dump_res = supabase.table('raw_dumps').insert([insert_data]).execute()
        dump_id = dump_res.data[0]['id'] if dump_res.data else None
    except Exception as e:
        audit_log_sync("webhook", "ERROR", f"Failed to save note dump: {e}")
        dump_id = None

    # ── Step 2: Attempt embedding ──
    embedding = await asyncio.to_thread(get_embedding, text)
    embed_success = bool(embedding and any(embedding))
    embed_status = 'success' if embed_success else 'failed'

    if not embed_success:
        # Mark as embedding_failed, write to DLQ, send retry receipt
        if dump_id:
            try:
                supabase.table('raw_dumps').update({"status": "embedding_failed"}).eq('id', dump_id).execute()
            except Exception as e:
                audit_log_sync("webhook", "ERROR", f"Failed to update dump {dump_id} to embedding_failed: {e}")
        try:
            await add_to_failed_queue('memories', str(dump_id or 'unknown'), 'embedding', 'Embedding returned null/zero vector')
        except Exception as e:
            audit_log_sync("webhook", "ERROR", f"Failed to write to failed_queue: {e}")
        ack = receipt or "✅ Captured. Memory indexing will retry shortly."
        await send_telegram(chat_id, f"{ack}")
        return

    # ── Step 3: Save to memories (success path) ──
    try:
        supabase.table('memories').insert({
            "content": text,
            "memory_type": "note",
            "embedding": embedding,
            "embedding_status": embed_status,
            "source": "webhook",
            "metadata": {"entity": entity}
        }).execute()
    except Exception as e:
        audit_log_sync("webhook", "ERROR", f"Failed to save note to memory: {e}")
        if dump_id:
            try:
                supabase.table('raw_dumps').update({"status": "embedding_failed"}).eq('id', dump_id).execute()
            except:
                pass
        try:
            await add_to_failed_queue('memories', str(dump_id or 'unknown'), 'memory_insert', str(e))
        except:
            pass
        ack = receipt or "✅ Captured. Memory indexing will retry shortly."
        await send_telegram(chat_id, f"{ack}")
        return

    # ── Step 4: Mark as processed ──
    if dump_id:
        try:
            supabase.table('raw_dumps').update({"status": "processed", "is_processed": True}).eq('id', dump_id).execute()
        except Exception as e:
            audit_log_sync("webhook", "WARNING", f"Failed to mark dump {dump_id} as processed: {e}")

    ack = receipt or "Note vaulted."
    await send_telegram(chat_id, f"{ack}")

    # Log acknowledgment to raw_dumps so it appears in web UI
    try:
        supabase.table('raw_dumps').insert([{
            "content": ack,
            "status": "processed",
            "is_processed": True,
            "direction": "outgoing",
            "sender": "system",
            "message_type": "acknowledgment",
            "source": source,
            "metadata": {"in_response_to": text, "type": "ack"}
        }]).execute()
    except Exception as ack_err:
        audit_log_sync("webhook", "WARNING", f"Failed to log ack to raw_dumps: {ack_err}")

async def handle_clarification(text: str, question: str, chat_id: int, session_id: str = None, receipt: str = None):
    ack = receipt or "Copy that. I need one more detail to log this."
    reply = f"{ack}\n\n{question}\n\n_Context: \"{text[:100]}...\"_"
    await send_telegram(chat_id, reply)

    if session_id:
        log_exchange(session_id, 'bot', 'CLARIFICATION', reply, chat_id)

    try:
        await asyncio.to_thread(
            lambda: supabase.table('raw_dumps').insert([{
                "content": text,
                "direction": "incoming",
                "sender": "telegram",
                "message_type": "clarification",
                "metadata": {"awaiting_clarification": True}
            }]).execute()
        )
    except Exception as clar_err:
        audit_log_sync("webhook", "WARNING", f"Failed to log clarification to raw_dumps: {clar_err}")

async def ask_intent_disambiguation(text: str, possible_intents: list, chat_id: int, session_id: str):
    opts = []
    for sc, (intent, label) in INTENT_OPTIONS.items():
        if intent in possible_intents:
            opts.append(f"`{sc}` — {label}")
    if not opts:
        return
    reply = (
        f"🧐 *Not sure what to do with this.* Is it?\n\n"
        + "\n".join(opts)
        + f"\n\n_Reply with a shortcode or just say it._"
    )
    log_exchange(session_id, 'bot', 'CLARIFICATION', json.dumps({"possible_intents": possible_intents, "original": text}), chat_id)
    await send_telegram(chat_id, reply)

async def resolve_disambiguation(text: str, chat_id: int, session_id: str, last_clarification: dict) -> bool:
    cleaned = text.strip().lower()
    if cleaned in INTENT_BY_KEYWORD:
        intent = INTENT_BY_KEYWORD[cleaned]
    elif cleaned in [v[0].lower() for v in INTENT_OPTIONS.values() if v[0].lower() != cleaned]:
        intent = next(v[0] for v in INTENT_OPTIONS.values() if v[0].lower() == cleaned)
    else:
        return False
    original = last_clarification.get("original", text)
    log_exchange(session_id, 'user', intent, text, chat_id)
    classification = {"title": original, "intent": intent}
    await route_by_intent(intent, original, chat_id, session_id, classification=classification)
    return True

async def ask_task_or_note_confirmation(text: str, classification: dict, chat_id: int, session_id: str):
    reply = (
        f"🧐 *Is this a task or a note?*\n\n"
        f"_{text[:200]}..._\n\n"
        f"`t` — 📋 Task — something to do\n"
        f"`n` — 📝 Note — record this"
    )
    log_exchange(
        session_id, 'bot', 'CLARIFICATION',
        json.dumps({
            "confirmation": "task_or_note",
            "possible_intents": ["TASK", "NOTE"],
            "original": text,
            "classification": classification
        }),
        chat_id
    )
    await send_telegram(chat_id, reply)

async def resolve_task_note_confirmation(text: str, chat_id: int, session_id: str, last_clarification: dict) -> bool:
    cleaned = text.strip().lower()
    if cleaned in ('t', 'task'):
        intent = 'TASK'
    elif cleaned in ('n', 'note'):
        intent = 'NOTE'
    else:
        return False
    original = last_clarification.get("original", text)
    classification = last_clarification.get("classification", {"title": original})
    classification["intent"] = intent
    log_exchange(session_id, 'user', intent, text, chat_id)
    await route_by_intent(intent, original, chat_id, session_id, classification=classification)
    return True

async def route_by_intent(intent: str, text: str, chat_id: int, session_id: str, classification: dict = None, source="telegram", sender="user", task_update_id: int = None):
    history_text = ""
    if session_id:
        pairs = get_history(session_id, max_tokens=5)
        history_text = format_history_for_prompt(pairs)

    if intent == 'TASK':
        title = classification.get('title', text) if classification else text
        receipt = classification.get('receipt') if classification else None
        entity = classification.get('entity') if classification else None
        time_context = classification.get('time_context', '') if classification else ''
        task_update_id = task_update_id if task_update_id is not None else (classification.get('task_update_id') if classification else None)
        await handle_confident_task(text, title, time_context, chat_id, receipt, entity=entity, source=source, sender=sender, task_update_id=task_update_id)
    elif intent == 'DAILY_BRIEF':
        await handle_daily_brief(text, chat_id, session_id=session_id, conversation_history=history_text)
    elif intent == 'QUERY':
        await interrogate_brain(text, chat_id, session_id=session_id, conversation_history=history_text)
    elif intent == 'NOTE':
        receipt = classification.get('receipt') if classification else None
        entity = classification.get('entity') if classification else None
        await handle_confident_note(text, chat_id, receipt or "Note secured.", source=source, sender=sender, entity=entity)
    elif intent == 'DELEGATE':
        supabase.table('agent_queue').insert({"query": text, "status": "pending"}).execute()
        ack = classification.get('receipt', "The intern is on it. I'll ping you when the research is ready.") if classification else "The intern is on it. I'll ping you when the research is ready."
        await send_telegram(chat_id, f"✓ {ack}")
    elif intent == 'DECLARE_PRACTICE':
        await handle_declare_practice(text, chat_id, classification or {})
    elif intent == 'NOISE':
        await handle_noise(chat_id)
    else:
        await handle_clarification(text, "Could you provide more details?", chat_id, session_id=session_id)

async def interrogate_brain(query: str, chat_id: int, session_id: str = None, conversation_history: str = ""):
    """On-Demand Brain Interrogation - Hybrid Graph + Vector Search."""
    try:
        await send_telegram(chat_id, "🧠 *Searching your vault...*")

        tactical_map = await hybrid_search_graph(query)

        embedding = await asyncio.to_thread(get_embedding, query)

        memories_res = supabase.rpc(
            'match_memories',
            {
                'query_embedding': embedding,
                'match_count': 5,
                'match_threshold': 0.5
            }
        ).execute()
        memories = memories_res.data if memories_res.data else []

        # TODO: If match_canonical_pages RPC does not exist yet in Supabase,
        # create it mirroring the match_memories pattern for canonical_pages table.
        combined_results = []
        for m in (memories or []):
            combined_results.append({
                "content": m.get('content', ''),
                "source": m.get('memory_type', 'memory').upper(),
                "link": m.get('url') or '',
                "similarity": m.get('similarity', 0)
            })

        try:
            canonical_res = supabase.rpc('match_canonical_pages', {
                'query_embedding': embedding,
                'match_count': 3,
                'match_threshold': 0.65
            }).execute()
            canonical_hits = canonical_res.data or []
            for hit in canonical_hits:
                combined_results.append({
                    "content": f"[CANONICAL] {hit.get('title', '')}: {hit.get('content', '')[:300]}",
                    "source": "CANONICAL",
                    "link": '',
                    "similarity": hit.get('similarity', 0)
                })
        except Exception as canon_err:
            print(f"Canonical pages search failed (RPC may not exist): {canon_err}")

        # Sort by similarity descending
        combined_results.sort(key=lambda x: x.get('similarity', 0), reverse=True)

        try:
            resources_res = supabase.table('resources').select('title, url, category, summary').execute()
            resources = resources_res.data or []
        except:
            resources = []

        # Fetch active tasks with project names
        active_tasks_list = []
        raw_tasks = []
        proj_map = {}
        try:
            tasks_res = supabase.table('tasks').select('id, title, priority, project_id, status, reminder_at, created_at').eq('is_current', True).not_.in_('status', ['done', 'cancelled']).order('priority', desc=True).order('created_at', desc=True).execute()
            raw_tasks = tasks_res.data or []
            if raw_tasks:
                proj_ids = list(set(t.get('project_id') for t in raw_tasks if t.get('project_id')))
                proj_map = {}
                if proj_ids:
                    proj_res = supabase.table('projects').select('id, name, org_tag').in_('id', proj_ids).execute()
                    for p in (proj_res.data or []):
                        proj_map[p['id']] = p['name']
                for t in raw_tasks:
                    p_name = proj_map.get(t.get('project_id'), 'INBOX')
                    active_tasks_list.append(_format_task_line(t.get('title', ''), p_name, t.get('priority', 'todo')))
        except Exception as tasks_err:
            print(f"Active tasks query failed: {tasks_err}")

        # Overdue detection — tasks past their reminder_at
        overdue_tasks = []
        now_utc = datetime.now(timezone.utc).isoformat()
        for t in raw_tasks:
            reminder = t.get('reminder_at')
            if reminder and reminder < now_utc:
                p_name = proj_map.get(t.get('project_id'), 'INBOX')
                overdue_tasks.append(_format_task_line(t.get('title', ''), p_name))

        # Recent completions — tasks done in last 24h
        recently_completed = []
        try:
            since = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
            completed_res = supabase.table('tasks').select('title, priority, project_id, updated_at').eq('is_current', False).eq('status', 'done').gte('updated_at', since).order('updated_at', desc=True).limit(5).execute()
            completed_raw = completed_res.data or []
            if completed_raw:
                done_proj_ids = list(set(t.get('project_id') for t in completed_raw if t.get('project_id')))
                done_proj_map = {}
                if done_proj_ids:
                    done_proj_res = supabase.table('projects').select('id, name').in_('id', done_proj_ids).execute()
                    for p in (done_proj_res.data or []):
                        done_proj_map[p['id']] = p['name']
                for t in completed_raw:
                    p_name = done_proj_map.get(t.get('project_id'), 'INBOX')
                    recently_completed.append(_format_task_line(t.get('title', ''), p_name))
        except Exception as done_err:
            print(f"Recent completions query failed: {done_err}")

        all_context = []

        if tactical_map:
            all_context.append(f"TACTICAL MAP:\n{tactical_map}")

        if active_tasks_list:
            all_context.append("ACTIVE TASKS:\n" + "\n".join(f"- {t}" for t in active_tasks_list))

        if overdue_tasks:
            all_context.append("OVERDUE:\n" + "\n".join(f"- {t}" for t in overdue_tasks))

        if recently_completed:
            all_context.append("RECENTLY COMPLETED (24h):\n" + "\n".join(f"- {t}" for t in recently_completed))

        for item in combined_results:
            source = item.get('source', 'memory').upper()
            content = item.get('content', '')
            link = item.get('link', '')
            all_context.append(f"[{source}] {content}" + (f" | Link: {link}" if link else ""))

        for r in resources[:3]:
            title = r.get('title', 'Untitled')
            url = r.get('url', '')
            category = r.get('category', 'resource')
            summary = r.get('summary', title)
            all_context.append(f"[{category.upper()}] {summary}" + (f" | Link: {url}" if url else ""))

        if not all_context:
            await send_telegram(chat_id, "🔍 *No relevant memories found.*\n\n_Try a different query._")
            return

        context_str = "\n\n".join(all_context)

        prompt = f"""You are Danny's Rhodey. Pragmatic, loyal, and a professional friend. You are the grounding wire to Danny's vision. You don't coach or 'motivate.' Speak simply and punchy.

Danny is asking a question. You have access to his tactical map, memories, active tasks, and resources. Look at the data below, identify what matters — dependencies, blockers — and cut through the noise.

Answer only what Danny asked. Do not list unrelated tasks or extra context.
{context_str}{conversation_history}

Question: {query}

Give a sharp, direct answer. If you spot a bottleneck or a pattern, call it out. If something is urgent, say so. If there's nothing useful, say that.

Formatting rules:
- Emoji goes at the **start** of each task line, not at the end
- Pick emojis naturally: 💰 money, 🏠 home, 📋 admin, 🛠️ work, 🏛️ ashraya/church, etc.
- Do NOT use `###` headers — use **bold** or just plain text for section breaks
- Do NOT prefix tasks with "TASK" — just list them cleanly. Do NOT include intent labels like TASK, NOTE, or QUERY anywhere in your response.
- Bullet points only, no numbered lists

Example format:
**Focus here** — clear bottleneck callout.
* 💰 Task name [Project]
* 📋 Another task [Project]

Always use [MEMORY] or [RESOURCE] brackets when citing — never write MEMORY or RESOURCE without brackets. Preserve the [Project] bracket from the task data exactly as shown."""

        response = await call_gemini_with_retry(prompt=prompt, model=CLASSIFICATION_MODEL)

        answer = response.text.strip()

        await send_telegram(chat_id, f"🧠 *Brain Interrogation:*\n\n{answer}")

        # Log bot reply to conversation history
        if session_id:
            log_exchange(session_id, 'bot', 'QUERY', answer, chat_id)

        # Log QUERY response to raw_dumps so it appears in web UI
        try:
            supabase.table('raw_dumps').insert([{
                "content": answer,
                "status": "processed",
                "is_processed": True,
                "direction": "outgoing",
                "sender": "system",
                "message_type": "response",
                "source": "pulse",
                "metadata": {
                    "type": "query_response",
                    "query": query
                }
            }]).execute()
        except Exception as log_err:
            audit_log_sync("webhook", "WARNING", f"Failed to log query response to raw_dumps: {log_err}")

    except Exception as e:
        audit_log_sync("webhook", "ERROR", f"Interrogation error: {e}")
        await send_telegram(chat_id, "⚠️ *Search failed.*\n\n_Try again._")

async def handle_noise(chat_id: int):
    await send_telegram(chat_id, "👍")

async def ask_task_update_confirmation(text: str, classification: dict, chat_id: int, session_id: str, matched_tasks: list):
    """Ask user whether to update an existing task or create a new one."""
    task = matched_tasks[0]
    reply = (
        f"🧐 *This looks like it relates to an existing task:*\n\n"
        f"_{task['title']}_\n\n"
        f"`u` — 🔄 Update existing task\n"
        f"`n` — ➕ Create new task"
    )
    log_exchange(
        session_id, 'bot', 'CLARIFICATION',
        json.dumps({
            "confirmation": "task_update",
            "matched_tasks": matched_tasks,
            "original": text,
            "classification": classification
        }),
        chat_id
    )
    await send_telegram(chat_id, reply)

async def resolve_task_update_confirmation(text: str, chat_id: int, session_id: str, last_clarification: dict) -> bool:
    """Handle user response to update-vs-create question."""
    cleaned = text.strip().lower()
    matched_tasks = last_clarification.get('matched_tasks', [])
    original = last_clarification.get("original", text)
    classification = last_clarification.get("classification", {"title": original})
    classification["intent"] = "TASK"

    if cleaned in ('u', 'update'):
        target = matched_tasks[0]
        classification["task_update_id"] = target['id']
        log_exchange(session_id, 'user', 'TASK', text, chat_id)
        await route_by_intent("TASK", original, chat_id, session_id,
                              classification=classification, task_update_id=target['id'])
        return True
    elif cleaned in ('n', 'new', 'create'):
        log_exchange(session_id, 'user', 'TASK', text, chat_id)
        await route_by_intent("TASK", original, chat_id, session_id, classification=classification)
        return True
    return False

async def handle_declare_practice(text: str, chat_id: int, classification: dict):
    """Handle DECLARE_PRACTICE intent — creates a declared practice node."""
    try:
        practice_name = classification.get('title', text).strip()
        if not practice_name or len(practice_name) < 3:
            await send_telegram(chat_id, "⚠️ Couldn't identify the practice. Try again.")
            return

        # Check for existing practice with similar label (threshold 0.85)
        existing_res = supabase.table('graph_nodes') \
            .select('id, label, metadata') \
            .eq('type', 'practice') \
            .in_('status', ['active', 'dormant']) \
            .execute()
        existing_practices = existing_res.data or []

        if existing_practices:
            name_embedding = await asyncio.to_thread(get_embedding, practice_name)
            for p in existing_practices:
                p_label = p.get('label', '')
                p_embedding = await asyncio.to_thread(get_embedding, p_label)
                dot = sum(a * b for a, b in zip(name_embedding, p_embedding))
                n_a = sum(a * a for a in name_embedding) ** 0.5
                n_b = sum(b * b for b in p_embedding) ** 0.5
                sim = dot / (n_a * n_b) if n_a and n_b else 0.0
                if sim >= 0.85:
                    await send_telegram(chat_id, f"Already tracking: {p_label}")
                    return

        # Create practice node
        ist_offset = timezone(timedelta(hours=5, minutes=30))
        now = datetime.now(ist_offset)
        metadata = {
            "declared": True,
            "canonical_name_set_at": now.strftime('%Y-%m-%d'),
            "frequency_observed": "0/14days",
            "frequency_baseline": "0/14days",
            "baseline_source": "bootstrap",
            "baseline_weeks_of_data": 0,
            "typical_time": None,
            "typical_days": [],
            "confidence": 1.0,
            "last_occurrence": None,
            "first_detected": now.strftime('%Y-%m-%d'),
            "occurrence_count": 0,
            "status": "active",
            "resumed_at": None,
            "entity": classification.get('entity'),
            "entities": [classification.get('entity')] if classification.get('entity') else [],
            "variants": [practice_name],
            "health_score": 100,
            "health_score_raw": 100
        }

        node_res = supabase.table('graph_nodes').insert({
            "label": practice_name,
            "type": "practice",
            "metadata": metadata
        }).execute()

        if node_res.data:
            await send_telegram(chat_id, f"Tracking: {practice_name}")
            print(f"📍 DECLARE_PRACTICE: Created practice node '{practice_name}'")
        else:
            await send_telegram(chat_id, "⚠️ Could not create practice. Try again.")

    except Exception as e:
        audit_log_sync("webhook", "ERROR", f"handle_declare_practice error: {e}")
        await send_telegram(chat_id, "⚠️ Something went wrong. Try again.")

