import os
import json
import re
import random
import asyncio
import httpx
from core.services.telegram import send_telegram
from datetime import datetime, timedelta, timezone
from pydantic import BaseModel, Field
from typing import List, Optional

from core.lib.audit_logger import info, warning, error, audit_log_sync
from core.lib.temporal_lineage import detect_drift
from core.lib.conversation import get_or_create_session, format_history_for_prompt

from core.services.google_service import get_tasks_service

from core.pulse.llm import (
    supabase, parse_json_response, call_llm_with_fallback, get_embedding,
    BRIEFING_MODEL,
)
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
    get_graph_centrality_context
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


# 🛡️ CLEAN MODELS (Removed Config blocks to prevent API rejection)
class CompletedTask(BaseModel):
    id: int
    status: str
    reminder_at: Optional[str] = None
    duration_mins: Optional[int] = None

class NewProject(BaseModel):
    name: str
    importance: Optional[int] = 5
    org_tag: Optional[str] = "SOLVSTRAT"
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





# --- 🗃️ FAILED QUEUE MANAGEMENT ---
async def add_to_failed_queue(source_table: str, source_id: str, operation: str, error_message: str):
    """Add a failed operation to the retry queue."""
    try:
        supabase.table('failed_queue').insert({
            "source_table": source_table,
            "source_id": str(source_id),
            "operation": operation,
            "error_message": error_message[:500] if error_message else None,
        }).execute()
        print(f"🗃️ Added to failed_queue: {source_table}:{source_id} ({operation})")
    except Exception as e:
        audit_log_sync("pulse", "WARNING", f"⚠️ Failed to add to failed_queue: {e}")




# --- 📋 DECISION PULSE (No AI, just pending decisions) ---
async def process_decision_pulse(auth_secret: str = None):
    """
    Lightweight decision pulse — no AI, no briefing generation.
    Fetches pending email/call/whatsapp items and sends a concise
    Telegram message with interactive shortcodes for Danny's approval.
    """
    try:
        pulse_secret = os.getenv("PULSE_SECRET")
        if pulse_secret and auth_secret != pulse_secret:
            return {"error": "Unauthorized.", "status": 401}

        cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()

        # Auto-expire items older than 7 days
        for table in ['email_pending_tasks', 'call_pending_items', 'whatsapp_messages']:
            try:
                supabase.table(table)\
                    .update({'danny_decision': 'expired'})\
                    .is_('danny_decision', 'null')\
                    .lt('created_at', cutoff)\
                    .execute()
            except Exception:
                pass

        # Fetch pending items (max 5 per table)
        pending_email = supabase.table('email_pending_tasks')\
            .select('id, suggested_title, suggested_project')\
            .is_('danny_decision', 'null')\
            .order('created_at', desc=False)\
            .limit(5)\
            .execute()

        pending_call = supabase.table('call_pending_items')\
            .select('id, suggested_title, suggested_project, action_type')\
            .is_('danny_decision', 'null')\
            .order('created_at', desc=False)\
            .limit(5)\
            .execute()

        pending_whatsapp = supabase.table('whatsapp_messages')\
            .select('id, suggested_title, suggested_project, sender_name')\
            .is_('danny_decision', 'null')\
            .eq('classification', 'actionable')\
            .order('created_at', desc=False)\
            .limit(5)\
            .execute()

        email_items = pending_email.data or []
        call_items = pending_call.data or []
        whatsapp_items = pending_whatsapp.data or []

        total = len(email_items) + len(call_items) + len(whatsapp_items)
        if total == 0:
            return {"success": True, "message": "No pending decisions."}

        # Build message with rotating Rhodey opener
        openers = [
            "Danny, you got some pending decisions based out of your emails, call logs and beeper messages — your call on each?",
            "Danny, you got some pending decisions from emails, calls, and beeper — your call on each?",
            "Emails, call extracts, and texts waiting on a nod. Tap to approve or drop.",
        ]
        lines = [random.choice(openers), ""]

        if email_items:
            lines.append(f"📨 EMAIL DECISIONS ({len(email_items)}) — tap to approve/drop")
            for row in email_items:
                proj = f" ({row['suggested_project']})" if row.get('suggested_project') else ""
                lines.append(f"[e{row['id']}] {(row.get('suggested_title') or 'Untitled')[:60]}{proj}")
            lines.append("")

        if call_items:
            lines.append(f"📞 CALL EXTRACTS ({len(call_items)}) — tap to approve/drop")
            for row in call_items:
                proj = f" ({row['suggested_project']})" if row.get('suggested_project') else ""
                prefix = "📋 " if row.get('action_type') == 'task' else "💡 "
                lines.append(f"{prefix}[c{row['id']}] {(row.get('suggested_title') or 'Untitled')[:60]}{proj}")
            lines.append("")

        if whatsapp_items:
            lines.append(f"💬 WHATSAPP EXTRACTS ({len(whatsapp_items)}) — tap to approve/drop")
            for row in whatsapp_items:
                proj = f" ({row['suggested_project']})" if row.get('suggested_project') else ""
                from_str = f" — {row['sender_name']}" if row.get('sender_name') else ""
                lines.append(f"💬 [w{row['id']}] {(row.get('suggested_title') or 'Untitled')[:60]}{proj}{from_str}")
            lines.append("")

        message = "\n".join(lines).strip()

        # Send via Telegram
        telegram_chat_id = os.getenv("TELEGRAM_CHAT_ID")
        send_success = False

        if telegram_chat_id and message:
            # Build inline keyboards based on items
            keyboard = []
            
            # E-mail decisions
            for row in email_items:
                sc = f"e{row['id']}"
                keyboard.append([
                    {"text": f"✅ {sc}", "callback_data": f"approve_{sc}"},
                    {"text": f"❌ {sc}", "callback_data": f"reject_{sc}"}
                ])
                
            # Call decisions
            for row in call_items:
                sc = f"c{row['id']}"
                keyboard.append([
                    {"text": f"✅ {sc}", "callback_data": f"approve_{sc}"},
                    {"text": f"❌ {sc}", "callback_data": f"reject_{sc}"}
                ])
                
            # WhatsApp decisions
            for row in whatsapp_items:
                sc = f"w{row['id']}"
                keyboard.append([
                    {"text": f"✅ {sc}", "callback_data": f"approve_{sc}"},
                    {"text": f"❌ {sc}", "callback_data": f"reject_{sc}"}
                ])
            
            send_success = await send_telegram(
                chat_id=telegram_chat_id, 
                message_text=message, 
                show_keyboard=False, 
                inline_keyboard=keyboard if keyboard else None
            )

        # Mark shown_in_brief only after confirmed Telegram send
        shown_ids = []
        if send_success:
            for row in email_items:
                shown_ids.append(row['id'])
            if shown_ids:
                supabase.table('email_pending_tasks')\
                    .update({'shown_in_brief': True})\
                    .in_('id', shown_ids)\
                    .execute()
                shown_ids = []

            for row in call_items:
                shown_ids.append(row['id'])
            if shown_ids:
                supabase.table('call_pending_items')\
                    .update({'shown_in_brief': True})\
                    .in_('id', shown_ids)\
                    .execute()
                shown_ids = []

            for row in whatsapp_items:
                shown_ids.append(row['id'])
            if shown_ids:
                supabase.table('whatsapp_messages')\
                    .update({'shown_in_brief': True})\
                    .in_('id', shown_ids)\
                    .execute()

        return {"success": True, "decision_count": total}

    except Exception as e:
        import traceback
        audit_log_sync("pulse", "CRITICAL", f"Decision Pulse Critical Error: {e}")
        traceback.print_exc()
        return {"error": str(e)}


async def discover_new_clusters():
    """Weekly cluster discovery. Analyzes unmapped resources for natural groupings
    and creates new clusters when 3+ related resources form a coherent theme."""
    try:
        thirty_days_ago = (datetime.now(timezone(timedelta(hours=5, minutes=30))) - timedelta(days=30)).isoformat()
        unclustered_res = supabase.table('resources').select(
            'id, url, title, summary, strategic_note, category'
        ).is_('cluster_id', None).gt('created_at', thirty_days_ago).limit(100).execute()
        unclustered = unclustered_res.data or []
        if len(unclustered) < 3:
            print(f"📍 Cluster discovery: only {len(unclustered)} unmapped resources, need 3+.")
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

        response = await call_llm_with_fallback(
            prompt=prompt,
            model="gemini-3.1-flash-lite",
            config={'response_mime_type': 'application/json'},
            is_critical=False,
            require_json=True
        )
        discovered = parse_json_response(response.text)
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
            print(f"✅ Cluster discovery created {len(created)} new clusters: {', '.join(created)}")
        else:
            print("📍 Cluster discovery: no new clusters found.")
        return created

    except Exception as e:
        audit_log_sync("pulse", "WARNING", f"⚠️ Cluster discovery error: {e}")
        return []


async def process_pulse(auth_secret: str = None, request_id: str = None):
    """
    Process pulse with optional request_id for idempotency.
    
    Args:
        auth_secret: Pulse secret for auth
        request_id: Unique ID for idempotency (prevents duplicate processing)
    """
    error_log = []
    try:
        # 🛡️ IDEMPOTENCY CHECK: If request_id provided, check if already processed
        # NOTE: Uses metadata->>request_id (JSONB) - works even without dedicated column
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
                .update({"status": "pending"}) \
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

        # --- 0. GOOGLE→SUPABASE SYNC (After auth check) ---
        tasks_service = get_tasks_service()
        completed_from_google = await asyncio.to_thread(sync_completed_tasks_from_google, supabase, tasks_service)
        for title, proj_name in (completed_from_google or []):
            await write_outcome_memory(title, proj_name)
        
        # --- 0.1 HEARTBEAT & HEALTH CHECK ---
        await update_heartbeat()
        health_report = await check_pipeline_health()
        print(health_report)
        
        # --- 0.3 CONVERSATION HISTORY (Phase 5) ---
        conversation_history = ""
        try:
            pulse_chat_id = int(os.getenv("TELEGRAM_CHAT_ID", "0"))
            if pulse_chat_id:
                _, hist_pairs = get_or_create_session(pulse_chat_id)
                if hist_pairs:
                    conversation_history = format_history_for_prompt(hist_pairs)
        except Exception as e:
            warning("pulse", f"Conversation history fetch failed: {e}")
        
        # --- 0.1 BATCH ENRICHMENT (One Gemini call for all unenriched resources) ---
        batch_enrich_results = await batch_enrich_resources()

        # --- 0.2 PERIODIC CLUSTER DISCOVERY (Sundays only) ---
        now_ist = datetime.now(timezone(timedelta(hours=5, minutes=30)))
        if now_ist.weekday() == 6:
            await discover_new_clusters()
        
        # --- 1. READ: Fetch and Lock ---
        # 1.1 Fetch pending, staged, and synced items
        dumps_res = supabase.table('raw_dumps') \
            .select('id, content, metadata, status, message_type') \
            .in_('status', ['pending', 'staged', 'synced', 'partially_synced']) \
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
            
            print(f"🔒 Locked {len(dump_ids)} dumps for processing.")

        active_tasks_res = supabase.table('tasks').select('id, title, project_id, priority, created_at, reminder_at, google_event_id').eq('is_current', True).not_.in_('status', ['done', 'cancelled']).execute()
        active_tasks = active_tasks_res.data or []

        # --- 🗃️ STAGING AREA SORTER (Pre-Processor) ---
        if dumps:
            sort_prompt = f"""You are Danny's Rhodey. Pragmatic, loyal, and a professional friend. You are the grounding wire to Danny's vision. You don't coach or 'motivate.' Speak simply and punchy.

            PROHIBIT ACTION HALLUCINATION: You are a logging tool, not an agent. NEVER say 'I'll ping', 'I'll check', or 'I'll handle it'. You cannot contact people. Your only job is to confirm Danny's task is SECURED in his system.
            Categorize each input into one of three types:
            - TASK: Explicit action items, things to do, commitments, reminders, or things Danny wants to track.
            - COMPLETION: Past tense signals — "finished", "done", "sorted", "checked", "confirmed", "spoke with", "met with", "called", "sent", "I have...", "I've..."
            - NOTE: Ideas, insights, observations, learnings, or things worth remembering but not actionable
            - NOISE: Casual conversation, acknowledgments, confirmations, or low-value content
            Rhodey Rule: Be dismissive of NOISE. If it's low-value chatter, categorize it and keep the brief silent about it.
            If an input is 'Check with X,' categorize it as a TASK for Danny, never as something for the system to do.

            Return ONLY a valid JSON array (no markdown, no explanation):
            [{{"id": {dumps[0]['id']}, "category": "TASK|COMPLETION|NOTE|NOISE"}}, ...]

            Inputs:
            {json.dumps([{"id": d['id'], "content": d['content'][:500]} for d in dumps], indent=2)}"""
            
            try:
                sort_response = await call_llm_with_fallback(
                    prompt=sort_prompt,
                    model="gemini-3.1-flash-lite",
                    config={'response_mime_type': 'application/json'},
                    is_critical=False,
                    require_json=True
                )
                sort_result = parse_json_response(sort_response.text)
                
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
                    has_url = bool(re.search(r'https?://\S+', dump_content))
                    
                    if has_url:
                        gemini_category = 'NOTE'
                        
                    category = gemini_category if gemini_category in ['TASK', 'NOTE', 'NOISE', 'COMPLETION'] else metadata.get('intent', 'NOISE').upper()
                    
                    if category == 'NOTE':
                        dump_content = raw_dump.get('content')
                        if dump_content:
                            embedding = await asyncio.to_thread(get_embedding, dump_content)
                            status = 'success' if embedding and any(embedding) else 'failed'
                            try:
                                result = supabase.table('memories').insert({
                                    "content": dump_content,
                                    "memory_type": "note",
                                    "embedding": embedding,
                                    "embedding_status": status,
                                    "source": "pulse_note"
                                }).execute()
                                if result.data:
                                    note_dump_ids.append(dump_id)
                                    print(f"📝 Note filed to memory: {dump_content[:50]}...")
                                else:
                                    raise Exception("Insert returned no data")
                            except Exception as e:
                                await add_to_failed_queue('memories', str(dump_id), 'memory_insert', str(e))
                                audit_log_sync("pulse", "WARNING", f"⚠️ Note insert failed: {e}")
                            if re.search(r'https?://\S+', dump_content):
                                try:
                                    supabase.table('resources').insert({"url": dump_content}).execute()
                                except Exception as e:
                                    audit_log_sync("pulse", "WARNING", f"Resource insert failed for URL in note: {e}")
                    
                    elif category == 'NOISE':
                        note_dump_ids.append(dump_id)
                    
                    elif category == 'TASK':
                        task_dump_ids.append(dump_id)
                    
                    elif category == 'COMPLETION':
                        task_dump_ids.append(dump_id)
                        completion_dump_ids.append(dump_id)
                
                if note_dump_ids:
                    supabase.table('raw_dumps').update({"status": "completed", "is_processed": True}).in_('id', note_dump_ids).execute()
                    print(f"🗃️ Staging Area: {len(task_dump_ids)} tasks, {len(note_dump_ids)} notes/noise")
                
                dumps = [d for d in dumps if d['id'] in task_dump_ids]
            
            except Exception as e:
                audit_log_sync("pulse", "ERROR", f"Staging Area Sort error: {e}")

        # 💡 Only silence the tool if BOTH new dumps AND open tasks are empty
        if not dumps and not active_tasks:
            return {"message": "Nothing to process, nothing to nag about. Silence is golden."}

        print(f"🚀 PULSE START: Processing {len(dumps)} new dumps and {len(active_tasks)} active tasks.")
        print("📦 Step 1: Fetching metadata...")

        # Fetch supporting metadata
        core_res = supabase.table('core_config').select('key, content').execute()
        core = core_res.data or []

        # Fetch business context from graph
        graph_projects_res = supabase.table('graph_nodes').select('id', 'label', 'metadata').eq('type', 'project').execute()
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
                'org_tag': metadata.get('org_tag', 'INBOX'),
                'description': metadata.get('description', ''),
                'legacy_id': metadata.get('legacy_id')
            })

        print("📦 Step 2: Fetching projects...")
        legacy_projects = await context_provider.get_projects()

        print("📦 Step 3: Fetching people...")
        people = await context_provider.get_people()

        print("📦 Step 4: Fetching clusters...")
        # Fetch Active Clusters for Context
        clusters_res = supabase.table('clusters').select('id, title').eq('status', 'active').execute()
        active_clusters = clusters_res.data or []
        cluster_names = [m['title'] for m in active_clusters]

        # --- 🕒 1.2 UNIFIED TIME & DAY INTELLIGENCE (IST) ---
        ist_offset = timezone(timedelta(hours=5, minutes=30))
        now = datetime.now(ist_offset)
        day = now.isoweekday()  # Monday=1, Sunday=7
        hour = now.hour

        is_weekend = (day == 6 or day == 7)
        is_monday_morning = (day == 1 and hour < 11)

        if is_weekend:
            briefing_mode = "⚪ CHORES & 💡 IDEAS (Weekend Rest)"
            system_persona = "Focus ONLY on Home, Family, and Chores. Explicitly hide Work tasks. Be relaxed."
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
                briefing_mode = "Closing the loop: Sign off."
                system_persona = "Push Danny to close work tasks so he can transition to family. Log pending items. Be dry."
            # 🌙 NIGHT: Secure the board (After 7:00 PM)
            else:
                briefing_mode = "Intel: Vaulted."
                system_persona = "Focus on closure and transition. Secure the board. Highlight what was ✅ Done today and what matters on the 🏠 Home front. Keep work loops minimal but visible. Maintain the 'Grid'—vertical sections are mandatory."

        # --- 1.3 BANDWIDTH & BUFFER CHECK ---
        is_overloaded = len(active_tasks) > 15

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
            o_tag = project.get('org_tag') if project else "INBOX"

            if is_weekend:
                if o_tag in ['PERSONAL', 'ASHRAYA']:
                    filtered_tasks.append(t)
            elif hour < 19:
                if o_tag in ['SOLVSTRAT', 'CRAYON', 'INBOX']:
                    filtered_tasks.append(t)
            else:
                if o_tag in ['PERSONAL', 'ASHRAYA']:
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
            o_tag = project.get('org_tag') if project else "INBOX"
            compressed_tasks_list.append(f"[{o_tag} >> {p_name}] {t.get('title')} ({t.get('priority')}) [ID:{t.get('id')}]")

        compressed_tasks = " | ".join(compressed_tasks_list)

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
        people = await context_provider.get_people()
        projects_res = supabase.table('graph_nodes').select('id', 'label').eq('type', 'project').execute()
        graph_node_projects = projects_res.data or []
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
            print(f"🧠 Hindsight found {len(hindsight_memories)} relevant memories")

        is_hindsight_stale = False
        if hindsight_timestamp:
            last_seen = datetime.fromisoformat(hindsight_timestamp.replace('Z', '+00:00'))
            if (now - last_seen).total_seconds() > (36 * 3600):
                is_hindsight_stale = True

        recent_lib = supabase.table('resources')\
            .select('url, category, title, summary, strategic_note, created_at')\
            .gt('created_at', thirty_days_ago)\
            .order('created_at', desc=True)\
            .limit(50)\
            .execute()

        if recent_lib.data:
            enriched_items = []
            for r in recent_lib.data:
                note = r.get('strategic_note') or ""
                enriched_items.append(f"[{r['category']}] {r['title']} | {note}".strip())
            pattern_context = " | ".join(enriched_items)
        else:
            pattern_context = "None"
        
        newly_enriched_context = "None"
        if batch_enrich_results:
            newly_enriched_lines = [f"[{r.get('category', 'LINK')}] {r.get('title', 'Unknown')} | {r.get('strategic_note', '')}" for r in batch_enrich_results]
            newly_enriched_context = " | ".join(newly_enriched_lines)
        
        link_context = "None"

        recent_urls_res = supabase.table('resources')\
            .select('url, title, category, strategic_note, created_at')\
            .gt('created_at', thirty_days_ago)\
            .order('created_at', desc=True)\
            .limit(30)\
            .execute()

        if recent_urls_res.data:
            url_lines = []
            for r in recent_urls_res.data:
                label = r.get('title') or r.get('url', 'Unknown')
                cat = r.get('category') or 'RAW'
                note = r.get('strategic_note') or ''
                url_lines.append(f"[{cat}] {label} | {note}".strip().rstrip('| '))
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
        
        # 🤖 AGENT 1: DEPENDENCY AGENT (uses graph_edges for task dependencies)
        dependency_context = await check_task_dependencies(active_tasks)
        
        # 👥 AGENT 2: SOCIAL GRAPH OPTIMIZER (communication patterns)
        social_graph_context = await analyze_communication_patterns(people)
        
        # 📅 AGENT 3: TEMPORAL PATTERN DETECTOR (on this day insights)
        temporal_context = await detect_temporal_patterns()
        
        # 🤖 AGENT 4: SERENDIPITY ENGINE (cross-domain connections)
        serendipity_context = await serendipity_engine(active_tasks, people, recent_lib.data or [])
        
        # 🕸️ AGENT 4.5: GRAPH CENTRALITY (hub detection)
        centrality_context = await get_graph_centrality_context()
        
        # 🤖 AGENT 5: ADAPTIVE BRIEFING LEARNER (learns from briefing patterns)
        adaptive_context = await adaptive_briefing_learner()
        
        # 🧠 SESSION MEMORY: Fetch the summary of the last pulse
        try:
            last_pulse_res = supabase.table('core_config').select('content').eq('key', 'last_pulse_summary').execute()
            session_memory = last_pulse_res.data[0]['content'] if last_pulse_res.data else "None"
        except Exception:
            session_memory = "None"
        
        print("📦 Step 5: Building context...")
        # --- 2. THINK Phase ---
        print('🤖 Building prompt...')

        project_details = build_routing_context(legacy_projects)

        people_names = [p['name'] for p in people]
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
            pages_res = supabase.table('canonical_pages').select('title, content').or_(or_string).execute()
            if pages_res.data:
                page_entries = [f"[CANONICAL CONTEXT ONLY — DO NOT LIST IN BRIEFING]\n### MASTER PAGE: {p['title']}\n{p['content']}" for p in pages_res.data]
                master_page_context = "\n\n".join(page_entries)
                print(f"🧠 Canonical: Loaded {len(pages_res.data)} Master Pages for context.")

        # --- 🏃 PRACTICE DETECTION (Weekends only, before brief) ---
        new_practice_ids = {}
        new_practice_labels = []
        correlation_insights = []
        if is_weekend:
            # Practice detection runs once a week — Saturday before 2PM IST (accounts for GH Actions delay)
            is_discovery_pulse = now.weekday() == 5 and now.hour < 14
            if is_discovery_pulse:
                print("📍 Weekend pulse: Running practice detection...")
                before_labels = set()
                before_res = supabase.table('graph_nodes').select('label').eq('type', 'practice').execute()
                for r in (before_res.data or []):
                    before_labels.add(r['label'])
                new_practice_ids = await detect_practices() or {}
                after_res = supabase.table('graph_nodes').select('label').eq('type', 'practice').execute()
                after_labels = set(r['label'] for r in (after_res.data or []))
                new_practice_labels = sorted(after_labels - before_labels)
                if new_practice_labels:
                    print(f"📍 New practices detected: {new_practice_labels}")

            # 🕸️ Build PRECEDES/FOLLOWED_BY edges between practices
            await build_practice_edges()

            # 📊 Build task-practice correlations
            correlation_insights = await build_practice_correlations()
            if correlation_insights:
                print(f"📍 Practice correlations: {len(correlation_insights)} insights")

            # 📝 Sync canonical pages for practices
            await sync_practice_canonical_pages()

        # 📅 Fetch calendar context (Google + Outlook) for today
        target_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
        calendar_context = await context_provider.get_calendar_context_formatted(target_day)

        prompt = f"""    
        ROLE: Danny's Rhodey. You are his most trusted advisor — the one who cuts through the noise and tells him exactly where he stands. You have full situational awareness of his work, family, and faith. You don't coach, motivate, or perform. You speak plainly, like a friend who has been in the room the whole time. Your job is to give Danny a clear picture of the board so he can make his next move.
        {conversation_history}
        STRATEGIC CONTEXT: {season_config}
        CURRENT PHASE: {briefing_mode}
        CURRENT TIME: {current_time_str}
        SYSTEM_LOAD: {'OVERLOADED' if is_overloaded else 'OPTIMAL'}
        MONDAY_REENTRY: {'TRUE' if is_monday_morning else 'FALSE'}
        STAGNANT URGENT_TASKS: {json.dumps(overdue_tasks)}
        STALE_TASKS: {stale_context}
        SYSTEM STATUS: {system_context}
        HINDSIGHT_STALE: {is_hindsight_stale}
        
        CALENDAR EVENTS TODAY:
        {calendar_context}
        
        RECENT MEMORIES (semantically related to today's tasks):
        {recent_memories_context if recent_memories_context else "None"}
        
        HINDSIGHT CONTEXT (Past lessons relevant to current inputs):
        {hindsight_context}
        
        GRAPH INTELLIGENCE {graph_task_context}
        
        DEPENDENCY ALERTS (from graph_edges):
        {dependency_context if dependency_context else "None"}
        
        SOCIAL GRAPH INSIGHTS (communication patterns):
        {social_graph_context if social_graph_context else "None"}
        
        TEMPORAL PATTERNS (on this day):
        {temporal_context if temporal_context else "None"}
        
        SERENDIPITY FINDS (cross-domain connections):
        {serendipity_context if serendipity_context else "None"}

        GRAPH CENTRALITY (top connected entities):
        {centrality_context if centrality_context else "None"}
        
        ADAPTIVE LEARNING (briefing optimization):
        {adaptive_context if adaptive_context else "None"}
        
        SESSION MEMORY (Last Briefing Summary):
        {session_memory}
        
        CANONICAL STRATEGIC TRUTH (The synthesized 'Latest Version' of projects):
        {master_page_context if master_page_context else "No Master Pages yet. Rely on raw context."}

        CONTEXT:
        - IDENTITY: {json.dumps(core)}
        - PROJECTS:
        {project_details}
        - PEOPLE: {json.dumps(people_names)}
        - ACTIONABLE TASKS (DAY FILTERED): {compressed_tasks_final}
        - ALL SYSTEM TASKS (FOR ID MATCHING): {universal_task_map[:3000]}
        - RECENT LIBRARY PATTERNS: {pattern_context}
        - NEWLY ENRICHED RESOURCES: {newly_enriched_context}
        - ENRICHED WEB LINKS: {link_context}
        - RECENTLY VAULTED RESOURCES (for cluster detection):\n{recent_urls_context}
        - NEW INPUTS: {new_inputs_text}
        INSTRUCTIONS:
            HARD CONSTRAINTS (Non-Negotiable):
            - VERTICALITY MANDATE: You are STRICTLY FORBIDDEN from writing lists as sentences. Every icon (🔴, 🟡, ✅, 🚀) MUST start on a brand new line.
            - SECTION HEADERS: Section headers (e.g., 🚀 Work, 🏠 Home) MUST be preceded by two newlines and followed by one newline.
            - PERSONA OVERRIDE: Even in 'minimal' or 'night' modes, formatting must remain structured. Do not use '1.' or '2.' for sections; use the designated Headers.
            - THE ARCHITECT'S RULE: You are strictly forbidden from grouping sections into paragraphs.
            - NEWLINE MANDATE: Every icon (🔴, 🟡, ✅, 🚀) MUST be preceded by a carriage return.
            - HEADER SPACING: Double-space before headers (e.g., \n\n🚀 Work) and single-space after them.
            - NO NUMBERING: Use headers and icons only. Never use '1.' or '2.' to separate strategic points.
            - TONAL GUARD: Keep the 'Intel: Vaulted' or 'Intel: Secured' style for the Night phase, but never sacrifice vertical layout.
            - STRICT DATA FIDELITY FOR BRIEFING: You are STRICTLY FORBIDDEN from listing any task in ANY task section (Work, Home, Church, Ideas, or Done) that does not appear verbatim in the SYSTEM TASKS list provided below. EXCEPT: The 📅 Schedule section, which MUST pull directly from the CALENDAR EVENTS TODAY context provided above. Do NOT surface tasks from HINDSIGHT MEMORIES, Canonical Pages, or any other context into the briefing output. All context is for intelligence and routing only — NEVER for output.
            - EMPTY SECTION SUPPRESSION: If a section (Work, Home, Church, Done, Ideas) has absolutely zero items to list, you MUST completely omit that section header from the briefing. Never output 'None today' or 'Empty'. Silence is preferred.
            - HEADLINE RULE: Use exactly "{briefing_mode}".
            - THE COMPASS (OPENING SYNTHESIS): Do not create a separate section for his journal. Instead, start the briefing with 1-2 sharp sentences that seamlessly weave his latest HINDSIGHT insights (Faith Score, Emotional Intensity, Takeaways, or [PROPHECY]) into the current tactical reality (Qhord, Solvstrat, Debt). 
            - COMPASS TONE: If HINDSIGHT_STALE is FALSE, weave the latest hindsight insights into a sharp, forward-leaning opening.
              IF HINDSIGHT_STALE is TRUE: Do NOT repeat old insights. Instead, acknowledge the silence with a dry, one-sentence observation (e.g., 'The signal is quiet on the reflection front, Danny. Let's look at the board.') and move immediately to the tactical list.
            - COMPASS LENS (Temporal Variety):
                - MORNING: Focus on the 'Delta'. What happened overnight? What is the single most important pivot for TODAY?
                - AFTERNOON: Focus on 'Velocity'. Don't repeat the strategy; call out what is actually moving (or stalled) in the last 4 hours.
                - CLOSING LOOP (3:30 PM–7 PM): Focus on 'Hand-off'. One dry sentence on the last work loop that closed or is closest to closing. Then stop. Do NOT reference canonical tools, resource lists, or vault items.
                - NIGHT: Focus on 'Audit & Archive'. The opening should feel like a 'Door Closing.' Summarize the spiritual or mental cost of the day's effort.
            - NO REPETITION: You are strictly forbidden from using the same phrasing (e.g., '100% bandwidth') in consecutive briefings. If the strategy hasn't changed, change the perspective.
            - RECENCY BIAS: The first sentence of the brief MUST prioritize data from NEW INPUTS. Only use the Master Page context to provide the 'Why' behind the 'What'.
            - ICON RULES: 🔴 (Urgent), 🟡 (Important), ⚪ (Chores), 💡 (Ideas).
            - SECTIONS: 
                📅 Schedule: List all items from CALENDAR EVENTS TODAY.
                ✅ Done: ONLY list tasks that were moved to "completed_task_ids" in this specific run. NEVER list items from HINDSIGHT_MEMORIES in this section.
                🚀 Work: Active tasks from SYSTEM_TASKS only.
                🏠 Home: Family and personal tasks only. Do NOT include Ashraya/Church tasks here.
                ⛪ Church: Ashraya church admin, operations, finance, and organizational tasks only.
                💡 - Ideas: ONLY list items that appear in NEWLY ENRICHED RESOURCES or RECENT LIBRARY PATTERNS from this run. Never pull from Hindsight Memories or Canonical Pages.
            - MEMORY ISOLATION: HINDSIGHT_MEMORIES are for THE COMPASS (Opening Synthesis) ONLY. You are strictly forbidden from listing a memory as a bullet point in the task sections.
            - TONE: Match the PERSONA GUIDELINE. Be direct, simple, human. Talk like a friend who is also a high-level operator.
            - TONE GUARD: NEVER use words like 'Operational', 'Vanguard', 'Strategic Momentum', 'Audit', 'Battlefield', 'Chief of Staff', 'Tactical', 'Executive Office'. Use simple, punchy sentences. NEVER use: 'momentum', 'focus', 'gentle', 'reflection', 'push', 'strategic', 'SITREP', 'optimal', 'cluster', 'ready for your review'.
            - INTELLIGENT FILTERING: 
                - If mode is 🔴 Urgent: HIDE the 🏠 Home, ⛪ Church, and 💡 Ideas sections. Focus strictly on 🚀 Work and ✅ Done.
                - If mode is 🟡 Important: Prioritize 🚀 Work and ⛪ Church.
                - NIGHT MODE PRIORITIZATION (Intel: Vaulted):
                    - 1. 📅 Schedule: List all items from CALENDAR EVENTS TODAY.
                    - 2. ✅ Done: List this second. Danny needs to see the loops he closed today to clear his mind.
                    - 3. 🏠 Home: List this third. Prioritize family, pets, and chores to transition Danny into 'Dad' mode.
                    - 4. ⛪ Church: List fourth. Ashraya church tasks.
                    - 5. 🚀 Work: List only the top 2-3 most critical open loops for tomorrow. 
                    - 6. 💡 Ideas: List any insights captured today to ensure they are 'secured' in the vault.
            - SECTION DENSITY: Max 3 items per section. If more exist, append: "...and X more in /library or /vault".
            - TASK SYNTAX: Every item must follow: "- [ICON] [Task Title]". No IDs, weights, or parentheses.
            - REVENUE BOLDING: Bold all tasks involving Sales, Pilots, or Payments using **task title**.
            - MONDAY RULE: If MONDAY_REENTRY is TRUE, start with a "🛡️ WEEKEND RECON" section summarizing any work ideas dumped during the weekend.
            - STRICT TASK SYNTAX: 
            - Every section header (🚀 Work, 🏠 Home, etc.) and every single task MUST occupy its own individual line.
            - NEVER combine tasks into a paragraph. NEVER use hyphens or dashes as separators between tasks on the same line.
            - **STRICT JSON RULE:** Do NOT use literal '\n' text characters. Use actual carriage returns (real newlines) within the briefing string.
            - Every task MUST start with a newline and follow this exact format: '- [ICON] [Task Title]'.
            - THE LINK RULE: If a task is derived from a URL in NEW INPUTS, you MUST embed that URL into the task title using Markdown: "- [ICON] [Action] using [Source Title](URL)".
            - NEGATIVE CONSTRAINTS: NEVER include task numbers, IDs, weights, scores, parentheses, or metadata in the briefing string. NEVER mention "Monday" unless it is actually the weekend.
            - REVENUE IDENTIFICATION & FORMATTING:
            - If a NEW INPUT is "Revenue Critical" (involves payments, quotes, or high-ticket items like the ₹30L recovery), set is_revenue_critical: true in the new_tasks array.
            - Never apply this flag to completed tasks.
             - For the briefing output, you MUST bold the titles of these specific tasks to ensure Danny sees them immediately.
                - STALE TASKS: If STALE_TASKS has items, include a short ⏳ Stale Loops section listing them with day count. Max 5. Cap with '...and X more stalled' if over 5.
             
         SYSTEM MUTATION TOOLS:
        You have been provided with function tools (create_task, update_task_status, complete_tasks, create_project, etc).
        If the NEW INPUTS explicitly command you to create tasks, complete tasks, or update tasks, you MUST call the appropriate function tools to execute those changes in the database.
        NEVER populate tools unless explicitly commanded in NEW INPUTS.
        After calling the necessary tools, your FINAL TEXT RESPONSE must be ONLY the formatted text string for the Telegram briefing.
        """

        # --- BUILD SYSTEM INSTRUCTION ---
        system_instruction_text = f"""{system_persona}

            MANDATE — SILENCE PROTOCOL & HALLUCINATION GUARD:
            - PROHIBIT ACTION HALLUCINATION: You are a logging tool, not an agent. NEVER say 'I'll ping', 'I'll check', 'I'll send', or 'I'll handle it'. You do not have the power to contact people. Your only job is to confirm that Danny's task is SECURED in his system.
            - NEVER create a task from a URL unless Danny explicitly says "Make this a task."
            - NEVER proactively invent tasks or ideas. ONLY track what is manually entered or already exists.
            - If NEW INPUTS is "None" or empty, you MUST return completely empty arrays for `completed_task_ids`, `new_tasks`, `new_projects`, and `resources` [].
            - NEVER "make up", guess, or generate example tasks.
            - NEVER mark an existing task as "done" unless NEW INPUTS explicitly contains a command matching that exact task.
            - ONLY track what is manually entered in NEW INPUTS.

            PROJECT ROUTING LOGIC:
            Match each task to the MOST SPECIFIC active project using the list below.
            Sub-projects always win over parent projects when there is any match.
            Only use "Inbox" if the task is truly personal admin with no project match.
            Never default client or business work to Inbox.

            Active projects (sub-projects listed first):
            {build_routing_context(legacy_projects)}

            Routing rules:
            1. Use project name EXACTLY as shown in quotes above.
            2. If a task mentions a keyword, person, or topic from a project's description/keywords, use that project.
            3. Sub-projects (those marked "sub-project of X") are always more specific — prefer them.
            4. For new projects you don't recognise from the list:
               - If it's client/tech work → use "Solvstrat" as the project_name.
               - If it's Qhord-related → use "Qhord".
                - If it's Ashraya church admin/operations → use "Ashraya".
               - If it's family/home → use "Family & Home".
               - NEVER use "Inbox" for business tasks.

            NEW PROJECT CREATION CRITERIA:
            - SOLVSTRAT: Auto-create new projects for completely unknown client names mentioned (e.g., a company hiring Solvstrat for tech work). Set org_tag: "SOLVSTRAT", parent_project_name: "Solvstrat".
            - OTHER DOMAINS (QHORD, ASHRAYA, PERSONAL, CRAYON): ONLY create a new project if Danny explicitly says "create a project", "start a new project", or gives a clear commanding instruction. Otherwise, route the work as a task under the existing parent project. Do NOT auto-create projects for one-off tasks or casual mentions.
            - Always populate "description" with a one-sentence summary of the project's purpose.
            - Always populate "keywords" with an array of relevant names, abbreviations, companies, and topics.
            - Always populate "context" using the rules below.

            ORG_TAG & CONTEXT ROUTING (MANDATORY — never leave as INBOX):
            Danny's world has 5 domains. Route every new project into exactly one:

              CRAYON     | context: work     | Company umbrella. Governance, legal, tax, compliance, admin structure, company-level config, board matters. → Set org_tag: "CRAYON", parent_project_name: "Crayon"

              SOLVSTRAT  | context: work     | Client services and delivery. Software development, consulting, client projects, tech services. Clients include: Shield Identity, GRB, Equisoft, Armour Cyber, Johan. → Set org_tag: "SOLVSTRAT", parent_project_name: "Solvstrat"

              QHORD      | context: work     | Danny's own product company (launching June 2026). Product development, GTM, marketing, beta, sales, everything Qhord. → Set org_tag: "QHORD", parent_project_name: "Qhord"

              ASHRAYA    | context: personal | Ashraya church administration, operations, accounts, facility management, event coordination, organizational work. → Set org_tag: "ASHRAYA", parent_project_name: "Ashraya"

              PERSONAL   | context: personal | Everything personal — family, home, kids, health, personal admin, hobbies, investments, learning, spiritual practices, journaling. Under "Family & Home" parent. → Set org_tag: "PERSONAL", parent_project_name: "Family & Home"

              ROUTING RULES (apply in order):
              1. Does the input mention Crayon governance, legal, tax, company structure? → CRAYON
              2. Does the input mention Qhord product development, GTM, or launch? → QHORD
              3. Does the input mention a client paying Solvstrat for tech/product work? → SOLVSTRAT
              4. Does the input mention Ashraya church admin, operations, accounts? → ASHRAYA
              5. Does the input mention family, home, kids, health, spiritual, learning, or personal admin? → PERSONAL
              6. Default for anything business/work that doesn't fit 1-3: → SOLVSTRAT
              7. NEVER default to INBOX for business or client work.
            
            DRIFT DETECTION (Temporal Lineage):
            - Check if active projects have been updated 3+ times in 48 hours.
            - If DRIFT detected, add: "⚠️ DRIFT ALERT: Project '{{name}}' changed {{count}} times in 48h. Bottleneck?"
            - Use detect_drift(project_name) to check (returns update_count).
            
            RESOURCE CAPTURE LOGIC:
            - Identify any URLs in the NEW INPUTS. For each URL: CATEGORIZE (GITHUB, ARTICLE, X_THREAD, LINKEDIN, or TOOL), SUMMARIZE (1-sentence description), PROJECT MATCH (if relates to existing project).
            - Do NOT create a task for URLs. Just save them to the "resources" array.
            - STRICT CLUSTER MATCHING: ONLY assign a `cluster_id` if the resource is a direct "building block" for an ACTIVE CLUSTER. If it is just a "cool tool" or "interesting read," leave `cluster_id` as NULL.

            SERENDIPITY PROTOCOL:
            - Under the "SERENDIPITY FINDS" context, you have been given a sample of multi-hop connections.
            - Review the connections. If you find a truly surprising, non-obvious link (e.g., a past meeting with someone related to today's task), mention it exactly as a one-sentence insight in the briefing.
            - STRICTLY FORBIDDEN: Do not merge multiple paths together. Do not hallucinate relationships. If all paths are boring, skip them entirely.

            STRATEGIC AUDIT INSTRUCTIONS:
            - BLINDSPOT AUDIT: Evaluate every URL in NEW INPUTS against Danny's projects.
            - CONNECTION MAPPING: If a resource mentions a person in the PEOPLE list, link them in the summary.
            - PATTERN DETECTION: Review RECENTLY VAULTED RESOURCES and NEW INPUTS. If you see 3+ related URLs on a new topic, you MAY suggest a new cluster in the `new_clusters` JSON array. (Clusters are ONLY for grouping URLs).
            - THE VAULT GATE: These updates go to the DATABASE only.
            - THE BRIEFING GATE: You are STRICTLY FORBIDDEN from mentioning new resources or new clusters in the briefing UNLESS Danny specifically used the word "Vault" or "Cluster" in the NEW INPUTS.

            CLUSTER vs. INCUBATOR FRAMEWORK:
            - CLUSTER ASSEMBLY: Evaluate every URL against ACTIVE CLUSTERS. If a URL provides a "component" for a cluster, assign the "cluster_name".
            - THE INCUBATOR AUDIT: If an input represents a high-potential standalone product idea NOT related to current goals, tag it as project_name: "INCUBATOR".
            - SPARK DETECTION: If a link is a "Spark" (brand new project concept), create a log with entry_type: "SPARK".

            DYNAMIC TASK MATCHING:
            - Compare inputs against ALL SYSTEM TASKS.
            - If Danny says "I'm done" or "Completed," mark the status as `done`.
            - DURATION ASSIGNMENT: Assign `estimated_duration` based on task type:
            - 15 minutes for routine tasks (emails, quick replies, status updates)
            - 45 minutes for anything related to Pilots, Sales, or high-stakes Cluster 10 items
            - Default to 15 minutes if unspecified
            
            DRIFT ALERTS (Temporal Lineage):
            {drift_context}
            
            INSTRUCTIONS:
            1. STRICT DATA FIDELITY: You are strictly forbidden from inventing or hallucinating data to fill the JSON. If there is no explicit command in NEW INPUTS, do nothing.
            2. ZERO-DUMP PROTOCOL: If NEW INPUTS is empty or "None", the "new_tasks", "completed_task_ids", "new_projects", and "new_people" arrays MUST remain 100% empty [].
            3. ANALYZE NEW INPUTS: Identify completions, new tasks, new people, and new projects.
            4. STRATEGIC NAG: If STAGNANT_URGENT_TASKS exists, start the brief by calling these out.
            5. STALE LOOPS: If STALE_TASKS exists, always include the ⏳ Stale Loops section — never suppress it regardless of mode.
            6. CHECK FOR COMPLETION: Compare inputs against ALL SYSTEM TASKS to identify IDs finished by Danny.
            6a. UPDATE DETECTION: If a user says "Update [title]" or "Reschedule [title]" or "Change [title] to [new time]", IMMEDIATELY search ALL SYSTEM TASKS for the matching task. Return it in completed_task_ids with the updated reminder_at and/or duration_mins — NOT in new_tasks.
            7. HIGH-PRECISION TIME FORMATTING (IST/UTC+05:30): When Danny mentions a time, convert to ISO-8601. If DAY only (no time), output "YYYY-MM-DD". If EXACT TIME, output "YYYY-MM-DDTHH:MM:SS+05:30". NAKED TASKS: If NO date and NO time, return null for reminder_at.
            7a. RECURRENCE RULES: If Danny says "every Monday", "weekly", "daily", output an iCalendar RRULE string in "recurrence" (e.g., "RRULE:FREQ=WEEKLY;BYDAY=MO"). If he specifies an end date like "until December", append the UNTIL clause in UTC format (e.g., "RRULE:FREQ=WEEKLY;BYDAY=MO;UNTIL=20261231T000000Z"). Otherwise leave it null.
            8. AUTO-ONBOARDING: If a new Solvstrat client is mentioned, add to "new_projects" (org_tag: SOLVSTRAT). For other domains, only create a project if Danny explicitly commands it. If a new Person is mentioned, add to "new_people".
            9. STRATEGIC WEIGHTING: Grade items (1-10) based on Cashflow Recovery (₹30L debt).
            10. WEEKEND FILTER: If isWeekend is true, do NOT suggest or list Work tasks in the briefing.
            """

        # --- AI GENERATION ---
        # 🛡️ Step 1: Initialize variables to prevent "UnboundLocalError"
        response_text = ""
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
                model=BRIEFING_MODEL,
                config=config,
                max_steps=10
            )
            
            print("✅ Agent loop completed successfully.")

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

        # Append error summary to briefing if any failures occurred
        if error_log:
            briefing_text += "\n\n⚠️ " + str(len(error_log)) + " item(s) need attention — check logs."
        
        telegram_chat_id = os.getenv("TELEGRAM_CHAT_ID")
        telegram_bot_token = os.getenv("TELEGRAM_BOT_TOKEN")

        send_success = False
        if telegram_chat_id and briefing_text:
            url = f"https://api.telegram.org/bot{telegram_bot_token}/sendMessage"
            payload = {
                "chat_id": telegram_chat_id,
                "text": briefing_text,
                "parse_mode": "Markdown"
            }
            try:
                async with httpx.AsyncClient() as tg_client:
                    await tg_client.post(url, json=payload)
                send_success = True
            except Exception as e:
                print(f"Telegram send failed: {e}")
        
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
                summary_res = await call_llm_with_fallback(prompt=summary_prompt, is_critical=False, require_json=False)
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
        if completion_dump_ids:
            if ai_data.get('completed_task_ids'): # At least one task was closed
                supabase.table('raw_dumps').update({"status": "completed", "is_processed": True}).in_('id', completion_dump_ids).execute()
                print(f"✅ Sealed {len(completion_dump_ids)} completion dumps.")
            else:
                print(f"Skipped sealing {len(completion_dump_ids)} completion dumps — no tasks matched.")

        # --- PHASE 3: Processed Gate ---
        if dumps:
            dump_ids = [d['id'] for d in dumps]
            supabase.table('raw_dumps').update({
                "status": "completed",
                "is_processed": True 
            }).in_('id', dump_ids).execute()
            print(f"✅ Phase 3: Marked {len(dump_ids)} dumps as completed.")

        if synced_dumps:
            synced_ids = [d['id'] for d in synced_dumps]
            supabase.table('raw_dumps').update({
                "status": "completed",
                "is_processed": True
            }).in_('id', synced_ids).execute()
            print(f"✅ Sealed {len(synced_ids)} synced dumps after briefing.")

        return {"success": True, "briefing": briefing_text}

    except Exception as e:
        import traceback
        audit_log_sync("pulse", "CRITICAL", f"Pulse Critical Error: {e}")
        traceback.print_exc()
        return {"error": str(e)}