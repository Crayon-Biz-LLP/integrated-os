from core.llm.constants import SYNTHESIS_MODEL
from core.services.db import get_supabase
import os
from datetime import datetime, timezone, timedelta
from googleapiclient.discovery import build

from core.lib.audit_logger import audit_log_sync
from core.services.google_service import get_google_creds
from core.webhook.telegram import send_telegram
from core.pulse.calendar import MemoryCache
from core.llm.fallback import generate_content_with_fallback
from core.llm.config import WorkloadProfile
from core.retrieval.config import config as retrieval_config
from core.retrieval.pipeline import retry_failed_index_runs
from core.decisions import expire_stale_decisions

def get_recently_ended_events(minutes_ended_min=5, minutes_ended_max=30):
    """Fetch events that ended between X and Y minutes ago — for post-meeting capture prompts.

    Google Calendar API filters by START time, so we fetch a wider window and filter
    by actual end time in Python.
    """
    service = build('calendar', 'v3', credentials=get_google_creds(), cache=MemoryCache())
    now = datetime.now(timezone.utc)
    # Wider window: fetch events that started up to 2 hours before the end window
    time_min = (now - timedelta(minutes=minutes_ended_max + 120)).isoformat()

    try:
        events_res = service.events().list(
            calendarId='primary',
            timeMin=time_min,
            singleEvents=True,
            orderBy='startTime'
        ).execute()

        ended = []
        for ev in events_res.get('items', []):
            end_raw = ev.get('end', {}).get('dateTime')
            if not end_raw:
                continue
            end_dt = datetime.fromisoformat(end_raw.replace('Z', '+00:00'))
            mins_ago = (now - end_dt).total_seconds() / 60
            if minutes_ended_min <= mins_ago <= minutes_ended_max:
                ended.append(ev)
        return ended
    except Exception as e:
        audit_log_sync("sentinel", "ERROR", f"Failed to fetch recently ended events: {e}")
        return []


def get_upcoming_events(minutes_ahead=60):
    """Fetch events starting between now and X minutes from now."""
    service = build('calendar', 'v3', credentials=get_google_creds(), cache=MemoryCache())
    
    # Needs timezone awareness, use UTC because format_rfc3339 expects it or naive.
    # Google API requires RFC3339 format.
    now = datetime.now(timezone.utc)
    end_time = now + timedelta(minutes=minutes_ahead)
    
    rfc_start = now.isoformat()
    rfc_end = end_time.isoformat()

    try:
        events_res = service.events().list(
            calendarId='primary',
            timeMin=rfc_start,
            timeMax=rfc_end,
            singleEvents=True,
            orderBy='startTime'
        ).execute()
        
        return events_res.get('items', [])
    except Exception as e:
        audit_log_sync("sentinel", "ERROR", f"Failed to fetch upcoming events: {e}")
        return []

async def fetch_event_context(title: str, supabase):
    """S2: Rich meeting prep context — graph-connected people, task edges, recent emails, and memories."""
    words = [w for w in title.split() if len(w) > 3]
    if not words:
        return ""

    query = " | ".join(words)
    context_parts = []
    matched_people = []

    try:
        # 1. Relevant active tasks
        tasks_res = supabase.table('tasks')\
            .select('title, status, priority, direction, committed_to')\
            .eq('is_current', True)\
            .not_.in_('status', ['done', 'cancelled'])\
            .text_search('title', query)\
            .limit(5)\
            .execute()
        if tasks_res.data:
            context_parts.append("📌 Relevant Pending Tasks:")
            for t in tasks_res.data:
                dir_str = ""
                if t.get('direction') == 'waiting_on':
                    dir_str = f" [WAITING ON: {t.get('committed_to', '?')}]"
                elif t.get('direction') == 'outbound':
                    dir_str = f" [OWED TO: {t.get('committed_to', '?')}]"
                context_parts.append(f"- [{t.get('priority', 'important')}] {t['title']}{dir_str}")
    except Exception:
        pass

    try:
        # 2. Graph-connected people — find people mentioned in event title
        people_res = supabase.table('graph_nodes')\
            .select('id, label, metadata')\
            .eq('type', 'person')\
            .execute()
        matched_people = []
        for p in (people_res.data or []):
            if p['label'].lower() in title.lower():
                matched_people.append(p)
        if matched_people:
            context_parts.append("👥 People in this meeting:")
            for p in matched_people[:5]:
                person_id = p.get('metadata', {}).get('people_id') if isinstance(p.get('metadata'), dict) else None
                # Find their active tasks
                if person_id:
                    try:
                        ptasks = supabase.table('graph_edges')\
                            .select('target_node_id, relationship')\
                            .eq('source_node_id', p['id'])\
                            .in_('relationship', ['INVOLVES', 'WORKS_ON', 'ASSIGNED_TO'])\
                            .limit(3)\
                            .execute()
                        task_count = len(ptasks.data or [])
                        context_parts.append(f"- {p['label']}: {task_count} active task connection(s)")
                    except Exception:
                        context_parts.append(f"- {p['label']}")
                else:
                    context_parts.append(f"- {p['label']}")
    except Exception:
        pass

    try:
        # 3. Recent emails from/to matched people (last 7 days)
        if matched_people:
            person_names = [p['label'] for p in matched_people[:3]]
            seven_days_ago = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
            email_conditions = [f'sender_name.ilike.%{name}%' for name in person_names]
            email_res = supabase.table('messages')\
                .select('sender_name, subject, created_at')\
                .eq('channel', 'email')\
                .gte('created_at', seven_days_ago)\
                .or_(','.join(email_conditions))\
                .order('created_at', desc=True)\
                .limit(3)\
                .execute()
            if email_res.data:
                context_parts.append("📧 Recent emails:")
                for e in email_res.data:
                    context_parts.append(f"- From {e.get('sender_name', '?')}: {(e.get('subject', '')[:60])}")
    except Exception:
        pass

    try:
        # 4. Semantically related memories (last 30 days)
        from core.retrieval.search import search_memories_compat
        memories = await search_memories_compat(
            query_text=title,
            top_k=3,
            threshold=0.6,
            recency_weight=0.5,
            importance_weight=0.2
        )
        if memories:
            context_parts.append("🧠 Relevant memories:")
            for m in memories[:3]:
                context_parts.append(f"- [{m.get('memory_type', '')}] {m.get('content', '')[:100]}")
    except Exception:
        pass

    return "\n".join(context_parts) if context_parts else ""

async def process_sentinel(auth_secret: str, trigger: str = "cron"):
    from core.pulse.run_logger import create_pulse_run, complete_pulse_run

    """Runs the Sentinel high-frequency scanner."""
    print("🛡️ Running Sentinel Nudge check...")
    supabase_url = os.getenv("SUPABASE_URL")
    supabase_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
    telegram_chat_id = os.getenv("TELEGRAM_CHAT_ID")
    
    if not supabase_url or not supabase_key or not telegram_chat_id:
        print("Sentinel missing env vars.")
        return {"error": "Missing env vars", "status": 500}

    supabase = get_supabase()
    run_id = await create_pulse_run(supabase, "sentinel", trigger)

    try:
        events = get_upcoming_events(minutes_ahead=60)
        if not events:
            print("No upcoming events in the next 60 mins.")
            await complete_pulse_run(supabase, run_id, status="completed",
                metadata={"reason": "no_upcoming", "alerted": 0})
            return {"success": True, "alerted": 0}

        now = datetime.now(timezone.utc)
        alerted_count = 0
        
        for event in events:
            try:
                event_id = event.get('id')
                title = event.get('summary', 'Untitled Event')
                start_raw = event.get('start', {}).get('dateTime')
                if not start_raw:
                    continue
                    
                start_dt = datetime.fromisoformat(start_raw.replace('Z', '+00:00'))
                mins_until = int((start_dt - now).total_seconds() / 60)
                
                if mins_until < 0 or mins_until > 45:
                    continue

                search_str = f"Sentinel_Sent:{event_id}"
                
                recent_log = supabase.table('audit_logs')\
                    .select('id')\
                    .eq('service', 'sentinel')\
                    .ilike('message', f"%{search_str}%")\
                    .limit(1)\
                    .execute()
                    
                if recent_log.data:
                    print(f"Skipping {title} (already nudged).")
                    continue
                    
                context = await fetch_event_context(title, supabase)
                
                msg = f"🚨 **ALARM: Meeting in {mins_until} mins!**\n📅 {title}"
                if context:
                    prompt = f"Write a 1-2 sentence maximum 'Pre-Flight Briefing' for a meeting called '{title}'. Here is some context from my system. Be extremely brief, do not use pleasantries. Just say what I need to know.\n\nContext:\n{context}"
                    
                    try:
                        ai_briefing = await generate_content_with_fallback(
                            prompt=prompt,
                            workload=WorkloadProfile.SYNTHESIS,
                            primary_model=os.getenv("GEMINI_FLASH_MODEL", SYNTHESIS_MODEL),
                            config={"temperature": 0.2}
                        )
                        msg += f"\n\n🧠 **Pre-Flight Context:**\n{ai_briefing.text.strip()}"
                    except Exception as e:
                        audit_log_sync("sentinel", "WARNING", f"AI context generation failed: {e}")
                        msg += f"\n\n🧠 **Context found:**\n{context}"

                success = await send_telegram(int(telegram_chat_id), msg)
                
                if success:
                    audit_log_sync("sentinel", "INFO", f"{search_str} - Nudged for {title}")
                    alerted_count += 1
                    print(f"✅ Nudged for: {title}")
                else:
                    audit_log_sync("sentinel", "ERROR", f"Failed to send Telegram nudge for {title}")
            except Exception as event_err:
                audit_log_sync("sentinel", "ERROR", f"Event processing failed for {event.get('summary', 'unknown')}: {event_err}")
                
        # --- PIGGYBACK: Weekly catch-up sweep (Sunday only) ---
        try:
            if now.weekday() == 6:  # Sunday
                last_sweep = supabase.table('audit_logs') \
                    .select('id') \
                    .eq('service', 'sentinel') \
                    .ilike('message', '%weekly_sweep%') \
                    .gte('created_at', (datetime.now(timezone.utc) - timedelta(hours=20)).isoformat()) \
                    .limit(1) \
                    .execute()
                if not last_sweep.data:
                    sweep_lines = []
                    # Stale tasks (>14 days, not done/cancelled)
                    fourteen_days_ago = (now - timedelta(days=14)).isoformat()
                    stale_res = supabase.table('tasks') \
                        .select('id, title, created_at, reminder_at') \
                        .eq('is_current', True) \
                        .eq('status', 'todo') \
                        .lt('created_at', fourteen_days_ago) \
                        .limit(10) \
                        .execute()
                    if stale_res.data:
                        sweep_lines.append(f"⏳ {len(stale_res.data)} task(s) stale >14 days:")
                        for t in stale_res.data[:5]:
                            try:
                                created = datetime.fromisoformat(t['created_at'].replace('Z', '+00:00'))
                                days_old = (now - created).days
                                sweep_lines.append(f"  • {t['title']} ({days_old}d old)")
                            except Exception:
                                sweep_lines.append(f"  • {t.get('title', 'Untitled')}")
                    # Unresolved clarifications
                    clar_res = supabase.table('clarification_feedback') \
                        .select('id, question_text, created_at') \
                        .is_('resolved_at', 'null') \
                        .gt('expires_at', now.isoformat()) \
                        .limit(5) \
                        .execute()
                    if clar_res.data:
                        sweep_lines.append(f"❓ {len(clar_res.data)} unanswered clarification(s)")
                    # Pending graph nodes
                    pg_res = supabase.table('pending_graph_nodes') \
                        .select('id, label, type') \
                        .eq('status', 'pending') \
                        .order('created_at', desc=True) \
                        .limit(10) \
                        .execute()
                    if pg_res.data:
                        sweep_lines.append(f"🕸️ {len(pg_res.data)} pending graph node(s)")
                    # Pending edges
                    pe_res = supabase.table('pending_graph_edges') \
                        .select('id') \
                        .eq('status', 'pending') \
                        .limit(10) \
                        .execute()
                    if pe_res.data:
                        sweep_lines.append(f"🔗 {len(pe_res.data)} pending graph edge(s)")
                    # Expire stale decisions (past their expires_at)
                    try:
                        expired_count = expire_stale_decisions()
                        if expired_count:
                            sweep_lines.append(f"⏰ {expired_count} expired decision(s) auto-closed")
                    except Exception as dec_err:
                        audit_log_sync("sentinel", "WARNING", f"Decision expiry failed: {dec_err}")

                    if sweep_lines:
                        sweep_msg = "📋 *Weekly Sweep — Items Needing Attention*\n\n" + "\n".join(sweep_lines)
                        await send_telegram(int(telegram_chat_id), sweep_msg)
                        audit_log_sync("sentinel", "INFO", "weekly_sweep: Sent weekly catch-up summary")
                    else:
                        audit_log_sync("sentinel", "INFO", "weekly_sweep: All clear — nothing stale")
        except Exception as e:
            audit_log_sync("sentinel", "ERROR", f"Weekly sweep error: {e}")

        # --- PIGGYBACK: Post-event capture prompts ---
        # Fire 5-30 min after an event ends, asking for notes/outcomes.
        try:
            post_events = get_recently_ended_events(minutes_ended_min=5, minutes_ended_max=30)
            for event in post_events:
                event_id = event.get('id')
                title = event.get('summary', 'Untitled Event')

                search_str = f"Sentinel_PostCapture:{event_id}"
                recent_log = supabase.table('audit_logs') \
                    .select('id') \
                    .eq('service', 'sentinel') \
                    .ilike('message', f"%{search_str}%") \
                    .limit(1) \
                    .execute()
                if recent_log.data:
                    continue

                msg = f"📝 **Meeting just ended: {title}**\nAny notes, decisions, or follow-ups from this? Just type naturally and I'll capture it."
                success = await send_telegram(int(telegram_chat_id), msg)
                if success:
                    audit_log_sync("sentinel", "INFO", f"{search_str} - Post-capture prompt for {title}")
        except Exception as e:
            audit_log_sync("sentinel", "ERROR", f"Post-event capture error: {e}")

        # --- PIGGYBACK: Dispatch unanswered clarifications ---
        try:
            clarifications_res = supabase.table('clarification_feedback') \
                .select('*') \
                .is_('resolved_at', 'null') \
                .is_('sent_at', 'null') \
                .gt('expires_at', datetime.now(timezone.utc).isoformat()) \
                .limit(5) \
                .execute()
                
            if clarifications_res.data:
                from core.clarifier import build_batch
                batch_msg = build_batch(clarifications_res.data, max_items=5)
                if batch_msg:
                    success = await send_telegram(int(telegram_chat_id), batch_msg)
                    if success:
                        c_ids = [c['id'] for c in clarifications_res.data]
                        supabase.table('clarification_feedback').update({
                            "sent_at": datetime.now(timezone.utc).isoformat()
                        }).in_('id', c_ids).execute()
                        audit_log_sync("sentinel", "INFO", f"Dispatched {len(c_ids)} clarifications")
        except Exception as e:
            audit_log_sync("sentinel", "ERROR", f"Clarification dispatch error: {e}")

        # --- PIGGYBACK: Clean up stale raw_dumps ---
        try:
            cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
            stale = supabase.table('raw_dumps') \
                .update({"status": "abandoned"}) \
                .in_('status', ['staged', 'pending']) \
                .lt('created_at', cutoff) \
                .execute()
            if stale.data:
                audit_log_sync("sentinel", "INFO",
                               f"Cleaned {len(stale.data)} stale raw_dumps (staged/pending >24h)")
        except Exception as e:
            audit_log_sync("sentinel", "ERROR", f"Raw dump cleanup error: {e}")

        # --- PIGGYBACK: Classifier feedback ingestion ---
        try:
            from core.webhook.feedback_loop import ingest_feedback_overrides
            corrections_count = ingest_feedback_overrides()
            if corrections_count > 0:
                audit_log_sync("sentinel", "INFO",
                               f"Feedback ingestion: {corrections_count} correction(s) processed")
        except Exception as e:
            audit_log_sync("sentinel", "WARNING", f"Feedback ingestion piggyback error (non-critical): {e}")

        # --- PIGGYBACK: Daily orphan retrieval sweep ---
        try:
            last_sweep = supabase.table('audit_logs') \
                .select('id') \
                .eq('service', 'sentinel') \
                .ilike('message', '%orphan sweep%') \
                .gte('created_at', (datetime.now(timezone.utc) - timedelta(hours=20)).isoformat()) \
                .limit(1) \
                .execute()
            if not last_sweep.data:
                from core.retrieval.cleanup import sweep_orphan_retrieval_entries
                sweep_orphan_retrieval_entries()
                audit_log_sync("sentinel", "INFO", "Daily orphan sweep completed")
        except Exception as e:
            audit_log_sync("sentinel", "ERROR", f"Orphan sweep piggyback error: {e}")

        # --- PIGGYBACK: Graph edge expiry (TF-002) ---
        try:
            last_edge_sweep = supabase.table('audit_logs') \
                .select('id') \
                .eq('service', 'sentinel') \
                .ilike('message', '%graph edge expiry%') \
                .gte('created_at', (datetime.now(timezone.utc) - timedelta(hours=20)).isoformat()) \
                .limit(1) \
                .execute()
            if not last_edge_sweep.data:
                result = supabase.rpc('expire_stale_graph_edges', {'expiry_days': 90}).execute()
                expired_count = result.data if result.data else 0
                if expired_count:
                    audit_log_sync("sentinel", "INFO", f"🕸️ Graph edge expiry: {expired_count} stale edges marked")
                else:
                    audit_log_sync("sentinel", "INFO", "🕸️ Graph edge expiry: no stale edges found")
        except Exception as e:
            audit_log_sync("sentinel", "WARNING", f"Graph edge expiry error (non-critical): {e}")

        # --- PIGGYBACK: People enrichment from graph edges (TF-003) ---
        try:
            last_people_sweep = supabase.table('audit_logs') \
                .select('id') \
                .eq('service', 'sentinel') \
                .ilike('message', '%people enrichment%') \
                .gte('created_at', (datetime.now(timezone.utc) - timedelta(hours=20)).isoformat()) \
                .limit(1) \
                .execute()
            if not last_people_sweep.data:
                from core.lib.people_utils import enrich_people_from_graph
                enriched = enrich_people_from_graph()
                if enriched:
                    audit_log_sync("sentinel", "INFO", f"👥 People enrichment: {enriched} person(s) updated from graph edges")
                else:
                    audit_log_sync("sentinel", "INFO", "👥 People enrichment: no updates needed")
        except Exception as e:
            audit_log_sync("sentinel", "WARNING", f"People enrichment error (non-critical): {e}")

        # --- PIGGYBACK: S4 Pattern detection (Sunday only) ---
        try:
            if now.weekday() == 6:  # Sunday
                last_pattern_sweep = supabase.table('audit_logs') \
                    .select('id') \
                    .eq('service', 'sentinel') \
                    .ilike('message', '%pattern detection%') \
                    .gte('created_at', (datetime.now(timezone.utc) - timedelta(hours=20)).isoformat()) \
                    .limit(1) \
                    .execute()
                if not last_pattern_sweep.data:
                    from core.pulse.patterns import detect_completion_patterns, format_patterns_for_briefing
                    patterns = detect_completion_patterns()
                    patterns_str = format_patterns_for_briefing(patterns)
                    if patterns_str and patterns.get('insights'):
                        # Store for next briefing to consume
                        supabase.table('core_config').upsert({
                            'key': 'weekly_patterns',
                            'content': patterns_str
                        }, on_conflict='key').execute()
                        audit_log_sync('sentinel', 'INFO', f'Pattern detection: {len(patterns["insights"])} insight(s) stored')
                    else:
                        audit_log_sync('sentinel', 'INFO', 'Pattern detection: no significant patterns found')
        except Exception as pat_err:
            audit_log_sync('sentinel', 'WARNING', f'Pattern detection error (non-critical): {pat_err}')

        # --- PIGGYBACK: S1 Proactive delegation alerts ---
        # Check waiting_on tasks that are stale (>3 days) and push a nudge
        try:
            last_del_sweep = supabase.table('audit_logs') \
                .select('id') \
                .eq('service', 'sentinel') \
                .ilike('message', '%delegation alert%') \
                .gte('created_at', (datetime.now(timezone.utc) - timedelta(hours=20)).isoformat()) \
                .limit(1) \
                .execute()
            if not last_del_sweep.data:
                waiting_tasks = supabase.table('tasks') \
                    .select('id, title, created_at, reminder_at, committed_to, direction') \
                    .eq('is_current', True) \
                    .eq('status', 'todo') \
                    .eq('direction', 'waiting_on') \
                    .not_.is_('committed_to', 'null') \
                    .execute()
                stale_delegations = []
                for t in (waiting_tasks.data or []):
                    last_touch = t.get('reminder_at') or t.get('created_at')
                    if not last_touch:
                        continue
                    try:
                        touch_dt = datetime.fromisoformat(str(last_touch).replace('Z', '+00:00').replace(' ', 'T'))
                        if touch_dt.tzinfo is None:
                            touch_dt = touch_dt.replace(tzinfo=timezone.utc)
                        days_stale = (datetime.now(timezone.utc) - touch_dt).days
                        if days_stale >= 3:
                            stale_delegations.append({
                                'title': t['title'],
                                'person': t['committed_to'],
                                'days': days_stale,
                                'task_id': t['id']
                            })
                    except Exception:
                        pass
                if stale_delegations:
                    stale_delegations.sort(key=lambda x: x['days'], reverse=True)
                    del_lines = ["⏳ *Delegation Stale — Needs Attention*\n"]
                    for d in stale_delegations[:5]:
                        del_lines.append(f"• Waiting on *{d['person']}* for {d['days']}d: {d['title']}")
                    del_msg = "\n".join(del_lines)
                    success = await send_telegram(int(telegram_chat_id), del_msg)
                    if success:
                        audit_log_sync("sentinel", "INFO", f"delegation alert: {len(stale_delegations)} stale delegation(s) flagged")
        except Exception as del_err:
            audit_log_sync("sentinel", "WARNING", f"Delegation alert error (non-critical): {del_err}")

        # --- PIGGYBACK: T1 Priority auto-escalation ---
        try:
            last_esc_sweep = supabase.table('audit_logs') \
                .select('id') \
                .eq('service', 'sentinel') \
                .ilike('message', '%auto-escalation%') \
                .gte('created_at', (datetime.now(timezone.utc) - timedelta(hours=6)).isoformat()) \
                .limit(1) \
                .execute()
            if not last_esc_sweep.data:
                esc_candidates = supabase.table('tasks') \
                    .select('id, title, created_at, priority, status, organization_id') \
                    .eq('is_current', True) \
                    .eq('status', 'todo') \
                    .eq('priority', 'important') \
                    .execute()
                escalated = []
                for t in (esc_candidates.data or []):
                    ca = t.get('created_at', '')
                    if not ca:
                        continue
                    try:
                        created_dt = datetime.fromisoformat(str(ca).replace('Z', '+00:00'))
                        days_old = (datetime.now(timezone.utc) - created_dt).days
                        if days_old >= 7:
                            supabase.table('tasks').update({'priority': 'urgent'}).eq('id', t['id']).execute()
                            escalated.append(t['title'][:50])
                    except Exception:
                        pass
                if escalated:
                    audit_log_sync("sentinel", "INFO", f"auto-escalation: {len(escalated)} task(s) escalated to urgent")
        except Exception as esc_err:
            audit_log_sync("sentinel", "WARNING", f"Auto-escalation error (non-critical): {esc_err}")

        # --- PIGGYBACK: S5 Follow-up auto-cancel ---
        # Close stale waiting_on tasks (>14d) that are unlikely to resolve
        try:
            last_auto_cancel = supabase.table('audit_logs') \
                .select('id') \
                .eq('service', 'sentinel') \
                .ilike('message', '%auto-cancel%') \
                .gte('created_at', (datetime.now(timezone.utc) - timedelta(hours=12)).isoformat()) \
                .limit(1) \
                .execute()
            if not last_auto_cancel.data:
                stale_waiting = supabase.table('tasks') \
                    .select('id, title, created_at, reminder_at, committed_to') \
                    .eq('is_current', True) \
                    .eq('status', 'todo') \
                    .eq('direction', 'waiting_on') \
                    .execute()
                cancelled = []
                for t in (stale_waiting.data or []):
                    touch = t.get('reminder_at') or t.get('created_at', '')
                    if not touch:
                        continue
                    try:
                        touch_dt = datetime.fromisoformat(str(touch).replace('Z', '+00:00'))
                        if touch_dt.tzinfo is None:
                            touch_dt = touch_dt.replace(tzinfo=timezone.utc)
                        days_stale = (datetime.now(timezone.utc) - touch_dt).days
                        if days_stale >= 14:
                            supabase.table('tasks').update({'status': 'cancelled'}).eq('id', t['id']).execute()
                            cancelled.append(t['title'][:60])
                    except Exception:
                        pass
                if cancelled:
                    audit_log_sync("sentinel", "INFO", f"auto-cancel: {len(cancelled)} stale waiting_on task(s) auto-cancelled (>14d)")
        except Exception as ac_err:
            audit_log_sync("sentinel", "WARNING", f"Auto-cancel error (non-critical): {ac_err}")

        # --- PIGGYBACK: M5 Expired memory sweep ---
        try:
            last_mem_sweep = supabase.table('audit_logs') \
                .select('id') \
                .eq('service', 'sentinel') \
                .ilike('message', '%memory sweep%') \
                .gte('created_at', (datetime.now(timezone.utc) - timedelta(hours=12)).isoformat()) \
                .limit(1) \
                .execute()
            if not last_mem_sweep.data:
                expired = supabase.table('memories') \
                    .select('id') \
                    .lt('expires_at', datetime.now(timezone.utc).isoformat()) \
                    .execute()
                expired_ids = [m['id'] for m in (expired.data or [])]
                if expired_ids:
                    from core.retrieval.cleanup import cleanup_memory_retrieval_index
                    failed = 0
                    for mid in expired_ids:
                        ok = False
                        for attempt in range(2):  # Retry once per item
                            try:
                                cleanup_memory_retrieval_index(mid)
                                supabase.table('memories').delete().eq('id', mid).execute()
                                ok = True
                                break
                            except Exception:
                                if attempt == 0:
                                    continue  # Retry
                        if not ok:
                            failed += 1
                            audit_log_sync("sentinel", "WARNING",
                                f"memory sweep: failed to clean up memory {mid} after 2 attempts")
                    if failed > len(expired_ids) // 2:
                        audit_log_sync("sentinel", "WARNING",
                            f"memory sweep: {failed}/{len(expired_ids)} items failed cleanup")
                    audit_log_sync("sentinel", "INFO",
                        f"memory sweep: {len(expired_ids) - failed}/{len(expired_ids)} expired memory(s) removed")
                    # Also run orphan sweep after cleanup
                    try:
                        from core.retrieval.cleanup import sweep_orphan_retrieval_entries
                        sweep_orphan_retrieval_entries()
                    except Exception:
                        pass
                else:
                    audit_log_sync("sentinel", "INFO", "memory sweep: no expired memories found")
        except Exception as mem_err:
            audit_log_sync("sentinel", "WARNING", f"Memory sweep error (non-critical): {mem_err}")

        # --- PIGGYBACK: Retry failed retrieval index runs ---
        if retrieval_config.indexing_enabled:
            try:
                retried = await retry_failed_index_runs(
                    max_retries=3, batch_size=10, retry_delay_seconds=0
                )
                if retried > 0:
                    audit_log_sync("sentinel", "INFO",
                                   f"Retry sweeper retried {retried} failed index runs")
            except Exception as e:
                audit_log_sync("sentinel", "ERROR",
                               f"Retry sweeper error: {e}")

        # --- PIGGYBACK: T4 Orphan recurring calendar events ---
        try:
            orphan_res = supabase.table('tasks') \
                .select('id, title, google_event_id, recurrence') \
                .eq('is_current', True) \
                .eq('status', 'cancelled') \
                .not_.is_('google_event_id', 'null') \
                .execute()
            orphan_events = []
            for t in (orphan_res.data or []):
                rec = t.get('recurrence', '')
                if rec and rec.lower() not in ('', 'none'):
                    from core.services.google_service import delete_calendar_event
                    delete_calendar_event(t['google_event_id'])
                    supabase.table('tasks').update({'google_event_id': None}).eq('id', t['id']).execute()
                    orphan_events.append(t['title'][:50])
            if orphan_events:
                audit_log_sync("sentinel", "INFO", f"orphan calendar: {len(orphan_events)} recurring event(s) cleaned up for cancelled tasks")
        except Exception as oe_err:
            audit_log_sync("sentinel", "WARNING", f"Orphan calendar cleanup error (non-critical): {oe_err}")

        await complete_pulse_run(supabase, run_id, status="completed",
            metadata={"alerted": alerted_count})
        return {"success": True, "alerted": alerted_count}

    except Exception as e:
        import traceback
        audit_log_sync("sentinel", "CRITICAL", f"Sentinel Critical Error: {e}")
        traceback.print_exc()
        await complete_pulse_run(supabase, run_id, status="failed", error_message=str(e))
        return {"error": str(e)}
