from core.lib.process_input import ProcessInput
from core.llm import get_embedding
import json
import re
import asyncio
import hashlib
from datetime import datetime, timezone, timedelta, time
from zoneinfo import ZoneInfo
from core.lib.audit_logger import audit_log_sync
from core.lib.time_utils import age_tag
from core.pulse.context import context_provider
from core.lib.conversation import get_history, log_exchange, format_history_for_prompt, get_thread_summary
from core.webhook.telegram import send_telegram
from core.webhook.classify import CLASSIFICATION_MODEL,  INTENT_OPTIONS, INTENT_BY_KEYWORD
from core.llm.fallback import generate_content_with_fallback
from core.llm.config import WorkloadProfile
from core.actions import ActionResult, accumulate_action, capture_response
from core.prompts.query import build_interrogate_brain_prompt, build_anaphora_resolution_prompt
from core.prompts.briefing import build_daily_brief_prompt
from core.prompts.workflow import build_enrichment_prompt
from core.webhook.utils import is_recent_raw_dump, supabase
from core.pulse.graph import hybrid_search_graph
from core.agents.quick_process import process_single_dump, get_tasks_service
from core.lib.decision_audit import log_decision, DecisionStage, set_decision_chain_id, get_decision_chain_id
from core.pulse.entity_extractor import extract_and_link_entities
from core.lib.graph_rules import normalize_label
from core.pulse.entity_resolver import resolve_entities_from_text
from core.services.db import maybe_single_safe, get_supabase


def _format_task_line(title: str, project_name: str, priority: str = None, suffix: str = "", organization_name: str = None) -> str:
    """Format a task line with consistent [Project] bracket.
    Strips the project name from the end of the title if already embedded
    to avoid duplication like 'Qhord [Qhord]'."""
    title = title.rstrip()
    if project_name and title.lower().endswith(project_name.lower()):
        title = title[:-len(project_name)].rstrip()
        
    from core.features import is_org_routing_enabled
    if is_org_routing_enabled() and organization_name:
        loc = f"{organization_name} · {project_name}" if project_name and project_name != "INBOX" else organization_name
        line = f"{title} [{loc}]"
    else:
        line = f"{title} [{project_name}]"
        
    if priority:
        line += f" ({priority})"
    if suffix:
        line += suffix
    return line

async def _process_task_closure(target: str, chat_id: int):
    """Find active tasks matching a target description and close them.
    
    Shared helper used by batch workflow execution and secondary_actions processing.
    Returns list of closed task titles.
    """
    supabase = get_supabase()
    tasks_res = supabase.table("tasks") \
        .select("id, title") \
        .eq("is_current", True) \
        .not_.in_("status", ["done", "cancelled"]) \
        .execute()
    active_tasks = tasks_res.data or []
    if not active_tasks:
        return []

    target_lower = target.lower()
    matching = [
        t for t in active_tasks
        if any(word in t["title"].lower() for word in target_lower.split() if len(word) > 3)
    ]
    if not matching:
        return []

    from core.pulse.tools import update_task_status
    closed = []
    for t in matching:
        result_msg = update_task_status(task_id=t["id"], status="done")
        if "Error" not in result_msg:
            closed.append(t["title"])
    return closed

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

        # Unified Calendar events for target day
        try:
            cal_events = await context_provider.get_calendar_events(target)
            for e in cal_events:
                time_str = e.get("time", "")
                title = e.get("title", "")
                is_past = False
                if time_str:
                    try:
                        event_dt = datetime.fromisoformat(time_str)
                        if event_dt < now:
                            is_past = True
                    except Exception:
                        pass
                events_list.append({"time": time_str, "title": title, "is_past": is_past})
        except Exception as cal_err:
            audit_log_sync("webhook", "WARNING", f"Brief calendar query failed: {cal_err}")

        # All active pending tasks (via ContextProvider)
        try:
            compressed_tasks, _ = await context_provider.hydrate_tasks_context(text)
            active_tasks_list = compressed_tasks.split(" | ") if compressed_tasks else []
        except Exception as t_err:
            audit_log_sync("webhook", "WARNING", f"Brief tasks query failed: {t_err}")

        # Overdue tasks
        try:
            now_iso = now.isoformat()
            overdue_res = supabase.table('tasks') \
                .select('title, project_id, organization_id, priority') \
                .eq('is_current', True) \
                .not_.in_('status', ['done', 'cancelled']) \
                .not_.is_('reminder_at', None) \
                .lt('reminder_at', now_iso) \
                .execute()
            if overdue_res.data:
                from core.features import is_org_routing_enabled
                projects = await context_provider.get_projects()
                proj_map = {p['id']: p['name'] for p in projects}
                
                org_map = {}
                if is_org_routing_enabled():
                    orgs = await context_provider.get_organizations()
                    org_map = {o['id']: o['name'] for o in orgs}

                for t in overdue_res.data:
                    pn = proj_map.get(t.get('project_id'), 'INBOX')
                    org_id = t.get('organization_id')
                    o_name = org_map.get(org_id) if org_id else None
                    overdue_tasks.append(_format_task_line(t.get('title', ''), pn, t.get('priority'), organization_name=o_name))
        except Exception as err:
            audit_log_sync("webhook", "WARNING", f"Brief overdue query failed: {err}")

        # Recent completions
        try:
            completed_raw = await context_provider.get_recently_completed_tasks()
            if completed_raw:
                from core.features import is_org_routing_enabled
                projects = await context_provider.get_projects()
                proj_map = {p['id']: p['name'] for p in projects}
                
                org_map = {}
                if is_org_routing_enabled():
                    orgs = await context_provider.get_organizations()
                    org_map = {o['id']: o['name'] for o in orgs}
                
                for t in completed_raw:
                    pn = proj_map.get(t.get('project_id'), 'INBOX')
                    org_id = t.get('organization_id')
                    o_name = org_map.get(org_id) if org_id else None
                    recently_completed.append(_format_task_line(t.get('title', ''), pn, organization_name=o_name))
        except Exception as err:
            audit_log_sync("webhook", "WARNING", f"Brief recent completions failed: {err}")

        def fmt_list(items):
            if not items:
                return "None"
            return "\n".join(f"- {i}" for i in items)

        calendar_text = fmt_list(
            ('[PAST] ' if e.get('is_past') else '') + e['title'] + (' at ' + e['time'][:16].replace('T', ' ')) if e.get('time') else e['title']
            for e in events_list
        ) if events_list else None

        prompt = build_daily_brief_prompt(
            now_str=now.strftime('%A, %d %B %Y, %H:%M %p IST'),
            day_label=day_label.lower(),
            conversation_history=conversation_history,
            calendar_text=calendar_text,
            overdue_text=fmt_list(overdue_tasks),
            todo_text=fmt_list(active_tasks_list),
            recent_done_text=fmt_list(recently_completed)
        )

        response = await generate_content_with_fallback(
            prompt=prompt,
            workload=WorkloadProfile.INTERACTIVE,
            primary_model=CLASSIFICATION_MODEL,
            config={'response_mime_type': 'application/json', 'max_output_tokens': 800}
        )
        try:
            data = response.parse_json()
            reply = data.get("user_facing_summary", "").strip()
        except Exception as e:
            audit_log_sync("webhook", "ERROR", f"Daily brief JSON parse failed: {e}. Failing closed.")
            reply = None  # Will trigger fallback generator

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
        _persist_chain_id(session_id)

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

    return reply

async def handle_confident_task(text: str, title: str, time_context: str, chat_id: int, receipt: str = None, entity: str = None, source: str = "telegram", sender: str = "user", task_update_id: int = None, history_text: str = "", session_id: str = None, extraction_method: str = None) -> str | None:
    """Handle a TASK intent — create or process a task.

    Returns the final response text sent (or None).
    """
    _last_sent = None

    # ── Idempotency guard: skip if identical content+source inserted within 60s ──
    if is_recent_raw_dump(text, source):
        _last_sent = receipt or "Logged."
        await send_telegram(chat_id, f"{_last_sent}")
        return _last_sent

    meta = {
        "intent": "TASK",
        "title": title,
        "time_context": time_context,
        "entity": entity
    }
    if task_update_id is not None:
        meta["task_update_id"] = task_update_id
    if extraction_method is not None:
        meta["extraction_method"] = extraction_method

    dedup_key = hashlib.md5(f"{source}:{text}".encode()).hexdigest()

    dump_id = None
    try:
        dump_res = supabase.table('raw_dumps').insert([{
            "content": text,
            "status": "pending",
            "direction": "incoming",
            "sender": sender,
            "message_type": "task",
            "source": source,
            "metadata": meta,
            "dedup_key": dedup_key
        }]).execute()
        dump_id = dump_res.data[0]['id'] if dump_res.data else None
    except Exception as e:
        audit_log_sync("webhook", "ERROR", f"Failed to save task dump: {e}")
        # dedup_key collision — fetch existing row
        try:
            existing = maybe_single_safe(supabase.table('raw_dumps').select('id').eq('dedup_key', dedup_key))
            dump_id = existing.data.get('id') if existing.data else None
        except Exception as e2:
            audit_log_sync("webhook", "ERROR", f"Failed to fetch existing dump by dedup_key: {e2}")

    _last_sent = receipt or "Logged."

    # Inline: process the dump immediately (fire-and-forget)
    if dump_id:
        try:
            tasks_service = get_tasks_service()
            # Pre-decided path: skip LLM re-extraction for fresh tasks
            if task_update_id:
                result = await process_single_dump(text, meta, tasks_service, history_text)
            else:
                pi = ProcessInput(
                    category="TASK",
                    text=text,
                    source=source,
                    title=title,
                    reminder_at=time_context,
                )
                result = await process_single_dump(text=text, metadata=meta, input=pi, tasks_service=tasks_service)

            if result.get('action') == 'clarify':
                question = result.get('question', "Could you provide more details?")
                _last_sent = f"{question}\n\n_Context: \"{text[:100]}...\"_"
                await send_telegram(chat_id, _last_sent)
                supabase.table('raw_dumps').update({
                    "status": "clarify_needed",
                    "metadata": {**meta, "clarification_question": question}
                }).eq('id', dump_id).execute()
                
            elif result.get('action') in ('created', 'completed', 'filed', 'updated'):
                supabase.table('raw_dumps').update({"status": "synced"}).eq('id', dump_id).execute()
                audit_log_sync("webhook", "INFO", f"Inline processed dump {dump_id}: {result['action']}")
                
                # Send the ack now that processing is done
                _last_sent = f"{_last_sent}"
                await send_telegram(chat_id, _last_sent)
                
                # Check if there is a calendar conflict warning to send
                conflict = result.get('conflict_warning')
                if conflict:
                    await send_telegram(chat_id, f"⚠️ Heads up: this overlaps with '{conflict}' on your calendar.")
                    
        except Exception as e:
            audit_log_sync("webhook", "WARNING", f"Inline processing failed for dump {dump_id}: {e}")
            _last_sent = f"{_last_sent}"
            await send_telegram(chat_id, _last_sent)
    else:
        await send_telegram(chat_id, f"{_last_sent}")

    return _last_sent


async def _enrich_memory_entities(text: str, memory_id: int, active_anchor: dict = None):
    """Shared helper for handle_project_update and handle_confident_note.

    If LLM extraction + deterministic fallback both miss, the active_anchor
    (from the conversation thread) is used as a last-resort anchor so the note
    is linked to whatever entity the user was just discussing.
    """
    chosen_org_id = None
    chosen_proj_id = None
    reason = "no_match"
    
    try:
        # 1. Try LLM extraction first (builds graph edges)
        extracted = await extract_and_link_entities(text, str(memory_id), 'memory')
        org_candidates, proj_candidates = extracted if extracted else ([], [])
        
        if len(proj_candidates) == 1:
            chosen_proj_id = proj_candidates[0]['id']
            if proj_candidates[0].get('org_id'):
                chosen_org_id = proj_candidates[0]['org_id']
                reason = "llm_project_implied_org"
            elif len(org_candidates) == 1:
                chosen_org_id = org_candidates[0]
                reason = "llm_single_match_both"
            else:
                reason = "llm_single_match_project"
        elif len(org_candidates) == 1:
            chosen_org_id = org_candidates[0]
            reason = "llm_single_match_org"
            
        # 2. Fallback to deterministic n-gram resolver if LLM missed
        if not chosen_org_id and not chosen_proj_id:
            res_org, res_proj, res_reason = resolve_entities_from_text(text)
            if res_org or res_proj:
                chosen_org_id = res_org
                chosen_proj_id = res_proj
                reason = f"deterministic_fallback: {res_reason}"
            
        # 3. Last-resort: inherit from active_anchor (conversation context)
        if not chosen_org_id and not chosen_proj_id and active_anchor:
            anchor_org = active_anchor.get('last_org_id')
            anchor_proj = active_anchor.get('last_project_id')
            if anchor_org or anchor_proj:
                chosen_org_id = chosen_org_id or anchor_org
                chosen_proj_id = chosen_proj_id or anchor_proj
                reason = f"anchor_inherit: {active_anchor.get('name', '')}"
                
        # 3. Apply updates if found (with versioning)
        if chosen_org_id or chosen_proj_id:
            update_data = {}
            if chosen_org_id:
                update_data['organization_id'] = chosen_org_id
            if chosen_proj_id:
                update_data['project_id'] = chosen_proj_id
            supabase.table('memories').update(update_data).eq('id', memory_id).execute()
            
        audit_log_sync("webhook", "INFO", f"Memory enrichment {memory_id} | reason={reason} | chosen_org={chosen_org_id} chosen_proj={chosen_proj_id}")
        return chosen_org_id, chosen_proj_id
        
    except Exception as e:
        audit_log_sync("webhook", "WARNING", f"Shared entity enrichment failed for memory {memory_id}: {e}")
        return None, None



def _resolve_calendar_datetime(raw_date: str, raw_time: str, capture_dt: datetime) -> str | None:
    if not raw_date or not raw_time:
        return None
        
    date_lower = raw_date.lower().strip()
    time_lower = raw_time.lower().strip()
    
    time_match = re.search(r'(\d{1,2})(?::(\d{2}))?\s*(am|pm|a|p)?', time_lower)
    if not time_match:
        return None
        
    hour = int(time_match.group(1))
    minute = int(time_match.group(2) or 0)
    meridiem = time_match.group(3)
    
    if meridiem and meridiem.startswith('p') and hour < 12:
        hour += 12
    elif meridiem and meridiem.startswith('a') and hour == 12:
        hour = 0
    elif not meridiem and 1 <= hour <= 6:
        # Implicit PM for small hours 1-6 if no AM/PM (heuristics for scheduling)
        hour += 12
        
    target_time = time(hour, minute)
    
    DAY_NAMES = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
    target_date = None
    
    if date_lower in DAY_NAMES:
        target_day = DAY_NAMES.index(date_lower)
        today = capture_dt.weekday()
        days_ahead = target_day - today
        if days_ahead < 0:
            days_ahead += 7
        elif days_ahead == 0:
            if target_time < capture_dt.time():
                days_ahead += 7
        target_date = capture_dt.date() + timedelta(days=days_ahead)
    elif "today" in date_lower:
        target_date = capture_dt.date()
    elif "tomorrow" in date_lower:
        target_date = capture_dt.date() + timedelta(days=1)
    else:
        from dateutil import parser
        try:
            parsed = parser.parse(date_lower, fuzzy=True, default=capture_dt)
            target_date = parsed.date()
            if target_date < capture_dt.date():
                 target_date = target_date.replace(year=target_date.year + 1)
        except Exception:
            pass
            
    if not target_date:
        return None
        
    resolved = datetime.combine(target_date, target_time, tzinfo=ZoneInfo('Asia/Kolkata'))
    return resolved.isoformat()

async def _run_post_capture_enrichment(
    text: str, chat_id: int, session_id: str,
    chosen_org_id: int | None, chosen_proj_id: int | None,
    receipt: str = None, enable_workflow: bool = True,
    active_anchor: dict = None,
    memory_id: int = None,
) -> str:
    """Post-capture enrichment: ask LLM if the capture implies a task or has a critical ambiguity.

    Returns the follow-up message to append (may be empty). Callers send the receipt themselves.
    Shared by handle_project_update, handle_confident_note, and handle_confident_completion.
    """
    anchor_hint = ""
    if active_anchor:
        anchor_name = active_anchor.get('name', '')
        if anchor_name:
            anchor_hint = f"\nConversation context: the user was recently discussing '{anchor_name}'. Use this to disambiguate references."

    prompt = build_enrichment_prompt(text, anchor_hint)

    analysis_res = await generate_content_with_fallback(
        prompt=prompt,
        workload=WorkloadProfile.INTERACTIVE,
        primary_model=CLASSIFICATION_MODEL,
        config={'response_mime_type': 'application/json'}
    )
    analysis = analysis_res.parse_json()

    signals = analysis.get("signals", [])
    followup_msg = ""
    
    # Backward compat
    if not signals and analysis.get("needs_task") and analysis.get("suggested_task_title"):
        signals.append({"type": "task_imperative", "task_title": analysis["suggested_task_title"], "confidence": 1.0})
    if not signals and analysis.get("needs_question") and analysis.get("suggested_question"):
        # Not perfect map but ensures we don't break old behavior entirely if LLM glitches
        pass

    if not signals:
        return followup_msg

    # Stage 2: Rank
    PRIORITY = ["calendar_event", "deadline", "task_imperative", "task_closure", "person_intro", "financial", "dependency"]
    
    def get_priority(s):
        t = s.get("type", "")
        rank = PRIORITY.index(t) if t in PRIORITY else 99
        conf = s.get("confidence", 0)
        return (rank, -conf)
        
    signals.sort(key=get_priority)
    
    ist_tz = ZoneInfo('Asia/Kolkata')
    
    # Anchor to real capture time to survive delayed processing
    capture_dt = datetime.now(ist_tz)
    if memory_id:
        try:
            from core.services.db import get_supabase
            mem_res = get_supabase().table('memories').select('created_at').eq('id', memory_id).execute()
            if mem_res.data and mem_res.data[0].get('created_at'):
                db_dt = datetime.fromisoformat(mem_res.data[0]['created_at'].replace('Z', '+00:00'))
                capture_dt = db_dt.astimezone(ist_tz)
        except Exception as e:
            audit_log_sync("enrichment", "WARNING", f"Failed to fetch created_at for memory {memory_id}, using now: {e}")

    # Stage 2: Collect ALL actionable signals (no break after first)
    actionable = []
    for s in signals:
        if s.get("confidence", 0) < 0.5:
            continue
        if s.get("type") in ("calendar_event", "deadline"):
            if not s.get("reminder_at"):
                dt_iso = _resolve_calendar_datetime(s.get("raw_date_text", ""), s.get("raw_time_text", ""), capture_dt)
                if dt_iso:
                    s["reminder_at"] = dt_iso
            actionable.append(s)
        elif s.get("type") == "task_imperative":
            actionable.append(s)
        elif s.get("type") == "task_closure":
            actionable.append(s)

    # Stage 3: Promote as batch
    if actionable and enable_workflow:
        from core.services.db import get_supabase
        supabase = get_supabase()
        lines = []
        for i, sig in enumerate(actionable):
            st = sig.get("type")
            title = sig.get("task_title") or sig.get("proposed_title") or sig.get("title") or "Untitled"
            if st == "calendar_event":
                line = f"  {i+1}. 📅 Event: {title}"
                if sig.get("reminder_at"):
                    line += f" — {sig['reminder_at']}"
            elif st == "deadline":
                line = f"  {i+1}. 📅 Deadline: {title}"
            elif st == "task_closure":
                line = f"  {i+1}. ✅ Close: {title}"
            else:
                line = f"  {i+1}. 📝 Task: {title}"
            lines.append(line)
        signals_summary = "\n".join(lines)
        followup_msg = f"\n\n📋 I found these items:\n{signals_summary}\n\nWant me to handle them?"

        try:
            supabase.table('conversation_workflows').insert({
                "chat_id": chat_id,
                "thread_id": session_id,
                "workflow_type": "batch",
                "payload": {"signals": actionable},
                "awaiting_user_input": True,
                "status": "active",
                "expires_at": (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat()
            }).execute()
        except Exception as e:
            audit_log_sync("workflow", "ERROR", f"Failed to create batch workflow: {e}")

    # Stage 4: Store signals in memory metadata
    if memory_id:
        try:
            supabase = get_supabase()
            res = supabase.table('memories').select('metadata').eq('id', memory_id).execute()
            existing_meta = (res.data[0].get('metadata') or {}) if res.data else {}
            new_meta = {
                **existing_meta,
                "signals": signals,
                "signals_version": existing_meta.get("signals_version", 0) + 1,
                "promoted_signal_type": "batch" if actionable else None,
                "promotion_status": "promoted" if actionable else "none",
                "actionable_count": len(actionable)
            }
            upd = {"metadata": new_meta}
            supabase.table('memories').update(upd).eq('id', memory_id).execute()
        except Exception as e:
            audit_log_sync("enrichment", "WARNING", f"Failed to update metadata with signals for memory {memory_id}: {e}")

    audit_log_sync("enrichment", "INFO",
        f"Memory {memory_id}: {len(signals)} signals, {len(actionable)} actionable, skipped={[s['type'] for s in signals if s not in actionable]}")

    return followup_msg


async def handle_project_update(text: str, chat_id: int, receipt: str = None, source: str = "telegram", sender: str = "user", entity: str = None, extraction_method: str = None, session_id: str = None, active_anchor: dict = None) -> str | None:
    """Handle a PROJECT_UPDATE intent — save update to memory.

    Returns the final response text sent (or None).
    """
    _last_sent = None

    # ── Idempotency guard ──
    if is_recent_raw_dump(text, source):
        _last_sent = receipt or "Update logged."
        await send_telegram(chat_id, f"{_last_sent}")
        return _last_sent

    # ── Step 1: Insert as staged ──
    metadata = {"intent": "PROJECT_UPDATE", "entity": entity}
    if extraction_method is not None:
        metadata["extraction_method"] = extraction_method
    dedup_key = hashlib.md5(f"{source}:{text}".encode()).hexdigest()
    insert_data = {
        "content": text,
        "status": "staged",
        "direction": "incoming",
        "sender": sender,
        "message_type": "note",
        "source": source,
        "metadata": metadata,
        "dedup_key": dedup_key
    }
    dump_id = None
    try:
        dump_res = supabase.table('raw_dumps').insert([insert_data]).execute()
        dump_id = dump_res.data[0]['id'] if dump_res.data else None
    except Exception as e:
        audit_log_sync("webhook", "ERROR", f"Failed to save update dump: {e}")
        try:
            existing = maybe_single_safe(supabase.table('raw_dumps').select('id').eq('dedup_key', dedup_key))
            dump_id = existing.data.get('id') if existing.data else None
        except Exception as dedup_err:
            audit_log_sync("webhook", "WARNING", f"Dedup lookup failed for {dedup_key}: {dedup_err}")

    # ── Step 2: Shared pipeline — embedding, memory, entities, index ──
    pi = ProcessInput(category="NOTE", text=text, source=source)
    meta = {"intent": "PROJECT_UPDATE"}
    if entity:
        meta["entity"] = entity
    if extraction_method:
        meta["extraction_method"] = extraction_method
    result = await process_single_dump(text=text, metadata=meta, input=pi)

    if result.get("action") == "error":
        if dump_id:
            supabase.table('raw_dumps').update({"status": "embedding_failed"}).eq('id', dump_id).execute()
        _last_sent = receipt or "✅ Captured. Memory indexing will retry shortly."
        await send_telegram(chat_id, f"{_last_sent}")
        return _last_sent

    memory_id = result.get("memory_id")

    # ── Step 3: Enrich memory with org/project from conversation anchor ──
    if memory_id:
        accumulate_action(ActionResult(action_type="memory_save", status="executed", entity_id=memory_id, human_label="Update logged"))
        from core.lib.audit_logger import log_audit
        try:
            chosen_org_id, chosen_proj_id = await _enrich_memory_entities(text, memory_id, active_anchor)
        except Exception as e:
            log_audit("handle_project_update", "error", f"Memory enrichment failed: {e}", memory_id=memory_id)
            chosen_org_id = chosen_proj_id = None
    else:
        chosen_org_id = chosen_proj_id = None

    # ── Step 4: Post-capture enrichment (shared helper) ──
    try:
        followup_msg = await _run_post_capture_enrichment(
            text, chat_id, session_id,
            chosen_org_id, chosen_proj_id,
            receipt=receipt, enable_workflow=True,
            active_anchor=active_anchor,
            memory_id=memory_id,
        )
        ack = receipt or "✅ Update logged and entities extracted."
        _last_sent = f"{ack}{followup_msg}"
        await send_telegram(chat_id, _last_sent)
        if session_id:
            log_exchange(session_id, 'bot', 'PROJECT_UPDATE', _last_sent, chat_id)
    except Exception as e:
        audit_log_sync("webhook", "WARNING", f"Update enrichment failed: {e}")
        _last_sent = receipt or "✅ Update logged."
        await send_telegram(chat_id, f"{_last_sent}")
        if session_id:
            log_exchange(session_id, 'bot', 'PROJECT_UPDATE', f"{_last_sent}", chat_id)

    # ── Step 5: Mark dump as processed ──
    if dump_id:
        try:
            supabase.table('raw_dumps').update({"status": "processed", "is_processed": True}).eq('id', dump_id).execute()
        except Exception as e:
            audit_log_sync("webhook", "WARNING", f"Failed to mark dump {dump_id} as processed: {e}")

    return _last_sent


async def handle_confident_note(text: str, chat_id: int, receipt: str = None, source: str = "telegram", sender: str = "user", entity: str = None, extraction_method: str = None, session_id: str = None, active_anchor: dict = None) -> str | None:
    """Handle a NOTE intent — save note to memory.

    Returns the final response text sent (or None).
    """
    _last_sent = None

    # ── Idempotency guard: skip if identical content+source inserted within 60s ──
    if is_recent_raw_dump(text, source):
        _last_sent = receipt or "Note vaulted."
        await send_telegram(chat_id, f"{_last_sent}")
        return _last_sent

    # ── Step 1: Insert as staged for audit trail + idempotency ──
    metadata = {"intent": "NOTE", "entity": entity}
    if extraction_method is not None:
        metadata["extraction_method"] = extraction_method
    dedup_key = hashlib.md5(f"{source}:{text}".encode()).hexdigest()
    insert_data = {
        "content": text,
        "status": "staged",
        "direction": "incoming",
        "sender": sender,
        "message_type": "note",
        "source": source,
        "metadata": metadata,
        "dedup_key": dedup_key
    }
    try:
        dump_res = supabase.table('raw_dumps').insert([insert_data]).execute()
        dump_id = dump_res.data[0]['id'] if dump_res.data else None
    except Exception as e:
        audit_log_sync("webhook", "ERROR", f"Failed to save note dump: {e}")
        try:
            existing = maybe_single_safe(supabase.table('raw_dumps').select('id').eq('dedup_key', dedup_key))
            dump_id = existing.data.get('id') if existing.data else None
        except Exception as e2:
            audit_log_sync("webhook", "ERROR", f"Failed to fetch existing dump by dedup_key: {e2}")
            dump_id = None

    # ── Step 2: Shared pipeline — embedding, memory insert, entity extraction, index ──
    pi = ProcessInput(category="NOTE", text=text, source=source)
    meta = {"intent": "NOTE"}
    if entity:
        meta["entity"] = entity
    if extraction_method:
        meta["extraction_method"] = extraction_method
    result = await process_single_dump(text=text, metadata=meta, input=pi)

    if result.get("action") == "error":
        if dump_id:
            try:
                supabase.table('raw_dumps').update({"status": "embedding_failed"}).eq('id', dump_id).execute()
            except Exception:
                pass
        try:
            from core.lib.audit_logger import write_dlq
            write_dlq('raw_dumps', str(dump_id) if dump_id else None, text, f"Shared pipeline failed: {result.get('reason')}")
        except Exception:
            pass
        _last_sent = receipt or "✅ Captured. Memory indexing will retry shortly."
        await send_telegram(chat_id, f"{_last_sent}")
        return _last_sent

    memory_id = result.get("memory_id")
    # ── Step 3: Enrich memory with org/project from conversation anchor ──
    if memory_id:
        accumulate_action(ActionResult(action_type="memory_save", status="executed", entity_id=memory_id, human_label="Note vaulted"))
        from core.lib.audit_logger import log_audit
        try:
            chosen_org_id, chosen_proj_id = await _enrich_memory_entities(text, memory_id, active_anchor)
        except Exception as e:
            log_audit("handle_confident_note", "error", f"Memory enrichment failed: {e}", memory_id=memory_id)
            chosen_org_id = chosen_proj_id = None
    else:
        chosen_org_id = chosen_proj_id = None

    # ── Step 3b: If note contains a URL, also vault as resource ──
    match = re.search(r'https?://\S+', text)
    if match:
        actual_url = match.group(0).rstrip('.,;:!?)"\'')
        try:
            existing = supabase.table('resources').select('id, dismissed_at').eq('url', actual_url).eq('is_current', True).limit(1).execute()
            if not existing.data:
                supabase.table('resources').insert({"url": actual_url}).execute()
            elif existing.data[0].get('dismissed_at'):
                await send_telegram(chat_id, "Already seen this link and dismissed it. Skipping.")
        except Exception as e:
            audit_log_sync("webhook", "WARNING", f"Resource insert failed for URL: {e}")

    # ── Step 4: Post-capture enrichment (selective for notes) ──
    _note_words = text.split()
    _is_substantial = len(_note_words) > 25 or (len(_note_words) > 10 and (chosen_org_id or chosen_proj_id))
    followup_msg = ""
    if _is_substantial and memory_id:
        try:
            followup_msg = await _run_post_capture_enrichment(
                text, chat_id, session_id,
                chosen_org_id, chosen_proj_id,
                receipt=receipt, enable_workflow=True,
                active_anchor=active_anchor,
                memory_id=memory_id,
            )
        except Exception as e:
            audit_log_sync("webhook", "WARNING", f"Note enrichment failed: {e}")

    # ── Step 5: Mark dump as processed ──
    if dump_id:
        try:
            supabase.table('raw_dumps').update({"status": "processed", "is_processed": True}).eq('id', dump_id).execute()
        except Exception as e:
            audit_log_sync("webhook", "WARNING", f"Failed to mark dump {dump_id} as processed: {e}")

    final_receipt = receipt or "Note vaulted."
    if followup_msg:
        _last_sent = f"{final_receipt}{followup_msg}"
        await send_telegram(chat_id, _last_sent)
    else:
        _last_sent = final_receipt
        await send_telegram(chat_id, final_receipt)
    if session_id:
        log_exchange(session_id, 'bot', 'NOTE', final_receipt, chat_id)

    return _last_sent

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

async def ask_intent_disambiguation(text: str, possible_intents: list, chat_id: int, session_id: str, deliberation: dict = None):
    keyboard = []
    for sc, (intent, label) in INTENT_OPTIONS.items():
        if intent in possible_intents:
            keyboard.append([{"text": label, "callback_data": sc}])
    if not keyboard:
        return
    # Surface deliberation scores when available
    score_lines = []
    if deliberation and deliberation.get("candidates"):
        for c in deliberation["candidates"]:
            fmt = f"· {c['intent']}: *{c['score']:.0%}* — {c.get('reasoning', '')}"
            if len(fmt) > 300:
                fmt = fmt[:300] + "…"
            score_lines.append(fmt)
    score_section = "\n".join(score_lines) if score_lines else ""
    reply = "🧐 *Not sure what to do with this.*"
    if score_section:
        reply += f"\n\nBased on what I know:\n{score_section}\n\n_—pick one:_"
    else:
        reply += " Is it?"
    log_exchange(session_id, 'bot', 'CLARIFICATION', json.dumps({
        "possible_intents": possible_intents, "original": text,
        "deliberation": deliberation,
    }), chat_id)
    await send_telegram(chat_id, reply, show_keyboard=False, inline_keyboard=keyboard)

FILLER_WORDS = {'the', 'a', 'an', 'it', 'is', 'was', 'this', 'that', 'to', 'for',
                 'of', 'in', 'on', 'at', 'by', 'with', 'or', 'and', 'but', 'not',
                 'its', 'about', 'just', 'please', 'thanks', 'make', 'do', 'my', 'me'}


async def resolve_disambiguation(text: str, chat_id: int, session_id: str, last_clarification: dict) -> bool:
    cleaned = text.strip().lower()
    if cleaned in INTENT_BY_KEYWORD:
        intent = INTENT_BY_KEYWORD[cleaned]
    elif cleaned in [v[0].lower() for v in INTENT_OPTIONS.values() if v[0].lower() != cleaned]:
        intent = next(v[0] for v in INTENT_OPTIONS.values() if v[0].lower() == cleaned)
    else:
        words = [w for w in cleaned.split() if len(w) > 2 and w not in FILLER_WORDS]
        intent_names = {v[0].lower(): v[0] for v in INTENT_OPTIONS.values()}
        intent = None
        for w in words:
            if w in intent_names:
                intent = intent_names[w]
                break
        if not intent:
            return False
    original = last_clarification.get("original", text)
    # --- #10 Feedback Loop: Track disambiguation override ---
    prev_intent = last_clarification.get("classification", {}).get("intent", "UNKNOWN")
    if prev_intent != "UNKNOWN" and intent and prev_intent != intent:
        audit_log_sync("webhook", "INFO",
            f"FEEDBACK_OVERRIDE: user corrected '{prev_intent}' → '{intent}' | text='{original[:80]}'")
    log_exchange(session_id, 'user', intent, text, chat_id)
    classification = {"title": original, "intent": intent}
    await route_by_intent(intent, original, chat_id, session_id, classification=classification)
    return True

async def ask_task_or_note_confirmation(text: str, classification: dict, chat_id: int, session_id: str):
    reply = f"🧐 *Is this a task or a note?*\n\n_{text[:200]}..._"
    keyboard = [
        [{"text": "📋 Task", "callback_data": "t"}, {"text": "📝 Note", "callback_data": "n"}]
    ]
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
    await send_telegram(chat_id, reply, show_keyboard=False, inline_keyboard=keyboard)

async def resolve_task_note_confirmation(text: str, chat_id: int, session_id: str, last_clarification: dict) -> bool:
    cleaned = text.strip().lower()
    if cleaned in ('t', 'task'):
        intent = 'TASK'
    elif cleaned in ('n', 'note'):
        intent = 'NOTE'
    elif 'task' in cleaned:
        intent = 'TASK'
    elif 'note' in cleaned:
        intent = 'NOTE'
    else:
        return False
    original = last_clarification.get("original", text)
    classification = last_clarification.get("classification", {"title": original})
    # --- #10 Feedback Loop: Track classification override ---
    prev_intent = classification.get('intent', 'UNKNOWN')
    if prev_intent != 'UNKNOWN' and prev_intent != intent:
        audit_log_sync("webhook", "INFO",
            f"FEEDBACK_OVERRIDE: user corrected '{prev_intent}' → '{intent}' | text='{original[:80]}'")
    classification["intent"] = intent
    log_exchange(session_id, 'user', intent, text, chat_id)
    await route_by_intent(intent, original, chat_id, session_id, classification=classification)
    return True

async def handle_role_update(text: str, chat_id: int, classification: dict, source="telegram"):
    """Handle ROLE_UPDATE intent — update people.role from a role attribution."""
    person_name = classification.get('person_name', '').strip()
    role_title = classification.get('role_title', '').strip()
    org_name = classification.get('org_name', '').strip()

    if not person_name:
        await send_telegram(chat_id, "I couldn't identify who to update. Please specify the person's name.")
        return
    if not role_title:
        await send_telegram(chat_id, "I need to know what role to set. Please specify the role.")
        return

    # Resolve person in people table
    try:
        res = supabase.table('people').select('id, name, role').ilike('name', f'%{person_name}%').eq('is_current', True).limit(1).execute()
        if res and res.data:
            person = res.data[0]
            person_id = person['id']
        else:
            # Check graph nodes for the person label
            gn = supabase.table('graph_nodes').select('id, label').eq('type', 'person').ilike('label', f'%{person_name}%').eq('is_current', True).limit(1).execute()
            if gn and gn.data:
                new_people = supabase.table('people').insert({
                    'name': gn.data[0]['label'],
                    'role': f"{role_title} of {org_name}" if org_name else role_title,
                    'organization_name': org_name or None,
                    'source': 'role_update'
                }).execute()
                person_id = new_people.data[0]['id']
                await send_telegram(chat_id, f"👤 Created people entry for {gn.data[0]['label']} with role: {role_title}" + (f" at {org_name}." if org_name else "."))
            else:
                await send_telegram(chat_id, f"I don't recognize '{person_name}' in the system. Please add them first.")
                return

        update_data = {}
        if org_name:
            update_data['organization_name'] = org_name
        new_role = f"{role_title} of {org_name}" if org_name else role_title
        if person.get('role') and new_role not in person['role']:
            update_data['role'] = f"{person['role']}; {new_role}"
        elif not person.get('role'):
            update_data['role'] = new_role
        if update_data:
            supabase.table('people').update(update_data).eq('id', person_id).execute()

        msg = f"✅ Role updated: {person_name} → {role_title}" + (f" at {org_name}." if org_name else ".")
        await send_telegram(chat_id, msg)
    except Exception as e:
        audit_log_sync("webhook", "ERROR", f"Role update failed: {e}")
        await send_telegram(chat_id, "I encountered an error updating the role. Please try again.")


async def route_by_intent(intent: str, text: str, chat_id: int, session_id: str, classification: dict = None, source="telegram", sender="user", task_update_id: int = None, active_anchor: dict = None):
    # Generate or retrieve decision_chain_id for this request
    cid = get_decision_chain_id()
    if not cid:
        cid = set_decision_chain_id()

    history_text = ""
    if session_id:
        pairs = get_history(session_id)
        history_text = format_history_for_prompt(pairs)

    handler_map = {
        'TASK': 'handle_confident_task',
        'DAILY_BRIEF': 'handle_daily_brief',
        'QUERY': 'interrogate_brain',
        'COMPLETION': 'handle_confident_completion',
        'NOTE': 'handle_confident_note',
        'PROJECT_UPDATE': 'handle_project_update',
        'DELEGATE': 'handle_delegate',
        'DECLARE_PRACTICE': 'handle_declare_practice',
        'ROLE_UPDATE': 'handle_role_update',
        'NOISE': 'handle_noise',
    }
    handler_name = handler_map.get(intent, 'handle_clarification')
    confidence = classification.get('confidence', 0) if classification else 0
    await log_decision(
        stage=DecisionStage.ROUTING,
        query_text=text,
        resolved_entities=[classification.get('entity', '')] if classification and classification.get('entity') else [],
        reason_codes=[],
        summary=f"Routing {intent} ({confidence:.0%}) → {handler_name}"
    )

    if intent == 'TASK':
        title = classification.get('title', text) if classification else text
        receipt = classification.get('receipt') if classification else None
        entity = classification.get('entity') if classification else None
        time_context = classification.get('time_context', '') if classification else ''
        task_update_id = task_update_id if task_update_id is not None else (classification.get('task_update_id') if classification else None)
        extraction_method = classification.get('extraction_method') if classification else None
        reply = await handle_confident_task(text, title, time_context, chat_id, receipt, entity=entity, source=source, sender=sender, task_update_id=task_update_id, history_text=history_text, session_id=session_id, extraction_method=extraction_method)
        if reply:
            capture_response(reply)
    elif intent == 'DAILY_BRIEF':
        reply = await handle_daily_brief(text, chat_id, session_id=session_id, conversation_history=history_text)
        if reply:
            capture_response(reply)
    elif intent == 'QUERY':
        reply = await interrogate_brain(text, chat_id, session_id=session_id, conversation_history=history_text, active_anchor=active_anchor)
        if reply:
            capture_response(reply)
    elif intent == 'COMPLETION':
        from core.webhook.completion_handler import handle_confident_completion
        receipt = classification.get('receipt') if classification else None
        entity = classification.get('entity') if classification else None
        await handle_confident_completion(
            text=text,
            title=classification.get("title", text) if classification else text,
            chat_id=chat_id,
            receipt=receipt,
            entity=entity,
            source=source,
            sender=sender
        )
    elif intent == 'NOTE':
        receipt = classification.get('receipt') if classification else None
        entity = classification.get('entity') if classification else None
        extraction_method = classification.get('extraction_method') if classification else None
        reply = await handle_confident_note(text, chat_id, receipt or "Note secured.", source=source, sender=sender, entity=entity, extraction_method=extraction_method, session_id=session_id, active_anchor=active_anchor)
        if reply:
            capture_response(reply)
    elif intent == 'PROJECT_UPDATE':
        receipt = classification.get('receipt') if classification else None
        entity = classification.get('entity') if classification else None
        extraction_method = classification.get('extraction_method') if classification else None
        reply = await handle_project_update(text, chat_id, receipt or "Update logged.", source=source, sender=sender, entity=entity, extraction_method=extraction_method, session_id=session_id, active_anchor=active_anchor)
        if reply:
            capture_response(reply)
    elif intent == 'DELEGATE':
        supabase.table('agent_queue').insert({"query": text, "status": "pending"}).execute()
        ack = classification.get('receipt', "The intern is on it. I'll ping you when the research is ready.") if classification else "The intern is on it. I'll ping you when the research is ready."
        await send_telegram(chat_id, f"✓ {ack}")
    elif intent == 'DECLARE_PRACTICE':
        await handle_declare_practice(text, chat_id, classification or {})
    elif intent == 'ROLE_UPDATE':
        await handle_role_update(text, chat_id, classification or {}, source=source)
    elif intent == 'NOISE':
        await handle_noise(chat_id)
    else:
        await handle_clarification(text, "Could you provide more details?", chat_id, session_id=session_id)

    # Process secondary_actions after primary handler completes
    if classification:
        secondary = classification.get("secondary_actions", [])
        for sa in secondary:
            sa_type = sa.get("type")
            sa_target = sa.get("target", "")
            sa_conf = sa.get("confidence", 0)
            if sa_conf < 0.5 or not sa_target:
                continue
            if sa_type == "task_closure":
                closed = await _process_task_closure(sa_target, chat_id)
                if closed:
                    await send_telegram(chat_id, f"✅ Closed: {', '.join(closed)}")
                else:
                    audit_log_sync("secondary_actions", "INFO", f"No tasks matched for closure: {sa_target}")

    _persist_chain_id(session_id)

_searching_locks = set()

async def delayed_searching_msg(chat_id: int):
    try:
        await asyncio.sleep(0.8)
        if chat_id in _searching_locks:
            await send_telegram(chat_id, "🧠 *Searching your vault...*")
    except asyncio.CancelledError:
        pass

def resolve_dates_from_query(query: str):
    """Resolve date references in a query to start and end dates.
    Returns (start_dt, end_dt) or (None, None) if unparsable."""
    low = query.lower()
    now = datetime.now(timezone(timedelta(hours=5, minutes=30)))
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    
    if 'this week' in low:
        start = today - timedelta(days=today.weekday())
        end = start + timedelta(days=6, hours=23, minutes=59, seconds=59)
        return start, end
    elif 'next week' in low:
        start = today + timedelta(days=7 - today.weekday())
        end = start + timedelta(days=6, hours=23, minutes=59, seconds=59)
        return start, end
    elif 'tomorrow' in low:
        start = today + timedelta(days=1)
        end = start + timedelta(hours=23, minutes=59, seconds=59)
        return start, end
    elif 'yesterday' in low:
        start = today - timedelta(days=1)
        end = start + timedelta(hours=23, minutes=59, seconds=59)
        return start, end
    elif 'today' in low or 'tonight' in low or 'this morning' in low or 'this afternoon' in low:
        return today, today + timedelta(hours=23, minutes=59, seconds=59)
        
    day_map = {'monday': 0, 'tuesday': 1, 'wednesday': 2, 'thursday': 3, 'friday': 4, 'saturday': 5, 'sunday': 6}
    for name, idx in day_map.items():
        if name in low:
            days_ahead = idx - today.weekday()
            if days_ahead < 0:
                days_ahead += 7
            elif days_ahead == 0 and f"next {name}" in low:
                days_ahead = 7
            start = today + timedelta(days=days_ahead)
            end = start + timedelta(hours=23, minutes=59, seconds=59)
            return start, end
            
    return None, None

async def safe_fetch(coro, default=None):
    try:
        res = await coro
        return res if res is not None else default
    except Exception as e:
        audit_log_sync("webhook", "WARNING", f"Safe fetch failed: {e}")
        return default

def _build_rich_anchor(graph_node_id, name):
    """Build a structured active_anchor with entity type, last task/project, and context snippet."""
    now_ist = datetime.now(timezone(timedelta(hours=5, minutes=30)))
    anchor = {
        "id": str(graph_node_id) if graph_node_id else None,
        "name": name,
        "type": "entity",
        "last_action": None,
        "last_task_id": None,
        "last_project_id": None,
        "last_org_id": None,
        "last_summary_snippet": None,
        "last_mentioned_at": now_ist.isoformat()
    }
    try:
        node_res = supabase.table('graph_nodes').select('type').eq('id', graph_node_id).execute()
        if node_res.data:
            anchor["type"] = node_res.data[0].get('type', 'entity')
    except Exception:
        pass
    try:
        task_res = supabase.table('tasks') \
            .select('id, title, project_id, organization_id, status') \
            .eq('is_current', True) \
            .neq('status', 'done') \
            .neq('status', 'cancelled') \
            .or_(f"title.ilike.%{name}%,metadata->>entity.ilike.%{name}%") \
            .order('created_at', desc=True) \
            .limit(1) \
            .execute()
        if task_res.data:
            t = task_res.data[0]
            anchor["last_task_id"] = str(t['id'])
            anchor["last_action"] = t.get('status', '')
            if t.get('project_id'):
                anchor["last_project_id"] = str(t['project_id'])
            if t.get('organization_id'):
                anchor["last_org_id"] = str(t['organization_id'])
    except Exception:
        pass
    try:
        mem_res = supabase.table('memories') \
            .select('content') \
            .eq('is_current', True) \
            .ilike('content', f'%{name}%') \
            .order('created_at', desc=True) \
            .limit(1) \
            .execute()
        if mem_res.data:
            anchor["last_summary_snippet"] = mem_res.data[0].get('content', '')[:200]
    except Exception:
        pass
    return anchor

async def interrogate_brain(query: str, chat_id: int, session_id: str = None, conversation_history: str = "", active_anchor: dict = None) -> str | None:
    """On-Demand Brain Interrogation - Universal Question Answering.

    Returns the final reply text that was sent (or None if nothing was sent).
    """
    search_task = None
    _last_reply = None
    if chat_id not in _searching_locks:
        _searching_locks.add(chat_id)
        search_task = asyncio.create_task(delayed_searching_msg(chat_id))
        
    try:
        # Anaphora & Entity Resolution
        resolved_entity = None
        try:
            anchor_context = ""
            if active_anchor:
                parts = [f"Active context: {active_anchor.get('name', '')}"]
                if active_anchor.get('type'):
                    parts.append(f"Type: {active_anchor['type']}")
                if active_anchor.get('last_action'):
                    parts.append(f"Last activity: {active_anchor['last_action']}")
                if active_anchor.get('last_summary_snippet'):
                    parts.append(f"Recent context: {active_anchor['last_summary_snippet'][:200]}")
                anchor_context = "\n".join(parts)
            # Load thread summary for broader conversational context
            thread_summary = ""
            if session_id:
                thread_summary = get_thread_summary(session_id)
            if thread_summary:
                anchor_context += f"\nEarlier in conversation: {thread_summary[:500]}"
            resolve_prompt = build_anaphora_resolution_prompt(anchor_context, conversation_history, query)

            resolve_response = await generate_content_with_fallback(
                prompt=resolve_prompt,
                workload=WorkloadProfile.INTERACTIVE,
                primary_model=CLASSIFICATION_MODEL,
                config={'response_mime_type': 'application/json'}
            )
            if resolve_response and resolve_response.text:
                import json
                try:
                    data = json.loads(resolve_response.text.strip())
                    resolved_query = data.get("resolved_query", "").strip()
                    if resolved_query and resolved_query.lower() != query.lower() and resolved_query.lower() != "none":
                        query = resolved_query
                    ent = data.get("primary_entity", "").strip()
                    if ent and ent.lower() != "none":
                        resolved_entity = ent
                except json.JSONDecodeError:
                    pass
        except Exception as e:
            audit_log_sync("webhook", "WARNING", f"Anaphora/Entity resolution failed: {e}")

        # If we found a new entity, try to resolve it to a graph node to set as anchor
        if resolved_entity:
            try:
                # Fast search for exact or partial match in graph_nodes
                node_res = supabase.table('graph_nodes').select('id, label').ilike('label', f'%{resolved_entity}%').eq('is_current', True).execute()
                if node_res.data:
                    matches = node_res.data
                    # Tiebreaker 1: exact match (case-insensitive)
                    exact = [n for n in matches if n['label'].lower() == resolved_entity.lower()]
                    if exact:
                        chosen = exact[0]
                    else:
                        # Tiebreaker 2: highest edge count
                        nids = [n['id'] for n in matches]
                        source_edges = supabase.table('graph_edges').select('source_node_id, target_node_id').in_('source_node_id', nids).eq('is_current', True).execute()
                        target_edges = supabase.table('graph_edges').select('source_node_id, target_node_id').in_('target_node_id', nids).eq('is_current', True).execute()
                        all_edge_data = (source_edges.data or []) + (target_edges.data or [])
                        ec = {}
                        if all_edge_data:
                            for e in all_edge_data:
                                for nid in nids:
                                    if e['source_node_id'] == nid or e['target_node_id'] == nid:
                                        ec[nid] = ec.get(nid, 0) + 1
                        chosen = max(matches, key=lambda n: ec.get(n['id'], 0))
                    
                    active_anchor = _build_rich_anchor(chosen['id'], chosen['label'])
                    
                    # Persist active_anchor to thread record for follow-up queries
                    if session_id:
                        try:
                            supabase.table('conversation_threads').update({
                                'active_anchor': active_anchor
                            }).eq('id', session_id).execute()
                        except Exception as persist_e:
                            audit_log_sync("webhook", "WARNING", f"Failed to persist active_anchor: {persist_e}")
            except Exception as e:
                audit_log_sync("webhook", "WARNING", f"Anchor node lookup failed: {e}")

        # If we have an active_anchor, we can scope our hybrid_search_graph or other context
        # For hybrid_search_graph, we can use the anchor's name instead of the query if we have it
        search_term = active_anchor["name"] if active_anchor else query

        start_dt, end_dt = resolve_dates_from_query(query)
        
        # Source Selection Heuristics (Tier 4d)
        lq = query.lower()
        is_schedule = any(w in lq for w in ['calendar', 'schedule', 'meeting', 'meet', 'today', 'tomorrow', 'week', 'when'])
        is_comms = any(w in lq for w in ['email', 'message', 'said', 'told', 'chat', 'whatsapp', 'contact'])
        is_action = any(w in lq for w in ['task', 'todo', 'block', 'status', 'progress', 'done', 'completed'])
        
        # Default to everything if no specific type is detected, otherwise filter
        fetch_all = not (is_schedule or is_comms or is_action)

        async def _empty_fetch(val): return val

        # Parallel fetch context
        tactical_map_task = safe_fetch(hybrid_search_graph(search_term, active_anchor["id"] if active_anchor else None), "")
        tasks_task = safe_fetch(context_provider.hydrate_tasks_context(query), ("", "")) if (fetch_all or is_action or is_schedule) else safe_fetch(_empty_fetch(("", "")), ("", ""))
        memories_task = safe_fetch(context_provider.hydrate_memories_context(query), "None") if (fetch_all or not is_schedule) else safe_fetch(_empty_fetch("None"), "None")
        resources_task = safe_fetch(context_provider.get_resources_context(query), "None") if (fetch_all or not is_schedule) else safe_fetch(_empty_fetch("None"), "None")
        practices_task = safe_fetch(context_provider.get_practices_context(), "None") if (fetch_all or not is_schedule) else safe_fetch(_empty_fetch("None"), "None")
        emails_task = safe_fetch(context_provider.get_email_context(query), "None") if (fetch_all or is_comms) else safe_fetch(_empty_fetch("None"), "None")
        whatsapp_task = safe_fetch(context_provider.get_whatsapp_context(query), "None") if (fetch_all or is_comms) else safe_fetch(_empty_fetch("None"), "None")
        pending_decisions_task = safe_fetch(context_provider.get_pending_decisions_context(), "None")
        
        # People context
        async def fetch_people():
            people = await context_provider.get_people()
            if not people:
                return "None"
            return ", ".join([p.get("name", "") for p in people if p.get("name")])
        people_task = safe_fetch(fetch_people(), "None") if (fetch_all or is_comms or is_schedule) else safe_fetch(_empty_fetch("None"), "None")
        
        # Completed tasks context
        async def fetch_completed():
            completed = await context_provider.get_recently_completed_tasks()
            if not completed:
                return "None"
            return "\n".join([f"- {t.get('title', '')}" for t in completed])
        completed_task = safe_fetch(fetch_completed(), "None") if (fetch_all or is_action) else safe_fetch(_empty_fetch("None"), "None")
        
        # Calendar context
        async def fetch_calendar():
            if start_dt is None or end_dt is None:
                return "None"
            events = await context_provider.get_range_calendar_events(start_dt, end_dt)
            if not events:
                return "None"
            
            now_local = datetime.now(timezone(timedelta(hours=5, minutes=30)))
            lines = []
            for e in events:
                time_str = e.get("time", "")
                t_display = time_str[:16].replace("T", " ") if time_str else ""
                
                if time_str:
                    try:
                        event_dt = datetime.fromisoformat(time_str)
                        if event_dt < now_local:
                            lines.append(f"- [PAST] {t_display} {e.get('title', '')} ({e.get('source', '')})")
                            continue
                    except Exception:
                        pass
                
                lines.append(f"- {t_display} {e.get('title', '')} ({e.get('source', '')})")
            return "\n".join(lines)
        calendar_task = safe_fetch(fetch_calendar(), "None") if (fetch_all or is_schedule or start_dt is not None) else safe_fetch(_empty_fetch("None"), "None")

        # Temporal, Serendipity, Hindsight Contexts (Tier 3)
        async def fetch_temporal():
            from core.pulse.memory import detect_temporal_patterns
            return await detect_temporal_patterns()
        temporal_task = safe_fetch(fetch_temporal(), "None") if (fetch_all) else safe_fetch(_empty_fetch("None"), "None")

        async def fetch_serendipity():
            from core.pulse.memory import serendipity_engine
            tasks = await context_provider.get_active_tasks()
            people = await context_provider.get_people()
            return await serendipity_engine(tasks, people, [], max_paths=5)
        serendipity_task = safe_fetch(fetch_serendipity(), "None") if (fetch_all or is_action) else safe_fetch(_empty_fetch("None"), "None")

        async def fetch_hindsight():
            from core.pulse.memory import retrieve_hindsight_memories
            tasks = await context_provider.get_active_tasks()
            memories_raw, _ = await retrieve_hindsight_memories([query], tasks, top_k=5)
            if memories_raw:
                cleaned = [m.replace("[MEMORY CONTEXT ONLY — DO NOT LIST IN BRIEFING] ", "") for m in memories_raw]
                return "\n".join(cleaned)
            return "None"
        hindsight_task = safe_fetch(fetch_hindsight(), "None") if (fetch_all or is_action) else safe_fetch(_empty_fetch("None"), "None")

        async def fetch_raw_comms():
            if not active_anchor and not resolved_entity:
                return "None"
            search_val = (active_anchor["name"] if active_anchor else resolved_entity).lower().replace(',', ' ')
            if not search_val or search_val == "none" or len(search_val) < 3:
                return "None"
                
            lines = []
            try:
                now_iso = datetime.now(timezone.utc).isoformat()
                # Get both incoming and outgoing emails, searching subject, body, and sender_id
                e_res = supabase.table('messages').select('id, subject, sender_name, sender_id, body, received_at, processing_status, direction, metadata') \
                    .eq('channel', 'email') \
                    .or_(f"subject.ilike.%{search_val}%,body.ilike.%{search_val}%,sender_id.ilike.%{search_val}%") \
                    .in_('processing_status', ['pending', 'completed']) \
                    .or_(f"expires_at.is.null,expires_at.gte.{now_iso}") \
                    .order('received_at', desc=True).limit(8).execute()
                
                found_sent = False
                if e_res.data:
                    for e in e_res.data:
                        direction = e.get('direction', 'incoming')
                        if direction == 'outgoing':
                            found_sent = True
                            preview = (e.get('body') or '').replace('\n', ' ')[:150]
                            lines.append(f"{age_tag(e.get('received_at'))} - [YOUR REPLY] Re: {e.get('subject', '')}: \"{preview}\"... (to {e.get('sender_id', '')})")
                        else:
                            lines.append(f"{age_tag(e.get('received_at'))} - [EMAIL] {e.get('subject', '')} (from {e.get('sender_name') or e.get('sender_id', '')}, status: {e.get('processing_status', '')})")
                            
                # Fallback to API if we didn't find any sent replies
                if not found_sent:
                    try:
                        from core.email_search import search_gmail_sent, search_outlook_sent
                        import asyncio
                        g_task = asyncio.to_thread(search_gmail_sent, search_val, 2)
                        o_task = asyncio.to_thread(search_outlook_sent, search_val, 2)
                        g_res, o_res = await asyncio.gather(g_task, o_task)
                        for msg in g_res + o_res:
                            preview = (msg.get('body_summary') or '').replace('\n', ' ')[:150]
                            lines.append(f"- [YOUR REPLY] Re: {msg.get('subject', '')}: \"{preview}\"... (to {msg.get('sender_email', '')}) - REALTIME API")
                    except Exception as e:
                        audit_log_sync("webhook", "WARNING", f"Fallback realtime sent fetch failed: {e}")
            except Exception:
                pass
                
            try:
                now_iso = datetime.now(timezone.utc).isoformat()
                w_res = supabase.table('messages').select('id, sender_name, body, received_at').eq('channel', 'whatsapp').ilike('body', f"%{search_val}%").or_(f"expires_at.is.null,expires_at.gte.{now_iso}").order('received_at', desc=True).limit(3).execute()
                if w_res.data:
                    for w in w_res.data:
                        text = w.get('body', '').replace('\n', ' ')[:100]
                        lines.append(f"{age_tag(w.get('received_at'))} - [WHATSAPP] {text}... (from {w.get('sender_name', '')})")
            except Exception:
                pass
                
            try:
                c_res = supabase.table('call_recordings').select('id, drive_file_name, transcript, created_at').ilike('transcript', f"%{search_val}%").order('created_at', desc=True).limit(2).execute()
                if c_res.data:
                    for c in c_res.data:
                        text = c.get('transcript', '').replace('\n', ' ')[:150]
                        lines.append(f"{age_tag(c.get('created_at'))} - [CALL RECORDING] {c.get('drive_file_name', '')}: {text}...")
            except Exception:
                pass
                
            if not lines:
                return "None"
            return "\n".join(lines)
            
        raw_comms_task = safe_fetch(fetch_raw_comms(), "None")

        async def fetch_canonical():
            if not active_anchor and not resolved_entity:
                return "None"
            search_val = (active_anchor["name"] if active_anchor else resolved_entity)
            if not search_val or search_val == "None":
                return "None"
            try:
                res = supabase.table('canonical_pages').select('title, content').eq('is_current', True).ilike('title', f"%{search_val}%").limit(1).execute()
                if res.data:
                    c = res.data[0].get('content', '')[:2000]
                    return f"{res.data[0].get('title')}:\n{c}..."
            except Exception:
                pass
            return "None"
        canonical_task = safe_fetch(fetch_canonical(), "None")

        async def fetch_projects():
            try:
                res = supabase.table('projects').select('name, status, organization_id, organizations(name)').eq('is_current', True).neq('status', 'archived').order('name').execute()
                if res.data:
                    lines = []
                    for p in res.data:
                        org_name = p.get('organizations', {}).get('name', 'INBOX') if p.get('organizations') else 'INBOX'
                        lines.append(f"- [{org_name}] {p.get('name')} ({p.get('status')})")
                    return "\n".join(lines)
            except Exception:
                pass
            return "None"
        projects_task = safe_fetch(fetch_projects(), "None") if (fetch_all or is_action) else safe_fetch(_empty_fetch("None"), "None")


        results = await asyncio.gather(
            tactical_map_task, tasks_task, memories_task, resources_task,
            practices_task, people_task, completed_task, calendar_task,
            emails_task, whatsapp_task, pending_decisions_task,
            temporal_task, serendipity_task, hindsight_task, raw_comms_task,
            canonical_task, projects_task
        )
        tactical_map, (compressed_tasks, _), memories_context, resources_context, \
            practices_context, people_context, completed_context, calendar_context, \
            emails_context, whatsapp_context, pending_decisions_context, \
            temporal_context, serendipity_context, hindsight_context, raw_comms_context, \
            canonical_context, projects_context = results

        available_sources = []
        all_context = []
        
        if tactical_map:
            all_context.append(f"TACTICAL MAP:\n{tactical_map}")
            available_sources.append("tactical map")
        if compressed_tasks:
            all_context.append(f"ACTIVE TASKS:\n{compressed_tasks}")
            available_sources.append("active tasks")
        if pending_decisions_context != "None":
            all_context.append(pending_decisions_context)
            available_sources.append("pending decisions")
        if completed_context != "None":
            all_context.append(f"RECENTLY COMPLETED TASKS:\n{completed_context}")
            available_sources.append("completed tasks")
        if memories_context != "None":
            all_context.append(f"RELEVANT MEMORIES:\n{memories_context}")
            available_sources.append("vault memories")
        if hindsight_context != "None" and hindsight_context != "":
            all_context.append(f"HINDSIGHT MEMORIES (Multi-signal):\n{hindsight_context}")
            available_sources.append("hindsight memories")
        if temporal_context != "None" and temporal_context != "":
            all_context.append(f"ON THIS DAY (Temporal patterns):\n{temporal_context}")
            available_sources.append("temporal patterns")
        if serendipity_context != "None" and "No multi-hop" not in serendipity_context and "No active tasks" not in serendipity_context and "Graph nodes unavailable" not in serendipity_context and "No graph nodes found" not in serendipity_context:
            all_context.append(f"SERENDIPITY (Hidden graph connections):\n{serendipity_context}")
            available_sources.append("serendipity connections")
        if emails_context != "None":
            all_context.append(f"EMAILS:\n{emails_context}")
            available_sources.append("emails")
        if whatsapp_context != "None":
            all_context.append(f"WHATSAPP MESSAGES:\n{whatsapp_context}")
            available_sources.append("whatsapp messages")
        if resources_context != "None":
            all_context.append(f"RESOURCES:\n{resources_context}")
            available_sources.append("resources")
        if practices_context != "None":
            all_context.append(f"PRACTICES:\n{practices_context}")
            available_sources.append("practices")
        if people_context != "None":
            all_context.append(f"PEOPLE:\n{people_context}")
            available_sources.append("people network")
        if calendar_context != "None":
            all_context.append(f"CALENDAR EVENTS:\n{calendar_context}")
            available_sources.append("calendar events")
        if raw_comms_context != "None":
            all_context.append(f"RAW EMAILS/MESSAGES (Exact match):\n{raw_comms_context}")
            available_sources.append("raw comms")
        if canonical_context != "None":
            all_context.append(f"CANONICAL KNOWLEDGE:\n{canonical_context}")
            available_sources.append("canonical knowledge")
        if projects_context != "None":
            all_context.append(f"ACTIVE PROJECTS:\n{projects_context}")
            available_sources.append("active projects")

        if not all_context:
            _last_reply = "🔍 *I don't have any relevant data to answer that.*\n\n_Try rephrasing._"
            await send_telegram(chat_id, _last_reply)
            return

        context_str = "\n\n".join(all_context)
        sources_str = ", ".join(available_sources)

        # Smart Header Detection
        if is_schedule and "calendar events" in available_sources:
            header = "📅 Here's your schedule:"
        elif is_action and "active tasks" in available_sources:
            header = "📋 Task status:"
        elif "vault memories" in available_sources and not is_action and not is_schedule and not is_comms:
            header = "🧠 From your vault:"
        else:
            header = "🧠 Here's what I found:"

        now_str = datetime.now(timezone(timedelta(hours=5, minutes=30))).strftime('%A, %d %B %Y, %H:%M %p IST')
        prompt = build_interrogate_brain_prompt(now_str, sources_str, context_str, conversation_history, query)

        # Log retrieval stage for "/why"
        entity_name = active_anchor["name"] if active_anchor else resolved_entity
        retrieved_items = [{"id": src, "content": src, "score": 1.0, "source": "interrogate_brain"} for src in available_sources]
        await log_decision(
            stage=DecisionStage.RETRIEVAL,
            query_text=query,
            resolved_entities=[entity_name] if entity_name else [],
            included_items=retrieved_items,
            reason_codes=[],
            summary=f"interrogate_brain: {len(available_sources)} sources consulted — {sources_str}"
        )

        response = await generate_content_with_fallback(
            prompt=prompt,
            workload=WorkloadProfile.INTERACTIVE,
            primary_model=CLASSIFICATION_MODEL,
            config={'response_mime_type': 'application/json', 'max_output_tokens': 800}
        )

        try:
            data = response.parse_json()
            answer = data.get("user_facing_summary", "").strip()
        except Exception as e:
            audit_log_sync("webhook", "ERROR", f"interrogate_brain JSON parse failed: {e}. Failing closed.")
            answer = "🧠 *I found some information, but had trouble formatting it safely.*"
        
        proactive_msg = ""
        if active_anchor:
            from core.pulse.proactive import check_proactive_signals
            try:
                proactive_msg = await asyncio.wait_for(
                    check_proactive_signals(active_anchor["name"]), timeout=1.5
                )
            except asyncio.TimeoutError:
                proactive_msg = ""
            
        final_reply = f"{header}\n\n{answer}"
        if proactive_msg:
            final_reply += f"\n\n{proactive_msg}"
            
        _last_reply = final_reply
        await send_telegram(chat_id, final_reply)

        # Log bot reply to conversation history
        if session_id:
            meta = {}
            if active_anchor:
                meta["active_anchor"] = active_anchor
            log_exchange(session_id, 'bot', 'QUERY', final_reply, chat_id, metadata=meta)
            
            # Persist active_anchor to thread for follow-up query carry-forward
            if active_anchor:
                try:
                    supabase.table('conversation_threads').update({
                        'active_anchor': active_anchor
                    }).eq('id', session_id).execute()
                except Exception as persist_e:
                    audit_log_sync("webhook", "WARNING", f"Failed to persist end-of-query anchor: {persist_e}")
            _persist_chain_id(session_id)

        # Log QUERY response to raw_dumps so it appears in web UI
        try:
            supabase.table('raw_dumps').insert([{
                "content": final_reply,
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
        _last_reply = "⚠️ *Search failed.*\n\n_Try again._"
        await send_telegram(chat_id, _last_reply)
    finally:
        if search_task:
            search_task.cancel()
        _searching_locks.discard(chat_id)
        return _last_reply

async def handle_noise(chat_id: int):
    await send_telegram(chat_id, "👍")

async def ask_task_update_confirmation(text: str, classification: dict, chat_id: int, session_id: str, matched_tasks: list):
    """Ask user whether to update an existing task or create a new one."""
    task = matched_tasks[0]
    reply = f"🧐 *This relates to an existing task:*\n\n_{task['title']}_"
    keyboard = [
        [{"text": "🔄 Update existing", "callback_data": "u"}],
        [{"text": "➕ Create new task", "callback_data": "n"}]
    ]
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
    await send_telegram(chat_id, reply, show_keyboard=False, inline_keyboard=keyboard)

async def resolve_task_update_confirmation(text: str, chat_id: int, session_id: str, last_clarification: dict) -> bool:
    """Handle user response to update-vs-create question."""
    cleaned = text.strip().lower()
    matched_tasks = last_clarification.get('matched_tasks', [])
    original = last_clarification.get("original", text)
    classification = last_clarification.get("classification", {"title": original})
    classification["intent"] = "TASK"

    is_update = cleaned in ('u', 'update') or 'update' in cleaned
    is_new = cleaned in ('n', 'new', 'create') or 'new' in cleaned or 'create' in cleaned

    if is_update and not is_new:
        target = matched_tasks[0]
        classification["task_update_id"] = target['id']
        log_exchange(session_id, 'user', 'TASK', text, chat_id)
        await route_by_intent("TASK", original, chat_id, session_id,
                              classification=classification, task_update_id=target['id'])
        return True
    elif is_new:
        log_exchange(session_id, 'user', 'TASK', text, chat_id)
        await route_by_intent("TASK", original, chat_id, session_id, classification=classification)
        return True
    elif is_update:
        target = matched_tasks[0]
        classification["task_update_id"] = target['id']
        log_exchange(session_id, 'user', 'TASK', text, chat_id)
        await route_by_intent("TASK", original, chat_id, session_id,
                              classification=classification, task_update_id=target['id'])
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
            .eq('is_current', True) \
            .execute()
        
        # Filter status in Python because status is inside the metadata JSONB column
        existing_practices = [p for p in (existing_res.data or []) if p.get('metadata', {}).get('status') in ['active', 'dormant']]

        if existing_practices:
            name_embedding = (await get_embedding(practice_name)).vector
            for p in existing_practices:
                p_label = p.get('label', '')
                p_embedding = (await get_embedding(p_label)).vector
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
            "normalized_label": normalize_label(practice_name),
            "metadata": metadata
        }).execute()

        if node_res.data:
            await send_telegram(chat_id, f"Tracking: {practice_name}")
            audit_log_sync("webhook", "INFO", f"DECLARE_PRACTICE: Created practice node '{practice_name}'")
        else:
            await send_telegram(chat_id, "⚠️ Could not create practice. Try again.")

    except Exception as e:
        audit_log_sync("webhook", "ERROR", f"handle_declare_practice error: {e}")
        await send_telegram(chat_id, "⚠️ Something went wrong. Try again.")


def _persist_chain_id(session_id: str):
    """Store the current decision_chain_id on the conversation thread."""
    from core.lib.decision_audit import get_decision_chain_id
    cid = get_decision_chain_id()
    if not cid or not session_id:
        return
    try:
        supabase.table('conversation_threads').update({
            'last_decision_chain_id': cid
        }).eq('id', session_id).execute()
    except Exception:
        pass

