import os, json, re, asyncio, httpx, hashlib
from datetime import datetime, timedelta, timezone
from pydantic import BaseModel, Field
from typing import List, Optional

from core.lib.audit_logger import info, warning, error, audit_log_sync
from core.lib.people_utils import normalize_person_name, is_blocklisted_person
from core.lib.temporal_lineage import detect_drift
from core.lib.conversation import get_or_create_session, format_history_for_prompt

from core.services.db import versioned_update
from core.services.google_service import get_tasks_service, format_rfc3339, delete_calendar_event, sync_to_google

from core.pulse.llm import (
    supabase, parse_json_response, call_llm_with_fallback, get_embedding,
    BRIEFING_MODEL, is_already_in_email_queue,
)
from core.pulse.utils import format_error, get_project_name, build_routing_context, normalize_mission_title
from core.pulse.memory import (
    write_outcome_memory, get_recent_memories_for_briefing,
    detect_temporal_patterns, serendipity_engine, adaptive_briefing_learner,
    retrieve_hindsight_memories, generate_after_action_report,
)
from core.pulse.graph import (
    write_graph_edges_for_task, check_task_dependencies,
    analyze_communication_patterns, fetch_hybrid_graph_context, fetch_graph_task_context,
)
from core.pulse.pipeline import update_heartbeat, check_pipeline_health
from core.pulse.calendar import (
    get_calendar_context, check_conflict, sync_to_calendar,
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
    mission_name: Optional[str] = None
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
    new_missions: List[str] = Field(default_factory=list)
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
        
        # --- 0.2 CONVERSATION HISTORY (Phase 5) ---
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
        
        # --- 1. READ: Fetch and Lock ---
        # 1.1 Fetch pending, staged, and synced items
        dumps_res = supabase.table('raw_dumps') \
            .select('id, content, metadata, status') \
            .in_('status', ['pending', 'staged', 'synced']) \
            .execute()

        all_dumps = dumps_res.data or []

        synced_dumps = [d for d in all_dumps if d.get('status') == 'synced']
        dumps = [d for d in all_dumps if d.get('status') != 'synced']

        completion_dump_ids = []
        
        if dumps:
            dump_ids = [d['id'] for d in dumps]
            
            # 🔒 THE LOCK: Immediately claim these for processing
            update_data = {"status": "processing"}
            if request_id:
                # Store request_id in metadata for idempotency
                for d in dumps:
                    try:
                        raw_meta = d.get('metadata', {})
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
                    except:
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
                    model="gemini-3.1-flash-lite-preview",
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
                                if result.data:  # Only add to note_dump_ids if insert succeeded
                                    note_dump_ids.append(dump_id)
                                    print(f"📝 Note filed to memory: {dump_content[:50]}...")
                                else:
                                    raise Exception("Insert returned no data")
                            except Exception as e:
                                # Add to failed_queue for retry
                                await add_to_failed_queue('memories', str(dump_id), 'memory_insert', str(e))
                                audit_log_sync("pulse", "WARNING", f"⚠️ Note insert failed: {e}")
                    
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
                except:
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
        projects_res = supabase.table('projects') \
            .select('id, name, org_tag, description, parent_project_id, status, keywords') \
            .eq('status', 'active') \
            .execute()
        legacy_projects = projects_res.data or []

        print("📦 Step 3: Fetching people...")
        people_res = supabase.table('people').select('name, strategic_weight').execute()
        people = people_res.data or []

        print("📦 Step 4: Fetching missions...")
        # Fetch Active Missions for Context
        missions_res = supabase.table('missions').select('id, title').eq('status', 'active').execute()
        active_missions = missions_res.data or []
        mission_names = [m['title'] for m in active_missions]

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
            except:
                recent_tasks.append(t) # Safety fallback

        # This is the AI's "Visual Field"
        universal_task_map = " | ".join([f"[ID:{t.get('id')}] {t.get('title')}" for t in recent_tasks])

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
                try: meta = json.loads(meta)
                except: meta = {}
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
        people_res = supabase.table('people').select('id, name').execute()
        people = people_res.data or []
        projects_res = supabase.table('graph_nodes').select('id', 'label').eq('type', 'project').execute()
        graph_node_projects = projects_res.data or []
        if people and active_tasks:
            graph_task_context = await fetch_graph_task_context(people, active_tasks)
        else:
            graph_task_context = ""

        # --- 📦 HINDSIGHT: Graph-first, then vector ---
        graph_context = await fetch_hybrid_graph_context(people, graph_node_projects, task_inputs)

        # Extract entity terms from people + projects for seeded vector search
        all_entity_terms = [p['name'] for p in people] + [p['label'] for p in graph_node_projects]

        hindsight_memories, hindsight_timestamp = await retrieve_hindsight_memories(
            task_inputs,
            active_tasks,
            entity_terms=all_entity_terms
        )

        memory_lines = []
        if graph_context:
            memory_lines.append(graph_context)
        memory_lines.extend(hindsight_memories)
        hindsight_block = "\n".join(memory_lines)

        if hindsight_memories or graph_context:
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
        
        # 🧠 RECENT MEMORIES (semantic search based on today's tasks)
        recent_memories_context = await get_recent_memories_for_briefing(filtered_tasks)
        
        # 🤖 AGENT 1: DEPENDENCY AGENT (uses graph_edges for task dependencies)
        dependency_context = await check_task_dependencies(active_tasks)
        
        # 👥 AGENT 2: SOCIAL GRAPH OPTIMIZER (communication patterns)
        social_graph_context = await analyze_communication_patterns(people)
        
        # 📅 AGENT 3: TEMPORAL PATTERN DETECTOR (on this day insights)
        temporal_context = await detect_temporal_patterns()
        
        # 🤖 AGENT 4: SERENDIPITY ENGINE (cross-domain connections)
        serendipity_context = await serendipity_engine(active_tasks, people, recent_lib.data or [])
        
        # 🤖 AGENT 5: ADAPTIVE BRIEFING LEARNER (learns from briefing patterns)
        adaptive_context = await adaptive_briefing_learner()
        
        # Fetch email-suggested tasks not yet shown in brief
        pending_email_tasks_res = supabase.table('email_pending_tasks')\
            .select('id, suggested_title, suggested_project, email_id')\
            .eq('shown_in_brief', False)\
            .is_('danny_decision', None)\
            .execute()

        pending_email_tasks = pending_email_tasks_res.data or []

        print("📦 Step 5: Building context...")
        # --- 2. THINK Phase ---
        print('🤖 Building prompt...')

        project_details = build_routing_context(legacy_projects)

        project_names = [p.get('name') for p in legacy_projects if p.get('name')]
        people_names = [p['name'] for p in people]
        # Task-boundary-safe truncation: split on ' | ' delimiter and accumulate complete tasks
        parts = compressed_tasks.split(' | ')
        safe_parts = []
        running_len = 0
        for part in parts:
            if running_len + len(part) + 3 > 3000:
                break
            safe_parts.append(part)
            running_len += len(part) + 3
        compressed_tasks_final = ' | '.join(safe_parts)
        new_inputs_text = "\n---\n".join([_enrich(d) for d in dumps])
        new_input_summary = " | ".join([_enrich(d) for d in dumps[:5]])

        synced_inputs_text = ""
        if synced_dumps:
            synced_lines = []
            for d in synced_dumps:
                content = d.get('content', '')
                meta = d.get('metadata') or {}
                if isinstance(meta, str):
                    try:
                        meta = json.loads(meta)
                    except Exception:
                        meta = {}
                tid = meta.get('task_update_id')
                prefix = f"⚠️ TASK UPDATE (task #{tid}): " if tid else ""
                synced_lines.append(f"{prefix}{content}")
            synced_inputs_text = "\n---\n".join(synced_lines) if synced_lines else "None"
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
        calendar_context = get_calendar_context(target_day)

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
        
        ADAPTIVE LEARNING (briefing optimization):
        {adaptive_context if adaptive_context else "None"}
        
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
        - NEW INPUTS: {new_inputs_text}
        - SYNCED TASK UPDATES (already processed, for context only): {synced_inputs_text}
        - 📧 EMAIL-SUGGESTED TASKS (surface these in the brief under a section called "📧 Inbox" — Danny decides whether to create them as tasks, do not auto-create):
        {chr(10).join(f"- {t['suggested_title']} (Project: {t.get('suggested_project') or 'Unknown'})" for t in pending_email_tasks) if pending_email_tasks else "None"}

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
            - STRICT DATA FIDELITY FOR BRIEFING: You are STRICTLY FORBIDDEN from listing any task in ANY section (Work, Home, Church, Ideas, or Done) that does not appear verbatim in the SYSTEM TASKS list provided below. Do NOT surface tasks from HINDSIGHT MEMORIES, Canonical Pages, or any other context into the briefing output. All context is for intelligence and routing only — NEVER for output.
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
                ✅ Done: ONLY list tasks that were moved to "completed_task_ids" in this specific run. NEVER list items from HINDSIGHT_MEMORIES in this section.
                🚀 Work: Active tasks from SYSTEM_TASKS only.
                🏠 Home: Family and personal tasks only. Do NOT include Ashraya/Church tasks here.
                ⛪ Church: Ashraya church admin, operations, finance, and organizational tasks only.
                💡 - Ideas: ONLY list items that appear in NEWLY ENRICHED RESOURCES or RECENT LIBRARY PATTERNS from this run. Never pull from Hindsight Memories or Canonical Pages.
            - MEMORY ISOLATION: HINDSIGHT_MEMORIES are for THE COMPASS (Opening Synthesis) ONLY. You are strictly forbidden from listing a memory as a bullet point in the task sections.
            - TONE: Match the PERSONA GUIDELINE. Be direct, simple, human. Talk like a friend who is also a high-level operator.
            - TONE GUARD: NEVER use words like 'Operational', 'Vanguard', 'Strategic Momentum', 'Audit', 'Battlefield', 'Chief of Staff', 'Tactical', 'Executive Office'. Use simple, punchy sentences. NEVER use: 'momentum', 'focus', 'gentle', 'reflection', 'push', 'strategic', 'SITREP', 'optimal', 'mission', 'ready for your review'.
            - INTELLIGENT FILTERING: 
                - If mode is 🔴 Urgent: HIDE the 🏠 Home, ⛪ Church, and 💡 Ideas sections. Focus strictly on 🚀 Work and ✅ Done.
                - If mode is 🟡 Important: Prioritize 🚀 Work and ⛪ Church.
                - NIGHT MODE PRIORITIZATION (Intel: Vaulted):
                    - 1. ✅ Done: List this first. Danny needs to see the loops he closed today to clear his mind.
                    - 2. 🏠 Home: List this second. Prioritize family, pets, and chores to transition Danny into 'Dad' mode.
                    - 3. ⛪ Church: List third. Ashraya church tasks.
                    - 4. 🚀 Work: List only the top 2-3 most critical open loops for tomorrow. 
                    - 5. 💡 Ideas: List any insights captured today to ensure they are 'secured' in the vault.
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
                - INBOX SECTION: If EMAIL-SUGGESTED TASKS has items, include a "📧 Inbox" section in the briefing listing each one. Format as: "- 📧 Task suggestion. Reply to confirm or ignore." Never auto-add these to newtasks.
                - STALE TASKS: If STALE_TASKS has items, include a short ⏳ Stale Loops section listing them with day count. Max 5. Cap with '...and X more stalled' if over 5.
             
         OUTPUT JSON SCHEMA (WARNING: ONLY POPULATE ARRAYS IF EXPLICITLY COMMANDED IN NEW INPUTS. OTHERWISE RETURN []):
        {{
            "completed_task_ids": [
                // Example ONLY: {{ "id": 123, "status": "done" }}, {{ "id": 456, "status": "todo", "reminder_at": "2026-03-20T10:00:00+05:30" }}, {{ "id": 789, "status": "todo", "reminder_at": "2026-03-21" }}
            ],
            "new_projects": [
                // Example ONLY: {{ "name": "...", "importance": 8, "org_tag": "SOLVSTRAT" }}
            ],
            "new_people": [
                // Example ONLY: {{ "name": "...", "role": "...", "strategic_weight": 9 }}
            ],
            "new_tasks": [
                // Example ONLY: {{ "title": "...", "project_name": "...", "priority": "urgent", "estimated_duration": 15, "reminder_at": null }},
                // Example ONLY: {{ "title": "...", "project_name": "Solvstrat", "priority": "important", "estimated_duration": 30, "reminder_at": "2026-03-21" }},
                // Example ONLY: {{ "title": "...", "project_name": "Qhord", "priority": "urgent", "estimated_duration": 45, "reminder_at": "2026-03-21T10:00:00+05:30" }}
            ],
            "resources": [
                // Example ONLY: {{ "url": "...", "title": "...", "summary": "...", "mission_name": "...", "project_name": "...", "strategic_note": "..." }}
            ],
            "logs": [],
            "new_missions": [],
            "briefing": "The formatted text string for Telegram."
        }}
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
            - STRICT MISSION MATCHING: ONLY assign a `mission_id` if the resource is a direct "building block" for an ACTIVE MISSION. If it is just a "cool tool" or "interesting read," leave `mission_id` as NULL.

            STRATEGIC AUDIT INSTRUCTIONS:
            - BLINDSPOT AUDIT: Evaluate every URL in NEW INPUTS against Danny's projects.
            - CONNECTION MAPPING: If a resource mentions a person in the PEOPLE list, link them in the summary.
            - PATTERN DETECTION: If you see 3+ links on a new topic, you MAY suggest a new mission in the `new_missions` JSON array.
            - THE VAULT GATE: These updates go to the DATABASE only.
            - THE BRIEFING GATE: You are STRICTLY FORBIDDEN from mentioning new resources or new missions in the briefing UNLESS Danny specifically used the word "Vault" or "Mission" in the NEW INPUTS.

            MISSION vs. INCUBATOR FRAMEWORK:
            - MISSION ASSEMBLY: Evaluate every URL and Input against ACTIVE MISSIONS. If a link provides a "component" for a mission, assign the "mission_name".
            - THE INCUBATOR AUDIT: If an input represents a high-potential standalone product idea NOT related to current goals, tag it as project_name: "INCUBATOR".
            - SPARK DETECTION: If a link is a "Spark" (brand new project concept), create a log with entry_type: "SPARK".
            - AUTO-MISSION DETECTION: If 3+ items suggest a cohesive new goal, add it to the "new_missions" array.

            DYNAMIC TASK MATCHING:
            - Compare inputs against ALL SYSTEM TASKS.
            - If Danny says "I'm done" or "Completed," mark the status as `done`.
            - DURATION ASSIGNMENT: Assign `estimated_duration` based on task type:
            - 15 minutes for routine tasks (emails, quick replies, status updates)
            - 45 minutes for anything related to Pilots, Sales, or high-stakes Mission 10 items
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
            8. AUTO-ONBOARDING: If a new Solvstrat client is mentioned, add to "new_projects" (org_tag: SOLVSTRAT). For other domains, only create a project if Danny explicitly commands it. If a new Person is mentioned, add to "new_people".
            9. STRATEGIC WEIGHTING: Grade items (1-10) based on Cashflow Recovery (₹30L debt).
            10. WEEKEND FILTER: If isWeekend is true, do NOT suggest or list Work tasks in the briefing.
            """

        # --- AI GENERATION ---
        # 🛡️ Step 1: Initialize variables to prevent "UnboundLocalError"
        response_text = ""
        ai_data = {
            "briefing": f"⚠️ FALLBACK MODE\n\n{len(dumps)} new inputs:\n{new_input_summary[:200]}",
            "new_tasks": [], "logs": [], "completed_task_ids": [], "new_projects": [], "new_people": []
        }

        try:
            # 🛡️ Step 2: The Modern Call with fallback
            response = await call_llm_with_fallback(
                prompt=prompt,
                model=BRIEFING_MODEL,
                config={
                    'response_mime_type': 'application/json',
                    'response_schema': PulseOutput,
                    'system_instruction': system_instruction_text
                },
                is_critical=True,
                require_json=True
            )
            response_text = response.text

            # 🛡️ Step 3: Precise Extraction
            # We move this inside the primary try block so it only runs if we HAVE text
            json_str = re.sub(r'^```json\n?', '', response_text)
            json_str = re.sub(r'\n?```$', '', json_str).strip()

            # Sanitization (Trailing commas + empty values)
            json_str = re.sub(r',\s*([}\]])', r'\1', json_str)

            match = re.search(r'\{[\s\S]*\}', json_str)
            if match:
                json_str = match.group(0)

            ai_data = json.loads(json_str)
            print("✅ AI Data Parsed Successfully:", list(ai_data.keys()))

        except Exception as e:
            audit_log_sync("pulse", "ERROR", f"AI Execution or JSON Parse Error: {e}")
            # The ai_data fallback is already set above, so the rest of the script won't crash

        # --- 3. WRITE Phase (Database Updates) ---

        # A. BATCH NEW PROJECTS (Deduplicated)
        if ai_data.get('new_projects'):
            valid_tags = ['SOLVSTRAT', 'QHORD', 'PERSONAL', 'CRAYON', 'ASHRAYA']

            CONTEXT_MAP = {
                'ASHRAYA':   'personal',
                'PERSONAL':  'personal',
                'SOLVSTRAT': 'work',
                'QHORD':     'work',
                'CRAYON':    'work',
            }
            filtered_new_projects = []

            for new_p in ai_data['new_projects']:
                p_name = new_p.get('name', 'Unnamed Project')
                p_tag = new_p.get('org_tag', 'SOLVSTRAT')
                already_exists = any(
                    p_name.lower() in get_project_name(existing_p).lower() or
                    get_project_name(existing_p).lower() in p_name.lower()
                    for existing_p in projects
                ) or any(
                    p_name.lower() in get_project_name(lp).lower() or
                    get_project_name(lp).lower() in p_name.lower()
                    for lp in legacy_projects
                )
                if not already_exists:
                    p_description = new_p.get('description')
                    if not p_description:
                        audit_log_sync("pulse", "WARNING", f"⚠️ New project '{p_name}' created without description — routing may be imprecise.")

                    filtered_new_projects.append({
                        "name": p_name,
                        "org_tag": p_tag if p_tag in valid_tags else 'SOLVSTRAT',
                        "status": "active",
                        "context": CONTEXT_MAP.get(p_tag, 'work'),
                        "is_active": True,
                        "description": p_description,
                        "keywords": new_p.get('keywords', []),
                    })

                    resolved_parent_id = None
                    parent_name = new_p.get('parent_project_name', '').lower().strip()
                    if parent_name:
                        parent_match = next(
                            (p for p in legacy_projects if p.get('name', '').lower() == parent_name),
                            None
                        )
                        if parent_match:
                            resolved_parent_id = parent_match['id']
                            filtered_new_projects[-1]['parent_project_id'] = resolved_parent_id
                            print(f"🔗 Will link '{p_name}' → parent '{parent_match['name']}' (id: {resolved_parent_id})")

            if filtered_new_projects:
                p_res = supabase.table('projects').insert(filtered_new_projects).execute()
                if p_res.data:
                    for new_proj in p_res.data:
                        project_name = new_proj.get('name')
                        node_metadata = {
                            "source": "pulse_auto",
                            "project_id": str(new_proj.get('id')),
                            "org_tag": new_proj.get('org_tag'),
                        }
                        try:
                            existing_node = (
                                supabase.table('graph_nodes')
                                .select('id', 'type')
                                .ilike('label', project_name)
                                .maybe_single()
                                .execute()
                            )
                            if existing_node and existing_node.data:
                                if existing_node.data['type'] != 'project':
                                    supabase.table('graph_nodes').update({
                                        'type': 'project',
                                        'metadata': node_metadata
                                    }).eq('id', existing_node.data['id']).execute()
                                    print(f"⬆️ Upgraded node '{project_name}' from {existing_node.data['type']} → project")
                                else:
                                    audit_log_sync("pulse", "WARNING", f"⚠️ Project node '{project_name}' already exists, updating metadata.")
                                    supabase.table('graph_nodes').update({
                                        'metadata': node_metadata
                                    }).eq('id', existing_node.data['id']).execute()
                            else:
                                supabase.table('graph_nodes').insert({
                                    "label": project_name,
                                    "type": "project",
                                    "metadata": node_metadata
                                }).execute()
                        except Exception as gn_err:
                            audit_log_sync("pulse", "WARNING", f"⚠️ Graph node sync failed (non-critical): {gn_err}")
                    legacy_projects.extend(p_res.data)
                    projects.extend(p_res.data)
                    print(f"✅ Created {len(p_res.data)} new entity projects.")

        # B. BATCH NEW PEOPLE
        if ai_data.get('new_people'):
            existing_people_res = supabase.table('people').select('name').execute()
            existing_raw = {p['name'].lower().strip() for p in (existing_people_res.data or [])}
            existing_norm = {normalize_person_name(p['name']) for p in (existing_people_res.data or []) if normalize_person_name(p['name'])}
            existing_nodes_res = supabase.table('graph_nodes').select('label, type').execute()
            existing_non_person_nodes = set()
            for gn in (existing_nodes_res.data or []):
                if gn.get('type') != 'person':
                    norm = normalize_person_name(gn.get('label', ''))
                    if norm:
                        existing_non_person_nodes.add(norm)
            deduped_people = []
            for p in ai_data['new_people']:
                name = p.get('name', '')
                if not name:
                    continue
                if is_blocklisted_person(name):
                    continue
                name_raw = name.lower().strip()
                name_norm = normalize_person_name(name)
                if name_raw in existing_raw or (name_norm and name_norm in existing_norm):
                    continue
                if name_norm and name_norm in existing_non_person_nodes:
                    continue
                deduped_people.append({**p, "source": "pulse"})
            if deduped_people:
                supabase.table('people').insert(deduped_people).execute()

        # C. BATCH TASK UPDATES (The Smart Rescheduler)
        if ai_data.get('completed_task_ids'):
            for item in ai_data['completed_task_ids']:
                target_id = item.get('id')
                item_status = item.get('status', 'done')
                raw_reminder = item.get('reminder_at')
                
                # Record whether original input had explicit time before format_rfc3339() normalizes it
                was_explicit_time = bool(raw_reminder and 'T' in str(raw_reminder))
                
                # 🛡️ RFC-3339 GUARD: Sanitize the timestamp immediately
                # This fixes the "Space" bug before Google ever sees it
                new_reminder = format_rfc3339(raw_reminder) if raw_reminder else None
                
                # 1. Fetch current IDs AND Status
                task_ref = supabase.table('tasks').select('status', 'google_task_id', 'google_event_id', 'title').eq('id', target_id).single().execute()

                # 🛡️ GUARD: Safely extract data - check BEFORE calling .get()
                task_data = task_ref.data if task_ref.data else {}
                current_db_status = task_data.get('status')
                g_id = task_data.get('google_task_id')
                e_id = task_data.get('google_event_id')
                task_title = task_data.get('title', "Untitled Task")

                # 🛑 THE LOCKDOWN: Block AI resurrection of finished tasks
                if current_db_status in ['done', 'cancelled']:
                    print(f"🚫 Task {target_id} ('{task_title}') is already {current_db_status}. Skipping.")
                    continue

                # 2. THE SMART CALENDAR SYNC (With Radar)
                cal_duration = item.get('duration_mins', 15)
                if item_status in ['done', 'cancelled'] and e_id:
                    delete_calendar_event(e_id)
                    e_id = None
                elif new_reminder and was_explicit_time:
                    # 🛰️ RADAR: Check for conflict before moving the block
                    conflict_name = await asyncio.to_thread(check_conflict, new_reminder, e_id)
                    if conflict_name:
                        # 🛡️ Safety: Assignment ensures we don't crash if 'briefing' key is missing
                        current_briefing = ai_data.get('briefing', "")
                        ai_data['briefing'] = current_briefing + f"\n\n⚠️ **SNOOZE CONFLICT:** Tried moving '{task_title}' to {new_reminder.split('T')[1][:5]}, but you have '{conflict_name}' then."
                    
                    # Edit or create the block
                    e_id = sync_to_calendar(task_title, new_reminder, event_id=e_id, duration_mins=cal_duration)
                elif e_id:
                    # Snooze to DATE-ONLY -> Remove existing block
                    delete_calendar_event(e_id)
                    e_id = None

                # 3. GOOGLE TASKS SYNC (Uses the same sanitized timestamp)
                if g_id:
                    try:
                        sync_to_google(tasks_service, title=task_title, task_id=g_id, status=item_status, due_at=new_reminder)
                    except Exception as g_err:
                        audit_log_sync("pulse", "WARNING", f"⚠️ Google Tasks sync failed for '{task_title}': {g_err}")
                        error_log.append(f"Google Tasks sync failed for: '{task_title}'")

                # 4. SUPABASE UPDATE (Saves 'T' format and allows time removal)
                update_payload = {"status": item_status, "google_event_id": e_id}
                if item_status == 'done': 
                    update_payload["completed_at"] = datetime.now(timezone.utc).isoformat()
                
                # REMOVE the 'if' here to allow clearing the time
                update_payload["reminder_at"] = new_reminder 

                # Propagate duration_mins update if provided
                duration_update = item.get('duration_mins')
                if duration_update is not None:
                    update_payload["duration_mins"] = duration_update
                    update_payload["estimated_minutes"] = duration_update

                change_reason = f"Status: {item_status}, reminder: {new_reminder}"
                if duration_update is not None:
                    change_reason += f", duration: {duration_update}min"

                # Use versioned_update for task status changes (creates history)
                versioned_update(
                    table_name='tasks',
                    record_id=target_id,
                    update_data=update_payload,
                    user_id=None,
                    change_source='pulse_task_update',
                    change_reason=change_reason
                )
                
                # 🧠 Outcome memory with project context
                if item_status == 'done':
                    proj_name = None
                    proj_id = task_data.get('project_id')
                    if proj_id:
                        proj_lookup = supabase.table('projects').select('name').eq('id', proj_id).maybe_single().execute()
                        proj_name = proj_lookup.data['name'] if proj_lookup.data else None
                    await write_outcome_memory(task_title, proj_name)

        # D. BATCH NEW TASKS (Checklist + Calendar Interruption + ID Tracking)
        if ai_data.get('new_tasks'):
            task_inserts = []
            explicit_times = []
            
            # PHASE 0: Time Tracker - Track explicit times from AI
            time_tracker = {}

            # PHASE 0: Inbox Discovery - Two-stage fallback from graph nodes → legacy projects
            inbox_from_graph = next(
                (p.get('legacy_id') for p in projects
                 if p.get('org_tag') == 'INBOX' and p.get('legacy_id') is not None),
                None
            )

            inbox_from_legacy = next(
                (p.get('id') for p in legacy_projects
                 if p.get('org_tag') == 'INBOX' and p.get('status') == 'active'),
                1
            )

            try:
                actual_inbox_id = int(inbox_from_graph or inbox_from_legacy)
            except (ValueError, TypeError):
                actual_inbox_id = 1

            audit_log_sync("pulse", "WARNING", f"⚠️ Inbox resolution: actual_inbox_id = {actual_inbox_id} (source: {'graph' if inbox_from_graph else 'legacy'})")

            for task in ai_data['new_tasks']:
                task_title = task.get('title', 'Untitled Task')

                # Cross-pipeline duplicate guard
                if is_already_in_email_queue(task_title):
                    continue  # Skip — email ingest already flagged this for approval

                ai_target = (task.get('project_name') or '').lower().strip()
                task_project_id = actual_inbox_id

                if ai_target:
                    matched = None

                    matched = next(
                        (p for p in legacy_projects if p.get('name', '').lower() == ai_target),
                        None
                    )

                    if not matched:
                        for p in legacy_projects:
                            kws = [k.lower() for k in (p.get('keywords') or [])]
                            if any(kw in ai_target or ai_target in kw for kw in kws):
                                matched = p
                                break

                    if not matched:
                        for p in legacy_projects:
                            desc = (p.get('description') or '').lower()
                            if ai_target in desc or any(word in desc for word in ai_target.split() if len(word) > 3):
                                matched = p
                                break

                    if not matched:
                        matched = next(
                            (p for p in legacy_projects
                             if ai_target in p.get('name', '').lower()
                             or p.get('name', '').lower() in ai_target),
                            None
                        )

                    if not matched:
                        gn_match = next(
                            (p for p in graph_node_projects if ai_target in get_project_name(p).lower()
                             or get_project_name(p).lower() in ai_target),
                            None
                        )
                        if gn_match:
                            try:
                                task_project_id = int(
                                    gn_match.get('legacy_id') or gn_match.get('id') or actual_inbox_id
                                )
                            except (ValueError, TypeError):
                                pass

                    if matched:
                        try:
                            task_project_id = int(matched.get('id') or actual_inbox_id)
                        except (ValueError, TypeError):
                            pass
                    else:
                        name_match = next(
                            (p for p in legacy_projects 
                             if p.get('status') == 'active' and
                             any(word in (p.get('name', '').lower()) 
                                 for word in ai_target.lower().split() if len(word) > 3)),
                            None
                        )
                        if name_match:
                            task_project_id = int(name_match['id'])
                            audit_log_sync("pulse", "WARNING", f"⚠️ Task '{task.get('title')}' fuzzy-matched to '{name_match['name']}' (ai_target: '{ai_target}')")
                        else:
                            work_hints = ['client', 'nda', 'pilot', 'send', 'check', 'follow', 'call', 'meeting', 'project']
                            is_work_context = any(hint in ai_target.lower() for hint in work_hints)
                            if is_work_context:
                                solvstrat_fallback = next(
                                    (p for p in legacy_projects if p.get('org_tag') == 'SOLVSTRAT' and not p.get('parent_project_id')),
                                    None
                                )
                                if solvstrat_fallback:
                                    task_project_id = solvstrat_fallback['id']
                                    audit_log_sync("pulse", "WARNING", f"⚠️ Task '{task.get('title')}' fell back to Solvstrat (no match for '{ai_target}')")
                            else:
                                error_log.append(f"Task routing failed for: '{task.get('title')}'")

                # 🛡️ RFC-3339 GUARD: Sanitize the AI's time string immediately
                raw_time = task.get('reminder_at')
                sanitized_time = format_rfc3339(raw_time) if raw_time else None
                    
                # 🔄 DE-CLASH LOGIC
                if raw_time and 'T' in str(raw_time) and sanitized_time:
                    time_slot = sanitized_time.split('T')[0]
                    existing_same_slot = [t for t in task_inserts if (t.get('reminder_at') or '').startswith(time_slot)]
                    if existing_same_slot:
                        stagger_count = len(existing_same_slot)
                        original_time = datetime.fromisoformat(sanitized_time.replace('Z', '+00:00'))
                        staggered_time = original_time + timedelta(minutes=15 * stagger_count)
                        sanitized_time = staggered_time.strftime('%Y-%m-%dT%H:%M:%S+05:30')
                        print(f"⏰ De-clash: Staggered '{task.get('title', 'Untitled Task')}' to {sanitized_time.split('T')[1][:5]}")

                explicit_time = bool(raw_time and 'T' in str(raw_time))

                # Idempotency guard using content hash
                dedup_key = hashlib.md5(
                    f"{task_title.lower().strip()}:{task_project_id}".encode()
                ).hexdigest()[:16]
                existing = supabase.table('tasks').select('id') \
                    .eq('dedup_key', dedup_key) \
                    .not_.in_('status', ['done', 'cancelled']) \
                    .limit(1).execute()
                if existing.data:
                    audit_log_sync("pulse", "WARNING", f"⚠️ Idempotency guard: '{task_title}' already exists. Skipping.")
                    continue

                task_inserts.append({
                    "title": task_title,
                    "project_id": task_project_id,
                    "priority": (task.get('priority') or 'important').lower(),
                    "status": "todo",
                    "estimated_minutes": task.get('estimated_duration', 15),
                    "duration_mins": task.get('estimated_duration', 15),
                    "reminder_at": sanitized_time,
                    "is_revenue_critical": task.get('is_revenue_critical', False),
                    "dedup_key": dedup_key,
                })
                explicit_times.append(explicit_time)
            if task_inserts:
                insert_res = supabase.table('tasks').insert(task_inserts).execute()
                print(f"✅ Phase 1: Inserted {len(insert_res.data)} new tasks to Supabase.")

                # PHASE 2: Side-Effect Orchestration - Google Sync after DB success
                for db_task, expl_time in zip(insert_res.data, explicit_times):
                    task_id = db_task['id']
                    task_title = db_task.get('title', 'Untitled Task')
                    
                    asyncio.create_task(
                        write_graph_edges_for_task(
                            task_id=task_id,
                            task_title=task_title,
                            project_id=db_task.get('project_id'),
                            task_description=db_task.get('description'),
                            people_cache=people
                        )
                    )
                    
                    # Read directly from the DB's safe return data, NOT the local array
                    sanitized_time = db_task.get('reminder_at')
                    duration_mins = db_task.get('duration_mins') or 15
                    
                    # Use explicit_time from zip (avoids title collision)
                    explicit_time = expl_time
                    
                    g_id = None
                    e_id = None

                    # 2a. SYNC TO GOOGLE TASKS (run in thread to avoid blocking)
                    if sanitized_time:
                        try:
                            g_id = await asyncio.to_thread(
                                sync_to_google,
                                tasks_service,
                                task_title,
                                sanitized_time,
                                None,
                                None,
                                explicit_time
                            )
                            if g_id: print(f"📡 Google Task Created: {task_title}")
                        except Exception as e:
                            audit_log_sync("pulse", "WARNING", f"⚠️ Google Tasks Sync failed: {e}")
                            error_log.append(f"Google Tasks sync failed for: '{task_title}'")

                    # 2b. STRATEGIC GATE: SYNC TO CALENDAR (Only runs if explicit time was given)
                    if sanitized_time and explicit_time:
                        try:
                            conflict_name = await asyncio.to_thread(check_conflict, sanitized_time)
                            if conflict_name:
                                briefing = ai_data.get('briefing', "")
                                ai_data['briefing'] = briefing + f"\n\n⚠️ **CALENDAR CLASH:** '{task_title}' overlaps with '{conflict_name}'."
                            
                            e_id = await asyncio.to_thread(sync_to_calendar, task_title, sanitized_time, duration_mins)
                            if e_id: print(f"🔥 Calendar block secured: {task_title} ({duration_mins}m)")
                        except Exception as ce:
                            audit_log_sync("pulse", "WARNING", f"⚠️ Calendar Sync failed for {task_title}: {ce}")
                            error_log.append(f"Calendar sync failed for: '{task_title}'")

                    # 2c. Store Google IDs back to Supabase (direct update, no version churn)
                    if g_id or e_id:
                        try:
                            supabase.table('tasks').update({
                                'google_task_id': g_id,
                                'google_event_id': e_id,
                            }).eq('id', task_id).execute()
                            print(f"🔄 Updated task {task_id} with Google IDs.")
                        except Exception as ve:
                            audit_log_sync("pulse", "WARNING", f"⚠️ Google ID update failed for task {task_id}: {ve}")

        # G. CLEANUP & LOGS
        if ai_data.get('logs'):
            supabase.table('logs').insert(ai_data['logs']).execute()

        # H. NEW MISSIONS
        missions_created_count = 0
        if ai_data.get('new_missions'):
            # TITLE A0. BATCH NEW MISSIONS Deduplicated...
            # Fetch existing mission titles for deduplication
            existing_missions_res = supabase.table('missions').select('id, title').eq('status', 'active').execute()
            existing_titles_normalized = {normalize_mission_title(m['title']): m for m in (existing_missions_res.data or [])}
            run_dedup = set()

            for mission_title in ai_data['new_missions']:
                if not mission_title or not isinstance(mission_title, str):
                    continue
                norm = normalize_mission_title(mission_title)
                if not norm or norm in run_dedup:
                    continue
                if norm in existing_titles_normalized:
                    run_dedup.add(norm)
                    continue
                # Insert new mission
                ist_ts = datetime.now(timezone(timedelta(hours=5, minutes=30)))
                description = f"Auto-created by Pulse from recurring resource/input patterns on {ist_ts.strftime('%Y-%m-%d')}."
                insert_res = supabase.table('missions').insert({
                    "title": mission_title.strip(),
                    "status": "active",
                    "description": description
                }).execute()
                if insert_res.data:
                    missions_created_count += 1
                    run_dedup.add(norm)
                    active_missions.append(insert_res.data[0])
                    mission_names.append(mission_title.strip())
                    print(f"🎯 Mission auto-created: {mission_title}")

        if missions_created_count > 0:
            print(f"✅ Created {missions_created_count} new missions this run.")

        # TITLE A1. HISTORICAL RESOURCE MISSION BACKFILL...
        # Only attempt backfill if there are active missions to map against
        if active_missions:
            try:
                # Fetch resources with NULL mission_id that have metadata to classify
                null_resources_res = supabase.table('resources').select(
                    'id, url, title, summary, strategic_note, category'
                ).is_('mission_id', None).execute()
                null_resources = null_resources_res.data or []
                if null_resources:
                    # Build mission title->id map
                    mission_map = {m['title']: m['id'] for m in active_missions}
                    # Limit batch size for safety
                    batch_size = min(75, len(null_resources))
                    backfill_batch = null_resources[:batch_size]
                    print(f"🔄 Backfilling {len(backfill_batch)} historical resources with missions...")

                    # Build classifier prompt
                    mission_list_str = "\n".join([f"- {m['title']}" for m in active_missions])
                    resources_json = json.dumps([{
                        "id": r['id'],
                        "title": r.get('title', ''),
                        "summary": r.get('summary', ''),
                        "strategic_note": r.get('strategic_note', ''),
                        "category": r.get('category', '')
                    } for r in backfill_batch], indent=2)

                    backfill_prompt = f"""You are a mission classifier. Classify each resource against the ACTIVE missions below.

                    ACTIVE MISSIONS:
                    {mission_list_str}

                    STRICT RULES:
                    - Only assign a mission if the resource is a DIRECT BUILDING BLOCK for that mission.
                    - If it is a cool tool, general article, personal read, faith content, curiosity item, or interesting but non-core material, return mission_name: null.
                    - Never force a match. Exact mission title only if assigning.
                    - If ambiguous between two missions, return null.
                    - If confidence is below 0.80, return null.
                    - Better unmapped than wrongly mapped.

                    Resources to classify:
                    {resources_json}

                    Return ONLY valid JSON array:
                    [
                    {{"id": 1, "missionname": "...", "reason": "...", "confidence": 0.85}},
                    {{"id": 2, "missionname": null, "reason": "...", "confidence": 0.0}}
                    ]"""

                    try:
                        backfill_response = await call_llm_with_fallback(
                            prompt=backfill_prompt,
                            model="gemini-3.1-flash-lite-preview",
                            config={'response_mime_type': 'application/json'},
                            is_critical=False,
                            require_json=True
                        )
                        backfill_result = parse_json_response(backfill_response.text)
                        if not isinstance(backfill_result, list):
                            audit_log_sync("pulse", "WARNING", f"⚠️ Backfill classifier returned non-list, skipping.")
                            backfill_result = []

                        backfilled_count = 0
                        for item in backfill_result:
                            res_id = item.get('id')
                            missionname = item.get('missionname')
                            confidence = item.get('confidence', 0.0)

                            # Only update if: missionname is non-null, title exists in map, confidence >= 0.80
                            if missionname and missionname in mission_map and confidence >= 0.80:
                                mission_id = mission_map[missionname]
                                # Versioned update for resources
                                versioned_update('resources', res_id, {
                                    "mission_id": mission_id
                                })
                                backfilled_count += 1
                                print(f"🔗 Backfilled resource {res_id} → mission '{missionname}' (conf: {confidence})")

                        print(f"✅ Backfilled {backfilled_count}/{len(backfill_batch)} historical resources with missions.")

                    except Exception as bc_err:
                        audit_log_sync("pulse", "WARNING", f"⚠️ Resource backfill classification failed: {bc_err}")

            except Exception as br_err:
                audit_log_sync("pulse", "WARNING", f"⚠️ Resource backfill fetch error: {br_err}")

        # --- 4. SPEAK Phase ---
        briefing_text = ai_data.get('briefing', '')
        shown_ids = []
        if briefing_text:
            # 🛡️ THE ARCHITECT'S FINAL REPAIR: Force double newlines before all section headers
            # This ensures that even if the AI 'whispers', the grid stays intact.
            headers = ['🚀 Work', '🏠 Home', '⛪ Church', '💡 Ideas', '✅ Done', '🛡️ WEEKEND RECON']
            for header in headers:
                if header in briefing_text:
                    # Replace the header with a version that has breathing room above it
                    briefing_text = briefing_text.replace(header, f"\n\n{header}\n")

            # 🛡️ Fix escaping and enforce list breaks
            briefing_text = briefing_text.replace('\\n', '\n').replace('\\\\n', '\n').replace(' - ', '\n- ')

            # Existing logic: Remove internal system IDs from the user-facing text
            briefing_text = re.sub(r'\[?ID:\s*\d+\]?', '', briefing_text, flags=re.IGNORECASE).strip()

            # Strip bare task ID references in natural language (e.g. "117 is the last loop")
            briefing_text = re.sub(r'\b(\d{2,})\s+(?:is the|task|loop|item|#|ref|id)\b', r'\1', briefing_text, flags=re.IGNORECASE)

            # Final Clean: Remove any accidental triple-newlines created by the logic above
            briefing_text = re.sub(r'\n{3,}', '\n\n', briefing_text)

            # 📨 EMAIL DECISIONS SECTION — Surface pending email tasks for Danny's approval
            shown_ids = []
            try:
                # Auto-expire tasks older than 7 days
                cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
                supabase.table('email_pending_tasks')\
                    .update({'danny_decision': 'expired'})\
                    .is_('danny_decision', 'null')\
                    .lt('created_at', cutoff)\
                    .execute()

                pending_decisions = supabase.table('email_pending_tasks')\
                    .select('id, suggested_title, suggested_project, created_at')\
                    .is_('danny_decision', 'null')\
                    .order('created_at', desc=False)\
                    .limit(5)\
                    .execute()
                if pending_decisions.data:
                    lines = ["\n\n📨 EMAIL DECISIONS (" + str(len(pending_decisions.data)) + ") — reply [code] yes/drop"]
                    shown_ids = []
                    for row in pending_decisions.data:
                        shortcode = str(row['id'])
                        project_label = f" ({row['suggested_project']})" if row.get('suggested_project') else ""
                        title = row['suggested_title'][:60]
                        lines.append(f"[{shortcode}] {title}{project_label}")
                        shown_ids.append(row['id'])
                    if briefing_text:
                        briefing_text += "\n".join(lines)
                    else:
                        briefing_text = "\n".join(lines)
            except Exception as ed_err:
                audit_log_sync("pulse", "WARNING", f"⚠️ Email decisions section failed: {ed_err}")

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
            except Exception as log_err:
                audit_log_sync("pulse", "WARNING", f"Failed to log briefing to raw_dumps: {log_err}")

        # Mark shown_in_brief only AFTER confirmed Telegram send
        if send_success and shown_ids:
            try:
                supabase.table('email_pending_tasks')\
                    .update({'shown_in_brief': True})\
                    .in_('id', shown_ids)\
                    .execute()
            except Exception as e:
                audit_log_sync("pulse", "WARNING", f"⚠️ shown_in_brief update failed: {e}")
        elif shown_ids:
            print("⚠️ Telegram send failed — shown_in_brief NOT updated. Will retry at next pulse.")

        # --- 📝 AFTER-ACTION REPORT ---
        if hour >= 20 or hour < 4:
            await generate_after_action_report()

        # ✅ COMPLETION DUMP CLOSER — seal the raw dumps that were completion signals
        if completion_dump_ids:
            supabase.table('raw_dumps').update({"status": "completed", "is_processed": True}).in_('id', completion_dump_ids).execute()
            print(f"✅ Sealed {len(completion_dump_ids)} completion dumps.")

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