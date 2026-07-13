from core.context import execute_context_strategy, PRE_FLIGHT_CONFIG
from core.llm.constants import SYNTHESIS_MODEL
from core.services.db import get_supabase
import os
import hashlib
import json
from datetime import datetime, timezone, timedelta
from googleapiclient.discovery import build

from core.lib.audit_logger import audit_log_sync
from core.services.google_service import get_google_creds
from core.webhook.telegram import send_telegram
from core.pulse.calendar import MemoryCache
from core.llm.fallback import generate_content_with_fallback
from core.llm.config import WorkloadProfile
from core.services.push_notification import send_push_notification


def hash_features_simple(features: dict, subsystem: str) -> str:
    """Simplified hash computation for sentinel use.

    Mirrors core.lib.telemetry.hash_features for hash chain consistency
    between sentinel feature hashing and telemetry pattern matching.

    Args:
        features: Feature dict, e.g. {"source": "telegram", "node_type": "person"}
        subsystem: Subsystem name for namespacing

    Returns:
        First 16 chars of MD5 hexdigest
    """
    canonical = {k: v for k, v in sorted(features.items()) if v is not None}
    raw = json.dumps(canonical, sort_keys=True, default=str)
    return hashlib.md5(f"{subsystem}:{raw}".encode()).hexdigest()[:16]

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
    """S2: Rich meeting prep context using Context Registry."""
    words = [w for w in title.split() if w.lower() not in ['a', 'an', 'the', 'in', 'on', 'at', 'with', 'for']]
    if not words:
        return ""
    
    query = " ".join(words)
    
    # Capitalized words as a fallback hint
    import re
    entities = re.findall(r'\b[A-Z][a-z]+\b', title)
    entities = [e for e in entities if e.lower() not in ['a', 'an', 'the', 'in', 'on', 'at', 'with', 'for']]
    
    result = await execute_context_strategy(
        query=query,
        strategy=PRE_FLIGHT_CONFIG,
        extracted_entities=entities
    )
    
    return result.get_formatted_context()

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
                    prompt = f"""Below is verified context for a meeting called '{title}'. Summarize the relevant context for this meeting. You may draw explicit inferences from dates and action items shown (e.g., if a due date is in the past, note it as overdue). Do not fabricate facts not present in the retrieved context. If context is empty, say 'No relevant context found.'

Return ONLY valid JSON:
{{
  "answer_type": "status_only",
  "user_facing_summary": "The 1-2 sentence briefing.",
  "claimed_actions": [],
  "needs_execution": false
}}

Context:
{context}"""
                    
                    try:
                        ai_briefing = await generate_content_with_fallback(
                            prompt=prompt,
                            workload=WorkloadProfile.SYNTHESIS,
                            primary_model=os.getenv("GEMINI_FLASH_MODEL", SYNTHESIS_MODEL),
                            config={"temperature": 0.2, "response_mime_type": "application/json"}
                        )
                        try:
                            data = ai_briefing.parse_json()
                            summary = data.get("user_facing_summary", "").strip()
                        except Exception as e:
                            audit_log_sync("sentinel", "ERROR", f"Sentinel JSON parse failed: {e}. Failing closed.")
                            summary = "Context generation failed formatting."
                        msg += f"\n\n🧠 **Pre-Flight Context:**\n{summary}"
                    except Exception as e:
                        audit_log_sync("sentinel", "WARNING", f"AI context generation failed: {e}")
                        msg += f"\n\n🧠 **Context found:**\n{context}"

                success = await send_telegram(int(telegram_chat_id), msg)
                
                if success:
                    audit_log_sync("sentinel", "INFO", f"{search_str} - Nudged for {title}")
                    alerted_count += 1
                    print(f"✅ Nudged for: {title}")
                    # P4: Push notification for meeting nudge (only when within 15 mins)
                    if mins_until <= 15:
                        try:
                            await send_push_notification(
                                title=f"Meeting in {mins_until} min",
                                body=title,
                                data={"type": "nudge", "event_title": title},
                            )
                        except Exception as push_err:
                            audit_log_sync("sentinel", "WARNING", f"Push nudge failed (non-critical): {push_err}")
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
                    # Decision expiry deferred to maintenance.py (weekly_housekeeping)

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

        # --- PIGGYBACK: Classifier feedback ingestion ---
        try:
            from core.webhook.feedback_loop import ingest_feedback_overrides
            corrections_count = ingest_feedback_overrides()
            if corrections_count > 0:
                audit_log_sync("sentinel", "INFO",
                               f"Feedback ingestion: {corrections_count} correction(s) processed")
        except Exception as e:
            audit_log_sync("sentinel", "WARNING", f"Feedback ingestion piggyback error (non-critical): {e}")

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
                        # P4: Push notification for stale delegations
                        try:
                            top_person = stale_delegations[0]["person"] if stale_delegations else "someone"
                            await send_push_notification(
                                title=f"⏳ {len(stale_delegations)} stale delegation(s)",
                                body=f"Waiting on {top_person} and {len(stale_delegations)-1} other(s)" if len(stale_delegations) > 1 else f"Waiting on {top_person}",
                                data={"type": "delegation"},
                            )
                        except Exception as push_err:
                            audit_log_sync("sentinel", "WARNING", f"Push delegation alert failed (non-critical): {push_err}")
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

        # Memory sweep, index queue, and retry-failed-runs deferred to maintenance.py (daily mode)

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

        # --- PIGGYBACK: T5 Graph Integrity Sweep ---
        try:
            # Copy approved pending edges with node_ids to graph_edges if missing
            pe_res = supabase.table('pending_graph_edges').select('id, source_node_id, relationship, target_node_id, shortcode, source_text, metadata') \
                .eq('status', 'approved') \
                .neq('approval_source', 'provenance') \
                .not_.is_('source_node_id', 'null') \
                .not_.is_('target_node_id', 'null') \
                .order('created_at', desc=True).limit(500).execute()
            
            if pe_res.data:
                valid_node_ids = set()
                n_res = supabase.table("graph_nodes").select("id").eq('is_current', True).execute()
                if n_res.data:
                    valid_node_ids = {n['id'] for n in n_res.data}
                
                to_insert = []
                seen = set()
                for pe in pe_res.data:
                    if pe["source_node_id"] not in valid_node_ids or pe["target_node_id"] not in valid_node_ids:
                        continue
                    key = (pe["source_node_id"], pe["relationship"], pe["target_node_id"])
                    if key in seen:
                        continue
                    seen.add(key)
                    to_insert.append({
                        "source_node_id": pe["source_node_id"],
                        "relationship": pe["relationship"],
                        "target_node_id": pe["target_node_id"],
                        "weight": 1.0,
                        "source_ref": pe.get("source_text") or f"pending_edge:{pe['id']}",
                        "metadata": pe.get("metadata", {})
                    })
                
                if to_insert:
                    supabase.table("graph_edges").upsert(
                        to_insert,
                        on_conflict="source_node_id, relationship, target_node_id",
                        ignore_duplicates=True
                    ).execute()
                
                # Move the successfully processed approved rows to the archive
                pe_ids = [pe["id"] for pe in pe_res.data]
                if pe_ids:
                    # Supabase python client doesn't support complex insert-from-select,
                    # so we will use RPC or direct inserts if we fetch the full row.
                    # Since we only fetched partial, it's better to fetch full, insert to archive, then delete.
                    pass

            # --- Archive Sweep for all terminal states ---
            # Moves rows older than 24h that are terminal to pending_graph_edges_archive
            try:
                # Use RPC to do this cleanly: moving rows to archive
                supabase.rpc('archive_terminal_pending_edges').execute()
            except Exception as archive_err:
                audit_log_sync("sentinel", "WARNING", f"Archive sweep error: {archive_err}")
                
        except Exception as ge_err:
            audit_log_sync("sentinel", "WARNING", f"Graph integrity sweep error (non-critical): {ge_err}")

        await complete_pulse_run(supabase, run_id, status="completed",
            metadata={"alerted": alerted_count})
        return {"success": True, "alerted": alerted_count}

    except Exception as e:
        import traceback
        audit_log_sync("sentinel", "CRITICAL", f"Sentinel Critical Error: {e}")
        traceback.print_exc()
        await complete_pulse_run(supabase, run_id, status="failed", error_message=str(e))
        return {"error": str(e)}
