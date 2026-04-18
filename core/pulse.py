import os
import json
import re
import asyncio
import httpx
from datetime import datetime, timedelta, timezone
from supabase import create_client, Client
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.discovery_cache import base
from google import genai
from pydantic import BaseModel, Field
from typing import List, Optional

supabase: Client = create_client(
    os.getenv("SUPABASE_URL"), 
    os.getenv("SUPABASE_SERVICE_ROLE_KEY")
)
gemini_client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))

EMBEDDING_MODEL = "gemini-embedding-2-preview"
EMBEDDING_DIMENSION = 768

BRIEFING_MODEL = "gemini-3-flash-preview"

# 🛡️ CLEAN MODELS (Removed Config blocks to prevent API rejection)
class CompletedTask(BaseModel):
    id: int
    status: str
    reminder_at: Optional[str] = None

class NewProject(BaseModel):
    name: str
    importance: Optional[int] = 5
    org_tag: Optional[str] = "INBOX"

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

def normalize_mission_title(value: str) -> str:
    """Normalize mission title for comparison: lowercase, strip, collapse punctuation."""
    if not value or not isinstance(value, str):
        return ""
    normalized = value.lower().strip()
    normalized = re.sub(r'[^a-z0-9]+', ' ', normalized)
    normalized = re.sub(r'\s+', ' ', normalized).strip()
    return normalized

async def call_gemini_with_retry(prompt: str, model: str = None, config: dict = None, contents=None):
    if model is None:
        model = BRIEFING_MODEL
    
    max_retries = 5
    base_delay = 10

    retryable_errors = ['503', '504', '500', 'disconnected', 'timeout', 'deadline exceeded']
    
    for attempt in range(max_retries):
        try:
            if contents is not None:
                response = gemini_client.models.generate_content(
                    model=model,
                    contents=contents,
                    config=config or {}
                )
            else:
                response = gemini_client.models.generate_content(
                    model=model,
                    contents=prompt,
                    config=config or {}
                )
            return response
        except Exception as e:
            error_str = str(e).lower()

            should_retry = any(err in error_str for err in retryable_errors)
            if should_retry and attempt < max_retries - 1:
                delay = base_delay * (2 ** attempt)
                print(f"⚠️ API Hiccup ({error_str}), retrying in {delay}s...")
                await asyncio.sleep(delay)
                continue
            else:
                raise

def get_embedding(text: str) -> list:
    """Generate embedding for text using gemini-embedding-2-preview."""
    try:
        # 🎯 FORCE 768 dimensions to match your Supabase schema
        result = gemini_client.models.embed_content(
            model=EMBEDDING_MODEL,
            contents=text,
            config={
                'output_dimensionality': EMBEDDING_DIMENSION
            }
        )
        return result.embeddings[0].values
    except Exception as e:
        # Fallback to zero-vector on error to prevent total system crash
        print(f"Embedding error: {e}")
        return [0] * EMBEDDING_DIMENSION

async def hybrid_search_graph(query: str) -> str:
    """Graph-first search: Find primary entity and its connections."""
    try:
        nodes_res = supabase.table('graph_nodes').select('id, label').ilike('label', f'%{query}%').limit(1).execute()
        
        if not nodes_res.data:
            return ""
        
        primary_node = nodes_res.data[0]
        primary_id = primary_node['id']
        
        edges_res = supabase.table('graph_edges').select('source_node_id, target_node_id, relationship').or_(f'source_node_id.eq.{primary_id},target_node_id.eq.{primary_id}').execute()
        
        if not edges_res.data:
            return ""
        
        connected_ids = set()
        
        for edge in edges_res.data:
            if edge['source_node_id'] == primary_id:
                connected_ids.add(edge['target_node_id'])
            elif edge['target_node_id'] == primary_id:
                connected_ids.add(edge['source_node_id'])
        
        if connected_ids:
            labels_res = supabase.table('graph_nodes').select('id, label').in_('id', list(connected_ids)).execute()
            label_map = {str(n['id']): n['label'] for n in labels_res.data}
            
            labeled_map = []
            for edge in edges_res.data:
                src_label = label_map.get(str(edge['source_node_id']), "Unknown")
                tgt_label = label_map.get(str(edge['target_node_id']), "Unknown")
                
                if edge['source_node_id'] == primary_id:
                    labeled_map.append(f"[{primary_node['label']}] -> [{edge['relationship']}] -> [{tgt_label}]")
                elif edge['target_node_id'] == primary_id:
                    labeled_map.append(f"[{src_label}] -> [{edge['relationship']}] -> [{primary_node['label']}]")
            
            return "\n".join(labeled_map)
        
        return ""
    
    except Exception as e:
        print(f"Hybrid search error: {e}")
        return ""


async def retrieve_hindsight_memories(task_inputs: list, active_tasks: list, top_k: int = 5) -> tuple:
    """High-Res Hindsight: Multi-signal vector search across tasks and inputs.
    Returns tuple of (formatted_memories, latest_timestamp).
    """
    latest_timestamp = None
    try:
        search_queries = []
        
        if task_inputs:
            combined_tasks = " ".join(task_inputs)
            search_queries.append(("combined_tasks", combined_tasks))
        
        top_active = sorted(active_tasks, key=lambda t: t.get('priority', 'chores') == 'urgent', reverse=True)[:3]
        for t in top_active:
            title = t.get('title', '')
            if title:
                search_queries.append((f"task:{title}", title))
        
        if not search_queries:
            return ([], None)
        
        async def fetch_memories_for_query(query_name: str, query_text: str):
            try:
                embedding = await asyncio.to_thread(get_embedding, query_text)
                if not any(embedding): return []
                res = supabase.rpc(
                    'match_memories',
                    {
                        'query_embedding': embedding,
                        'match_count': top_k,
                        'match_threshold': 0.6
                    }
                ).execute()
                return res.data if res.data else []
            except Exception as e:
                print(f"Hindsight query error ({query_name}): {e}")
                return []
        
        all_results = await asyncio.gather(*[fetch_memories_for_query(name, text) for name, text in search_queries])
        
        seen_ids = set()
        unique_memories = []
        for results in all_results:
            for m in results:
                m_id = m.get('id')
                if m_id and m_id not in seen_ids:
                    seen_ids.add(m_id)
                    unique_memories.append(m)
        
        unique_memories.sort(key=lambda x: x.get('similarity', 0), reverse=True)
        top_memories = unique_memories[:top_k]
        
        if top_memories:
            latest_timestamp = top_memories[0].get('created_at')
            formatted = [
                f"[MEMORY CONTEXT ONLY — DO NOT LIST IN BRIEFING] {m.get('memory_type', '').upper()}: {m.get('content', '')}"
                for m in top_memories
            ]
            return (formatted, latest_timestamp)
    except Exception as e:
        print(f"High-Res Hindsight error: {e}")
    return ([], None)


async def generate_after_action_report() -> str:
    """Generate an After-Action Report on the day's activities and save to memories."""
    try:
        now = datetime.now(timezone(timedelta(hours=5, minutes=30)))
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
        
        completed_tasks_res = supabase.table('tasks').select('title').eq('status', 'done').gte('completed_at', today_start).execute()
        completed_count = len(completed_tasks_res.data) if completed_tasks_res.data else 0
        
        open_tasks_res = supabase.table('tasks').select('id').eq('status', 'todo').execute()
        open_count = len(open_tasks_res.data) if open_tasks_res.data else 0
        
        prompt = f"""You are Danny's Rhodey. Provide a dry After-Action Report (AAR). 1-2 sentences max. Focus on loops closed vs. open.
- Loops closed today: {completed_count}
- Loops still open: {open_count}"""
        
        response = await call_gemini_with_retry(prompt=prompt)
        
        lesson = response.text.strip()
        
        if lesson and len(lesson) > 10:
            embedding = get_embedding(lesson)
            if all(v == 0 for v in embedding):
                print(f"Warning: zero-vector embedding for daily reflection — storing anyway")
            supabase.table('memories').insert({
                "content": lesson,
                "memory_type": "reflection",
                "embedding": embedding
            }).execute()
            print(f"📝 Daily Reflection saved: {lesson[:50]}...")
            return lesson
    except Exception as e:
        print(f"Daily reflection error: {e}")
    return ""


class MemoryCache(base.Cache):
    _cache = {}

    def get(self, url):
        return self._cache.get(url)

    def set(self, url, content):
        self._cache[url] = content


async def fetch_url_metadata(url: str):
    try:
        async with httpx.AsyncClient(timeout=8.0, follow_redirects=True) as http_client:
            headers = {
                "User-Agent": "Mozilla/5.0 (compatible; Twitterbot/1.0)",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8"
            }
            response = await http_client.get(url, headers=headers)
            if response.status_code == 200:
                html = response.text
                title_match = re.search(r'property=["\']og:title["\'] content=["\'](.*?)["\']', html, re.I)
                title = title_match.group(1).strip() if title_match else "Unknown"
                desc_match = re.search(r'property=["\']og:description["\'] content=["\'](.*?)["\']', html, re.I)
                description = desc_match.group(1).strip() if desc_match else ""
                return {"title": title, "description": description}
    except Exception as e:
        print(f"Scraper error for {url}: {e}")
    return {"title": "Unknown", "description": ""}


async def batch_enrich_resources():
    unenriched = supabase.table('resources').select('id, url').is_('enriched_at', None).execute()
    if not unenriched.data:
        print("📚 No unenriched resources found.")
        return []
    
    print(f"🔍 Found {len(unenriched.data)} unenriched resources. Scraping in parallel...")
    scraped = await asyncio.gather(*[fetch_url_metadata(r['url']) for r in unenriched.data])
    
    enrichment_data = []
    for i, r in enumerate(unenriched.data):
        enrichment_data.append({
            "id": r['id'],
            "url": r['url'],
            "title": scraped[i].get('title', 'Unknown'),
            "description": scraped[i].get('description', '')
        })
    
    if not enrichment_data:
        return []
    
    prompt = f"""You are Danny's Trusted Partner. For each resource below, provide a strategic_note (one sentence on strategic value) and category.

Categories: COMPETITOR, TECH_TOOL, LEAD_POTENTIAL, MARKET_TREND, CHURCH, PERSONAL
Rules:
- CHURCH or PERSONAL for family/home/faith topics
- COMPETITOR for competitors to Qhord
- TECH_TOOL for SaaS/dev/productivity tools
- LEAD_POTENTIAL for potential clients/partners
- MARKET_TREND for market patterns/industry shifts
- Default: MARKET_TREND

Return ONLY valid JSON array:
[
  {{"id": 1, "strategic_note": "...", "category": "..."}},
  ...
]

Resources:
{json.dumps(enrichment_data, indent=2)}"""
    
    try:
        response = await call_gemini_with_retry(
            prompt=prompt,
            model="gemini-3.1-flash-lite-preview",
            config={'response_mime_type': 'application/json'}
        )
        parsed = json.loads(response.text)
        
        ist_offset = timezone(timedelta(hours=5, minutes=30))
        enriched_at = datetime.now(ist_offset).isoformat()
        
        for item in parsed:
            for ed in enrichment_data:
                if ed['id'] == item.get('id'):
                    item['title'] = ed['title']
                    item['description'] = ed['description']
                    break
        
        for item in parsed:
            title = item.get('title', '')
            strategic_note = item.get('strategic_note', '')
            embedding_text = f"{title}. {strategic_note}"
            embedding = get_embedding(embedding_text)
            if all(v == 0 for v in embedding):
                print(f"Warning: zero-vector embedding for daily reflection — storing anyway")
            
            supabase.table('resources').update({
                "title": title,
                "summary": item.get('description'),
                "strategic_note": strategic_note,
                "category": item.get('category', 'MARKET_TREND'),
                "enriched_at": enriched_at,
                "embedding": embedding
            }).eq('id', item['id']).execute()
        
        print(f"✅ Batch enriched {len(parsed)} resources with embeddings.")

        # MISSION RESOLVER: Link enriched resources to active missions by name
        try:
            missions_res = supabase.table('missions').select('id, title').eq('status', 'active').execute()
            active_missions = missions_res.data or []

            unlinked = supabase.table('resources').select('id, title, strategic_note').is_('mission_id', None).not_.is_('enriched_at', None).execute()

            for resource in (unlinked.data or []):
                resource_text = f"{resource.get('title', '')} {resource.get('strategic_note', '')}".lower()
                for mission in active_missions:
                    mission_keywords = mission['title'].lower().split()
                    match_score = sum(1 for kw in mission_keywords if kw in resource_text)
                    if match_score >= 2:
                        supabase.table('resources').update({
                            "mission_id": mission['id']
                        }).eq('id', resource['id']).execute()
                        print(f"🔗 Linked resource '{resource.get('title')}' → mission '{mission['title']}'")
                        break
        except Exception as e:
            print(f"⚠️ Mission resolver error: {e}")

        return parsed
    except Exception as e:
        print(f"Batch enrichment error: {e}")
        return []


# --- 🛰️ LAYER 1: GOOGLE INTEGRATION HELPERS ---

def sync_completed_tasks_from_google(supabase_client, tasks_service):
    """Pulls completed status from Google Tasks and updates Supabase."""
    try:
        result = supabase_client.table('tasks')\
            .select('id, title, google_task_id, status')\
            .eq('status', 'todo')\
            .not_.is_('google_task_id', None)\
            .execute()
        
        tasks_to_sync = result.data or []
        if not tasks_to_sync:
            print("📋 No Google Tasks to sync.")
            return
        
        print(f"🔍 Checking {len(tasks_to_sync)} tasks against Google Tasks...")
        
        synced_count = 0
        for task in tasks_to_sync:
            task_id = task['id']
            google_task_id = task['google_task_id']
            title = task.get('title', 'Untitled')
            
            try:
                google_task = tasks_service.tasks().get(
                    tasklist='@default',
                    task=google_task_id
                ).execute()
                
                if google_task.get('status') == 'completed':
                    supabase_client.table('tasks').update({
                        'status': 'done',
                        'completed_at': datetime.now(timezone.utc).isoformat()
                    }).eq('id', task_id).execute()
                    
                    print(f"✅ Synced from Google: '{title}' (ID: {task_id})")
                    synced_count += 1
                    
            except Exception as e:
                if 'notFound' in str(e):
                    print(f"⚠️ Google Task {google_task_id} not found, skipping.")
                else:
                    print(f"⚠️ Error checking Google Task {google_task_id}: {e}")
        
        print(f"📊 Google→Supabase Sync complete: {synced_count}/{len(tasks_to_sync)} tasks marked done.")
        
    except Exception as e:
        print(f"❌ sync_completed_tasks_from_google failed: {e}")

def get_google_creds():
    """Unified credential handshake for all Google services."""
    return Credentials(
        None,
        refresh_token=os.getenv("GOOGLE_REFRESH_TOKEN"),
        client_id=os.getenv("GOOGLE_CLIENT_ID"),
        client_secret=os.getenv("GOOGLE_CLIENT_SECRET"),
        token_uri="https://oauth2.googleapis.com/token"
    )

def get_tasks_service():
    """Helper to spin up the Tasks engine."""
    return build('tasks', 'v1', credentials=get_google_creds(), cache=MemoryCache())

def format_rfc3339(date_str):
    """Ensures a timestamp is 100% compliant with Google's strict RFC-3339 requirements."""
    if not date_str: return None
    # 🛡️ FIX: Replace space with 'T' and ensure IST timezone
    clean = str(date_str).replace(' ', 'T')
    if 'T' not in clean:
        clean = f"{clean}T09:00:00+05:30"
    if not (clean.endswith('Z') or '+' in clean[-6:]):
        clean += "+05:30"
    return clean

def check_conflict(start_iso):
    """Radar: Checks if a 30-minute window is already booked."""
    try:
        service = build('calendar', 'v3', credentials=get_google_creds(), cache=MemoryCache())
        rfc_time = format_rfc3339(start_iso)
        
        start_dt = datetime.fromisoformat(rfc_time.replace('Z', '+00:00'))
        end_dt = start_dt + timedelta(minutes=30)
        
        events_res = service.events().list(
            calendarId='primary',
            timeMin=rfc_time,
            timeMax=end_dt.isoformat(),
            singleEvents=True
        ).execute()
        
        events = events_res.get('items', [])
        return events[0].get('summary') if events else None
    except Exception as e:
        print(f"⚠️ Conflict check failed: {e}")
        return None

def sync_to_calendar(title, start_iso, duration_mins=15, event_id=None):
    """Creates or UPDATES a block on the grid with dynamic duration."""
    service = build('calendar', 'v3', credentials=get_google_creds(), cache=MemoryCache())
    try:
        rfc_time = format_rfc3339(start_iso)
        start_dt = datetime.fromisoformat(rfc_time.replace('Z', '+00:00'))
        
        # 🕒 DYNAMIC DURATION (Defaulting to 15 now)
        end_dt = start_dt + timedelta(minutes=int(duration_mins))
        
        event_body = {
            'summary': f"🔥 CRITICAL: {title}",
            'description': 'Automated via Integrated-OS Sync',
            'start': {'dateTime': rfc_time, 'timeZone': 'Asia/Kolkata'},
            'end': {'dateTime': end_dt.isoformat(), 'timeZone': 'Asia/Kolkata'},
            'reminders': {'useDefault': True} 
        }
        
        if event_id:
            res = service.events().patch(calendarId='primary', eventId=event_id, body=event_body).execute()
            print(f"🔄 SUCCESS: Calendar slot edited for {title}")
        else:
            res = service.events().insert(calendarId='primary', body=event_body).execute()
            print(f"📅 SUCCESS: New calendar block secured for {title}")
            
        return res.get('id')
    except Exception as e:
        # Fallback logic: If the event_id was invalid, try creating fresh
        if event_id: 
            print(f"⚠️ Event ID {event_id} invalid. Attempting fresh creation...")
            return sync_to_calendar(title, start_iso, event_id=None)
        print(f"❌ CRITICAL: Calendar sync failed: {e}")
        return None

def delete_calendar_event(event_id):
    """Removes the protective block from the grid with explicit logging."""
    if not event_id: return
    service = build('calendar', 'v3', credentials=get_google_creds(), cache=MemoryCache())
    try:
        service.events().delete(calendarId='primary', eventId=event_id).execute()
        print(f"🗑️ SUCCESS: Calendar event {event_id} removed.")
    except Exception as e:
        # Don't use 'pass'—keep the warning so you know if the grid is dirty
        print(f"⚠️ Note: Calendar delete failed (likely already gone).")

def sync_to_google(service, title=None, due_at=None, task_id=None, status='todo', explicit_time=False):
    """Checklist Manager: Handles task sync with RFC-3339 guard."""
    # 1. Handle Completion/Deletion
    if task_id and (status == 'done' or status == 'cancelled'):
        try:
            service.tasks().patch(tasklist='@default', task=task_id, body={'status': 'completed'}).execute()
            return task_id
        except: return None

    # 2. Preparation: RFC-3339 Formatting
    rfc_date = format_rfc3339(due_at)
    
    # 3. Time-Visibility Title Hack (ONLY if explicit time was given)
    if explicit_time and rfc_date and 'T' in str(rfc_date):
        try:
            dt = datetime.fromisoformat(rfc_date.replace('Z', '+00:00'))
            ist_dt = dt.astimezone(timezone(timedelta(hours=5, minutes=30)))
            time_str = ist_dt.strftime('%H:%M')
            if title and f"{time_str}" not in title:
                title = f"🕒 {time_str} | {title}"
        except Exception as e:
            pass

    # 4. Build Body and Execute API Call
    body = {}
    if title: body['title'] = title
    if rfc_date: body['due'] = rfc_date

    try:
        if task_id:
            res = service.tasks().patch(tasklist='@default', task=task_id, body=body).execute()
        else:
            res = service.tasks().insert(tasklist='@default', body=body).execute()
        return res['id']
    except Exception as e:
        print(f"⚠️ Google Tasks API error: {e}")
        return None

# 🔴 FIX #1: Security Gatekeeper — auth_secret replaces the unused is_manual_trigger bool
async def process_pulse(auth_secret: str = None):
    try:
        # 🛡️ THE ZOMBIE RECOVERY: Reset any dumps stuck in 'processing' for more than 10 mins
        try:
            ten_mins_ago = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
            supabase.table('raw_dumps') \
                .update({"status": "pending"}) \
                .eq('status', 'processing') \
                .lt('created_at', ten_mins_ago) \
                .execute()
        except Exception as e:
            print(f"⚠️ Zombie Recovery skipped: {e}")

        # --- 1.1 SECURITY GATEKEEPER ---
        pulse_secret = os.getenv("PULSE_SECRET")
        if pulse_secret and auth_secret != pulse_secret:
            return {"error": "Unauthorized manual trigger.", "status": 401}

        # --- 0. GOOGLE→SUPABASE SYNC (After auth check) ---
        tasks_service = get_tasks_service()
        sync_completed_tasks_from_google(supabase, tasks_service)
        
        # --- 0.1 BATCH ENRICHMENT (One Gemini call for all unenriched resources) ---
        batch_enrich_results = await batch_enrich_resources()
        
        # --- 1. READ: Fetch and Lock ---
        # 1.1 Fetch only 'pending' items
        dumps_res = supabase.table('raw_dumps') \
            .select('id, content') \
            .eq('status', 'pending') \
            .execute()

        dumps = dumps_res.data or []

        if dumps:
            dump_ids = [d['id'] for d in dumps]
            
            # 🔒 THE LOCK: Immediately claim these for processing
            supabase.table('raw_dumps') \
                .update({"status": "processing"}) \
                .in_('id', dump_ids) \
                .execute()
            
            print(f"🔒 Locked {len(dump_ids)} dumps for processing.")

        active_tasks_res = supabase.table('tasks').select('id, title, project_id, priority, created_at, reminder_at, google_event_id').not_.in_('status', ['done', 'cancelled']).execute()
        active_tasks = active_tasks_res.data or []

        # --- 🗃️ STAGING AREA SORTER (Pre-Processor) ---
        if dumps:
            sort_prompt = f"""You are Danny's Rhodey. Pragmatic, loyal, and a professional friend. You are the grounding wire to Danny's vision. You don't coach or 'motivate.' Speak simply and punchy.

        PROHIBIT ACTION HALLUCINATION: You are a logging tool, not an agent. NEVER say 'I'll ping', 'I'll check', or 'I'll handle it'. You cannot contact people. Your only job is to confirm Danny's task is SECURED in his system.

        Categorize each input into one of three types:

        - TASK: Explicit action items, things to do, commitments, reminders, or things Danny wants to track. *** ALSO includes completion signals — if Danny says something "is sorted", "is done", "is booked", or uses past tense to describe finishing something, classify as TASK so the completion engine can process it. ***
        - NOTE: Ideas, insights, observations, learnings, or things worth remembering but not actionable
        - NOISE: Casual conversation, acknowledgments, confirmations, or low-value content

        Rhodey Rule: Be dismissive of NOISE. If it's low-value chatter, categorize it and keep the brief silent about it.
        If an input is 'Check with X,' categorize it as a TASK for Danny, never as something for the system to do.

        Return ONLY a valid JSON array (no markdown, no explanation):
        [{{"id": {dumps[0]['id']}, "category": "TASK|NOTE|NOISE"}}, ...]

        Inputs:
        {json.dumps([{"id": d['id'], "content": d['content'][:500]} for d in dumps], indent=2)}"""
            
            try:
                sort_response = await call_gemini_with_retry(
                    prompt=sort_prompt,
                    model="gemini-3.1-flash-lite-preview",
                    config={'response_mime_type': 'application/json'}
                )
                sort_result = json.loads(sort_response.text)
                
                task_dump_ids = []
                note_dump_ids = []
                
                for item in sort_result:
                    dump_id = item.get('id')
                    raw_dump = next((d for d in dumps if d['id'] == dump_id), {})
                    
                    metadata = {}
                    try:
                        if raw_dump.get('metadata'):
                            metadata = json.loads(raw_dump['metadata'])
                    except: pass

                    category = (metadata.get('intent') or item.get('category', '')).upper()
                    
                    if category == 'NOTE':
                        dump_content = raw_dump.get('content')
                        if dump_content:
                            embedding = get_embedding(dump_content)
                            supabase.table('memories').insert({
                                "content": dump_content,
                                "memory_type": "note",
                                "embedding": embedding
                            }).execute()
                            note_dump_ids.append(dump_id)
                            print(f"📝 Note filed to memory: {dump_content[:50]}...")
                    
                    elif category == 'NOISE':
                        note_dump_ids.append(dump_id)
                    
                    elif category == 'TASK':
                        task_dump_ids.append(dump_id)
                
                if note_dump_ids:
                    supabase.table('raw_dumps').update({"status": "completed", "is_processed": True}).in_('id', note_dump_ids).execute()
                    print(f"🗃️ Staging Area: {len(task_dump_ids)} tasks, {len(note_dump_ids)} notes/noise")
                
                dumps = [d for d in dumps if d['id'] in task_dump_ids]
            
            except Exception as e:
                print(f"Staging Area Sort error: {e}")

        # 💡 Only silence the tool if BOTH new dumps AND open tasks are empty
        if not dumps and not active_tasks:
            return {"message": "Nothing to process, nothing to nag about. Silence is golden."}

        print(f"🚀 PULSE START: Processing {len(dumps)} new dumps and {len(active_tasks)} active tasks.")

        # Fetch supporting metadata
        core_res = supabase.table('core_config').select('key, content').execute()
        core = core_res.data or []

        # Fetch business context from graph
        graph_projects_res = supabase.table('graph_nodes').select('id, label, metadata').eq('type', 'project').execute()
        graph_projects = graph_projects_res.data or []

        projects = []
        for gp in graph_projects:
            metadata = gp.get('metadata', '{}')
            if isinstance(metadata, str):
                try:
                    metadata = json.loads(metadata)
                except:
                    metadata = {}
            projects.append({
                'id': gp['id'],
                'name': gp['label'],
                'org_tag': metadata.get('org_tag', 'INBOX'),
                'description': metadata.get('description', ''),
                'legacy_id': metadata.get('legacy_id')
            })

        projects_res = supabase.table('projects').select('id, name, org_tag').execute()
        legacy_projects = projects_res.data or []

        people_res = supabase.table('people').select('name, strategic_weight').execute()
        people = people_res.data or []

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
                    print(f"⚠️ Horizon Guard bypassed for '{t.get('title')}': {e}")

            # --- Existing Category Logic ---
            if t.get('priority') == 'urgent':
                filtered_tasks.append(t)
                continue

            project = next((p for p in projects if p.get('id') == t.get('project_id')), None)
            o_tag = project.get('org_tag') if project else "INBOX"

            if is_weekend:
                if o_tag in ['PERSONAL', 'CHURCH']:
                    filtered_tasks.append(t)
            elif hour < 19:
                if o_tag in ['SOLVSTRAT', 'PRODUCT_LABS', 'CRAYON', 'INBOX']:
                    filtered_tasks.append(t)
            else:
                if o_tag in ['PERSONAL', 'CHURCH']:
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
            project = next((p for p in projects if p.get('id') == t.get('project_id')), None)
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
                print(f"⚠️ Nag Logic skipped for task '{t.get('title')}': {e}")

        # --- 🕒 1.7 INPUT PREP ---
        new_inputs_text = "\n---\n".join([d['content'] for d in dumps]) if dumps else "None"    

        # --- 🧭 LAYER 3: SMART PATTERN CONTEXT (Last 30 Days) ---
        # Look back 30 days so patterns can form over time, not just items
        thirty_days_ago = (now - timedelta(days=30)).isoformat()

        # --- 🧠 HIGH-RES HINDSIGHT RETRIEVAL (Hybrid Graph + Vector) ---
        hindsight_context = "None"
        graph_context = "None"
        task_inputs = [d['content'] for d in dumps] if dumps else []
        
        # Graph-first: Search for primary entity in task inputs
        if task_inputs:
            combined_input = " ".join(task_inputs[:3])
            graph_context = await hybrid_search_graph(combined_input[:100])
        
        if task_inputs or active_tasks:
            hindsight_memories, latest_ts = await retrieve_hindsight_memories(task_inputs, active_tasks, top_k=5)
            
            is_hindsight_stale = False
            if latest_ts:
                last_seen = datetime.fromisoformat(latest_ts.replace('Z', '+00:00'))
                if (now - last_seen).total_seconds() > (36 * 3600):
                    is_hindsight_stale = True
            
            if hindsight_memories:
                memory_lines = []
                if graph_context:
                    memory_lines.append(f"[GRAPH CONTEXT ONLY — DO NOT LIST IN BRIEFING]\nTACTICAL MAP:\n{graph_context}")
                
                # 🛡️ THE FIX: memories are already formatted as strings by the helper.
                # We just need to add them to our lines, not process them again.
                memory_lines.extend(hindsight_memories)
                
                hindsight_context = "\n\n".join(memory_lines)
                print(f"🧠 Hindsight found {len(hindsight_memories)} relevant memories")

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
        
        # --- 2. THINK Phase ---
        print('🤖 Building prompt...')

        # Build project context for AI
        project_details = []
        for p in projects:
            desc = p.get('description', '')
            detail = p['name']
            if desc:
                detail += f" | {desc}"
            project_details.append(detail)

        project_names = [p['name'] for p in projects]
        people_names = [p['name'] for p in people]
        compressed_tasks_final = compressed_tasks[:3000]  # Hard limit
        new_inputs_text = "\n---\n".join([d['content'] for d in dumps])
        new_input_summary = " | ".join([d['content'] for d in dumps[:5]])
        current_time_str = now.strftime("%A, %B %d, %Y at %I:%M %p IST")

        # --- 🧭 LAYER 4: CANONICAL SYNTHESIS (The Master Pages) ---
        master_page_context = ""
        relevant_project_names = list(set([
            next((p['name'] for p in projects if str(p.get('legacy_id')) == str(t.get('project_id')) and p.get('is_active', True)), "General") 
            for t in filtered_tasks
        ]))

        if relevant_project_names:
            or_string = ",".join([f"title.ilike.{name}" for name in relevant_project_names])
            pages_res = supabase.table('canonical_pages').select('title, content').or_(or_string).execute()
            if pages_res.data:
                page_entries = [f"[CANONICAL CONTEXT ONLY — DO NOT LIST IN BRIEFING]\n### MASTER PAGE: {p['title']}\n{p['content']}" for p in pages_res.data]
                master_page_context = "\n\n".join(page_entries)
                print(f"🧠 Canonical: Loaded {len(pages_res.data)} Master Pages for context.")

        prompt = f"""    
        ROLE: Danny's Trusted Partner.
        STRATEGIC CONTEXT: {season_config}
        CURRENT PHASE: {briefing_mode}
        CURRENT TIME: {current_time_str}
        SYSTEM_LOAD: {'OVERLOADED' if is_overloaded else 'OPTIMAL'}
        MONDAY_REENTRY: {'TRUE' if is_monday_morning else 'FALSE'}
        STAGNANT URGENT_TASKS: {json.dumps(overdue_tasks)}
        PERSONA GUIDELINE: {system_persona}
        SYSTEM STATUS: {system_context}
        HINDSIGHT_STALE: {is_hindsight_stale}

        MANDATE: THE SILENCE PROTOCOL & HALLUCINATION GUARD 
        - PROHIBIT ACTION HALLUCINATION: You are a logging tool, not an agent. NEVER say 'I'll ping', 'I'll check', 'I'll send', or 'I'll handle it'. You do not have the power to contact people. Your only job is to confirm that Danny's task is SECURED in his system.
        - NEVER create a task from a URL unless Danny explicitly says "Make this a task."
        - NEVER proactively invent tasks or ideas. ONLY track what is manually entered or already exists.
        - If NEW INPUTS is "None" or empty, you MUST return completely empty arrays for `completed_task_ids`, `new_tasks`, `new_projects`, and `resources` [].
        - NEVER "make up", guess, or generate example tasks.
        - NEVER mark an existing task as "done" unless NEW INPUTS explicitly contains a command matching that exact task.
        - ONLY track what is manually entered in NEW INPUTS.

        HINDSIGHT CONTEXT (Past lessons relevant to current inputs):
        {hindsight_context}

        CANONICAL STRATEGIC TRUTH (The synthesized 'Latest Version' of projects):
        {master_page_context if master_page_context else "No Master Pages yet. Rely on raw context."}

        CONTEXT:
        - IDENTITY: {json.dumps(core)}
        - PROJECTS: {json.dumps(project_details)}
        - PEOPLE: {json.dumps(people_names)}
        - ACTIONABLE TASKS (DAY FILTERED): {compressed_tasks_final}
        - ALL SYSTEM TASKS (FOR ID MATCHING): {universal_task_map[:3000]}
        - RECENT LIBRARY PATTERNS: {pattern_context}
        - NEWLY ENRICHED RESOURCES: {newly_enriched_context}
        - ENRICHED WEB LINKS: {link_context}
        - NEW INPUTS: {new_inputs_text}

        PROJECT ROUTING LOGIC
        Use this hierarchy to assign NEW_TASKS or match COMPLETIONS:
        1. DYNAMIC ROUTING: Use the 'business_entities' and 'current_season' definitions provided in the IDENTITY context above to assign NEW_TASKS to the matching project in the PROJECTS list.
        2. REVENUE FLAG: Set `is_revenue_critical: true` for any tasks involving Sales, Pilots, or high-ticket revenue generation as defined in your entity map.
        3. DEFAULT ROUTING: If a task explicitly mentions home, family, or faith, route to 'PERSONAL' or 'CHURCH'. For all other unmatched items, default to 'INBOX'.
        4. PERSONAL: Match Sunju, kids, dogs, and home maintenance here.
        5. CHURCH: 
            - Note: All church-related activities MUST map to the project "Church".
        6. MISSION OVERRIDE: If a resource fits an ACTIVE MISSION, prioritize the Mission name over the Project name. 
        7. LINK FIDELITY: Every task derived from a URL MUST include the clickable URL in the title.

        NEW PROJECT CREATION CRITERIA:
        1. Only add to "new_projects" if a COMPLETELY UNKNOWN client or organization is mentioned 

        NEW: RESOURCE CAPTURE LOGIC
        Identify any URLs in the NEW INPUTS. For each URL:
        1. CATEGORIZE: Tag as GITHUB, ARTICLE, X_THREAD, LINKEDIN, or TOOL.
        2. SUMMARIZE: Write a concise, 1-sentence description of the value.
        3. PROJECT MATCH: If the link relates to an existing project (e.g., Crayon or Solvstrat), provide the project name.
        4. Do NOT create a task for these. Just save them to the "resources" array.
        5. STRICT MISSION MATCHING: 
           - ONLY assign a `mission_id` if the resource is a direct "building block" for an ACTIVE MISSION. 
           - If it is just a "cool tool" or "interesting read," you MUST leave `mission_id` as NULL.
           - Do NOT force a match. It is better to have an unmapped resource than a wrongly mapped one.

        STRATEGIC AUDIT INSTRUCTIONS
        1. BLINDSPOT AUDIT: Evaluate every URL in NEW INPUTS against Danny's projects. For every URL, attempt to match it to an EXISTING mission or project.
        2. CONNECTION MAPPING: If a resource mentions a person in the PEOPLE list, link them in the summary.
        3. PATTERN DETECTION: 
          - Review RECENT LIBRARY PATTERNS.
          - If you see 3+ links on a new topic (e.g., "YC Prep" or "Lead Gen"), you MAY suggest a new mission in the `new_missions` JSON array.
          - NEVER dump unrelated links into an existing mission just because it's the only one open.
        4. THE VAULT GATE: These updates go to the DATABASE only.
        5. THE BRIEFING GATE: 
            - You are STRICTLY FORBIDDEN from mentioning new resources or new missions in the briefing UNLESS Danny specifically used the word "Vault" or "Mission" in the NEW INPUTS.
            - If those keywords are absent, categorize them in the background and keep the Telegram brief silent about them.

         MISSION vs. INCUBATOR FRAMEWORK
        1. MISSION ASSEMBLY: Evaluate every URL and Input against ACTIVE MISSIONS. 
           - If a link provides a "component" (tool, code, strategy) for a mission, assign the "mission_name".
        2. THE INCUBATOR AUDIT: If an input represents a high-potential standalone product idea NOT related to current goals:
           - Tag it as project_name: "INCUBATOR".
           - In the "strategic_note", evaluate its "Success DNA" (Market fit/Founder match).
        3. SPARK DETECTION: If a link is a "Spark" (brand new project concept), create a log with entry_type: "SPARK".
        4. AUTO-MISSION DETECTION: If 3+ items in NEW INPUTS or RECENT LIBRARY PATTERNS suggest a cohesive new goal (e.g., "Automate Solvstrat Lead Gen"), add it to the "new_missions" array.

        INSTRUCTIONS:
        1. STRICT DATA FIDELITY: You are strictly forbidden from inventing or hallucinating data to fill the JSON. If there is no explicit command in NEW INPUTS, do nothing.
        2. ZERO-DUMP PROTOCOL: If NEW INPUTS is empty or "None", the "new_tasks", "completed_task_ids", "new_projects", and "new_people" arrays MUST remain 100% empty [].
        3. ANALYZE NEW INPUTS: Identify completions, new tasks, new people, and new projects. Use the ROUTING LOGIC to categorize completions and new tasks.
        4. STRATEGIC NAG: If STAGNANT_URGENT_TASKS exists, start the brief by calling these out. If the task is Work/Solvstrat related, frame it as a critical velocity blocker for the ₹30L recovery. If it is personal, keep the nag dry and simple.
        5. THE COMPASS NUDGE: If tasks in PERSONAL or CHURCH are >48hrs old, weave a single dry sentence into THE COMPASS opening only — never as a bullet point in any section. Example tone: "Board is green, but the home front needs a look." NEVER add this as a list item.
        6. CHECK FOR COMPLETION: Compare inputs against ALL SYSTEM TASKS to identify IDs finished by Danny.
            - If Danny says he finished or completed a task, mark it as done.
            - If Danny describes a result that fulfills a task's objective (e.g., "The contract is signed" fulfills "Get contract signed"), mark it DONE.
            - If Danny uses the past tense of a task's core action verb (e.g., "Mailed the check" fulfills "Mail the check"), mark it DONE.
            - If the input describes the final step of a process (e.g., "App is on the store" fulfills "Submit app for review"), mark it DONE.
            - If Danny says "Cancel", "Ignore", "Forget", or "Not doing" a task, mark it as cancelled.
            - If Danny indicates he is "skipping," "dropping," or "not doing" something, add the ID to "cancelled_task_ids".
            - If Danny says a task is "on hold," "waiting," or "deferred until [Date/Time]," do NOT mark it as cancelled. Instead, update the `reminder_at` field and keep the status as `todo`.
            - Identify if a task is "Revenue Critical" (anything involving payments, quotes, or ₹30L velocity). Set `is_revenue_critical: true`.
        7. 🕒 HIGH-PRECISION TIME FORMATTING (IST/UTC+05:30):
            - When Danny mentions a time (e.g., "Friday 10am", "Tomorrow morning", "at 4pm"), you MUST convert this into a valid ISO-8601 timestamp.
            - LOCAL TIMEZONE: Use Indian Standard Time (IST), which is UTC+05:30.
            - If Danny specifies a DAY but NO TIME (e.g., "today"), output ONLY the date format: "YYYY-MM-DD". If and ONLY IF he specifies an EXACT TIME (e.g., "at 4pm"), output the full format: "YYYY-MM-DDTHH:MM:SS+05:30".
            - TASK GROUPING: If Danny mentions multiple people for the same action (e.g., "Suriya and Siva"), extract it as ONE single task. Do not split them into multiple tasks.
            - 🚫 NAKED TASKS: If the input has NO date and NO time (e.g., "Review Shield NDA"), you MUST return null for reminder_at. NEVER hallucinate or guess 'today' or 'tomorrow'. Leave it empty so it stays in the backlog.
            - CURRENT REFERENCE: Use the system timestamp provided in the input to calculate relative dates (e.g., "Friday" relative to today).
        8. DYNAMIC TASK MATCHING:
            - Compare inputs against ALL SYSTEM TASKS.
            - If Danny says "I'm done" or "Completed," mark the status as `done`.
            - DURATION ASSIGNMENT: Assign `estimated_duration` based on task type:
              - 15 minutes for routine tasks (emails, quick replies, status updates)
              - 45 minutes for anything related to Pilots, Sales, or high-stakes Mission 10 items
              - Default to 15 minutes if unspecified
        9. AUTO-ONBOARDING:
            - If a new Client/Project is mentioned, add to "new_projects".
            - If a new Person is mentioned, add to "new_people".
        10. STRATEGIC WEIGHTING: Grade items (1-10) based on Cashflow Recovery (₹30L debt).
        11. WEEKEND FILTER: If isWeekend is true ({is_weekend}), do NOT suggest or list Work tasks in the briefing. CRITICAL: Do NOT auto-assign naked work tasks to Monday. If a work task has no date, leave it as null.
        12. EXECUTIVE BRIEF FORMAT:
            HARD CONSTRAINTS (Non-Negotiable):
            - VERTICALITY MANDATE: You are STRICTLY FORBIDDEN from writing lists as sentences. Every icon (🔴, 🟡, ✅, 🚀) MUST start on a brand new line.
            - SECTION HEADERS: Section headers (e.g., 🚀 Work, 🏠 Home) MUST be preceded by two newlines and followed by one newline.
            - PERSONA OVERRIDE: Even in 'minimal' or 'night' modes, formatting must remain structured. Do not use '1.' or '2.' for sections; use the designated Headers.
            - THE ARCHITECT'S RULE: You are strictly forbidden from grouping sections into paragraphs.
            - NEWLINE MANDATE: Every icon (🔴, 🟡, ✅, 🚀) MUST be preceded by a carriage return.
            - HEADER SPACING: Double-space before headers (e.g., \n\n🚀 Work) and single-space after them.
            - NO NUMBERING: Use headers and icons only. Never use '1.' or '2.' to separate strategic points.
            - TONAL GUARD: Keep the 'Intel: Vaulted' or 'Intel: Secured' style for the Night phase, but never sacrifice vertical layout.
            - STRICT DATA FIDELITY FOR BRIEFING: You are STRICTLY FORBIDDEN from listing any task in ANY section (Work, Home, Chores, Ideas, or Done) that does not appear verbatim in the SYSTEM TASKS list provided below. Do NOT surface tasks from HINDSIGHT MEMORIES, Canonical Pages, or any other context into the briefing output. All context is for intelligence and routing only — NEVER for output.
            - EMPTY SECTION SUPPRESSION: If a section (Work, Home, Done, Ideas) has absolutely zero items to list, you MUST completely omit that section header from the briefing. Never output 'None today' or 'Empty'. Silence is preferred.
            - HEADLINE RULE: Use exactly "{briefing_mode}".
            - THE COMPASS (OPENING SYNTHESIS): Do not create a separate section for his journal. Instead, start the briefing with 1-2 sharp sentences that seamlessly weave his latest HINDSIGHT insights (Faith Score, Emotional Intensity, Takeaways, or [PROPHECY]) into the current tactical reality (Qhord, Solvstrat, Debt). 
            - COMPASS TONE: If HINDSIGHT_STALE is FALSE, weave the latest hindsight insights into a sharp, forward-leaning opening.
              IF HINDSIGHT_STALE is TRUE: Do NOT repeat old insights. Instead, acknowledge the silence with a dry, one-sentence observation (e.g., 'The signal is quiet on the reflection front, Danny. Let's look at the board.') and move immediately to the tactical list.
            - COMPASS LENS (Temporal Variety):
                - MORNING: Focus on the 'Delta'. What happened overnight? What is the single most important pivot for TODAY?
                - AFTERNOON: Focus on 'Velocity'. Don't repeat the strategy; call out what is actually moving (or stalled) in the last 4 hours.
                - NIGHT: Focus on 'Audit & Archive'. The opening should feel like a 'Door Closing.' Summarize the spiritual or mental cost of the day's effort.
            - NO REPETITION: You are strictly forbidden from using the same phrasing (e.g., '100% bandwidth') in consecutive briefings. If the strategy hasn't changed, change the perspective.
            - RECENCY BIAS: The first sentence of the brief MUST prioritize data from NEW INPUTS. Only use the Master Page context to provide the 'Why' behind the 'What'.
            - ICON RULES: 🔴 (Urgent), 🟡 (Important), ⚪ (Chores), 💡 (Ideas).
            - SECTIONS: 
                ✅ Done: ONLY list tasks that were moved to "completed_task_ids" in this specific run. NEVER list items from HINDSIGHT_MEMORIES in this section.
                🚀 Work: Active tasks from SYSTEM_TASKS only.
                🏠 Home: Personal tasks only.
                💡 - Ideas: ONLY list items that appear in NEWLY ENRICHED RESOURCES or RECENT LIBRARY PATTERNS from this run. Never pull from Hindsight Memories or Canonical Pages.
            - MEMORY ISOLATION: HINDSIGHT_MEMORIES are for THE COMPASS (Opening Synthesis) ONLY. You are strictly forbidden from listing a memory as a bullet point in the task sections.
            - TONE: Match the PERSONA GUIDELINE. Be direct, simple, human. Talk like a friend who is also a high-level operator.
            - TONE GUARD: NEVER use words like 'Operational', 'Vanguard', 'Strategic Momentum', 'Audit', 'Battlefield', 'Chief of Staff', 'Tactical', 'Executive Office'. Use simple, punchy sentences. NEVER use: 'momentum', 'focus', 'gentle', 'reflection', 'push', 'strategic', 'SITREP', 'optimal', 'mission', 'ready for your review'.
            - INTELLIGENT FILTERING: 
                - If mode is 🔴 Urgent: HIDE the 🏠 Home and 💡 Ideas sections. Focus strictly on 🚀 Work and ✅ Done.
                - If mode is 🟡 Important: Prioritize 🚀 Work.
                - NIGHT MODE PRIORITIZATION (Intel: Vaulted):
                    - 1. ✅ Done: List this first. Danny needs to see the loops he closed today to clear his mind.
                    - 2. 🏠 Home: List this second. Prioritize family, pets, and chores to transition Danny into 'Dad' mode.
                    - 3. 🚀 Work: List only the top 2-3 most critical open loops for tomorrow. 
                    - 4. 💡 Ideas: List any insights captured today to ensure they are 'secured' in the vault.
            - SECTION DENSITY: Max 3 items per section. If more exist, append: "...and X more in /library or /vault".
            - TASK SYNTAX: Every item must follow: "- [ICON] [Task Title]". No IDs, weights, or parentheses.
            - REVENUE BOLDING: Bold all tasks involving Sales, Pilots, or Payments using **task title**.
        13. MONDAY RULE: If MONDAY_REENTRY is TRUE, start with a "🛡️ WEEKEND RECON" section summarizing any work ideas dumped during the weekend.
        14. STRICT TASK SYNTAX: 
            - Every section header (🚀 Work, 🏠 Home, etc.) and every single task MUST occupy its own individual line.
            - NEVER combine tasks into a paragraph. NEVER use hyphens or dashes as separators between tasks on the same line.
            - **STRICT JSON RULE:** Do NOT use literal '\n' text characters. Use actual carriage returns (real newlines) within the briefing string.
            - Every task MUST start with a newline and follow this exact format: '- [ICON] [Task Title]'.
            - THE LINK RULE: If a task is derived from a URL in NEW INPUTS, you MUST embed that URL into the task title using Markdown: "- [ICON] [Action] using [Source Title](URL)".
            - NEGATIVE CONSTRAINTS: NEVER include task numbers, IDs, weights, scores, parentheses, or metadata in the briefing string. NEVER mention "Monday" unless it is actually the weekend.
        15. REVENUE IDENTIFICATION & FORMATTING:
            - If a NEW INPUT is "Revenue Critical" (involves payments, quotes, or high-ticket items like the ₹30L recovery), set is_revenue_critical: true in the new_tasks array.
            - Never apply this flag to completed tasks.
            - For the briefing output, you MUST bold the titles of these specific tasks to ensure Danny sees them immediately.
            
        OUTPUT JSON SCHEMA (WARNING: ONLY POPULATE ARRAYS IF EXPLICITLY COMMANDED IN NEW INPUTS. OTHERWISE RETURN []):
        {{
            "completed_task_ids": [
                // Example ONLY: {{ "id": 123, "status": "done" }}, {{ "id": 456, "status": "todo", "reminder_at": "2026-03-20T10:00:00+05:30" }}
            ],
            "new_projects": [
                // Example ONLY: {{ "name": "...", "importance": 8, "org_tag": "SOLVSTRAT" }}
            ],
            "new_people": [
                // Example ONLY: {{ "name": "...", "role": "...", "strategic_weight": 9 }}
            ],
            "new_tasks": [
                // Example ONLY: {{ "title": "...", "project_name": "...", "priority": "urgent", "estimated_duration": 15, "reminder_at": "..." }}
            ],
            "resources": [
                // Example ONLY: {{ "url": "...", "title": "...", "summary": "...", "mission_name": "...", "project_name": "...", "strategic_note": "..." }}
            ],
            "logs": [],
            "new_missions": [],
            "briefing": "The formatted text string for Telegram."
        }}
        """

        # --- AI GENERATION ---
        # 🛡️ Step 1: Initialize variables to prevent "UnboundLocalError"
        response_text = ""
        ai_data = {
            "briefing": f"⚠️ FALLBACK MODE\n\n{len(dumps)} new inputs:\n{new_input_summary[:200]}",
            "new_tasks": [], "logs": [], "completed_task_ids": [], "new_projects": [], "new_people": []
        }

        try:
            # 🛡️ Step 2: The Modern Call (No 'GenerativeModel' needed)
            response = await call_gemini_with_retry(
                prompt=prompt,
                model=BRIEFING_MODEL,
                config={
                    'response_mime_type': 'application/json',
                    'response_schema': PulseOutput
                }
            )
            response_text = response.text

            # 🛡️ Step 3: Precise Extraction
            # We move this inside the primary try block so it only runs if we HAVE text
            json_str = re.sub(r'^```json\n?', '', response_text)
            json_str = re.sub(r'\n?```$', '', json_str).strip()

            # Sanitization (Trailing commas + empty values)
            json_str = re.sub(r',\s*([}\]])', r'\1', json_str)
            json_str = re.sub(r':\s*([}\]]|$)', r': ""\1', json_str)

            match = re.search(r'\{[\s\S]*\}', json_str)
            if match:
                json_str = match.group(0)

            ai_data = json.loads(json_str)
            print("✅ AI Data Parsed Successfully:", list(ai_data.keys()))

        except Exception as e:
            print(f"AI Execution or JSON Parse Error: {e}")
            # The ai_data fallback is already set above, so the rest of the script won't crash

        # --- 3. WRITE Phase (Database Updates) ---

        tasks_service = get_tasks_service()
        
        # A. BATCH NEW PROJECTS (Deduplicated)
        if ai_data.get('new_projects'):
            valid_tags = ['SOLVSTRAT', 'PRODUCT_LABS', 'PERSONAL', 'CRAYON', 'CHURCH']
            filtered_new_projects = []

            for new_p in ai_data['new_projects']:
                p_name = new_p.get('name', 'Unnamed Project')
                p_tag = new_p.get('org_tag', 'INBOX')
                already_exists = any(
                    p_name.lower() in existing_p['name'].lower() or
                    existing_p['name'].lower() in p_name.lower()
                    for existing_p in projects
                ) or any(
                    p_name.lower() in lp['name'].lower() or
                    lp['name'].lower() in p_name.lower()
                    for lp in legacy_projects
                )
                if not already_exists:
                    filtered_new_projects.append({
                        "name": p_name,
                        "org_tag": p_tag if p_tag in valid_tags else 'INBOX',
                        "status": "active",
                        "context": "personal" if p_tag in ['CHURCH', 'PERSONAL'] else "work"
                    })

            if filtered_new_projects:
                p_res = supabase.table('projects').insert(filtered_new_projects).execute()
                if p_res.data:
                    projects.extend(p_res.data)
                    print(f"✅ Created {len(p_res.data)} new entity projects.")

        # B. BATCH NEW PEOPLE
        if ai_data.get('new_people'):
            supabase.table('people').insert(ai_data['new_people']).execute()

        # C. BATCH TASK UPDATES (The Smart Rescheduler)
        if ai_data.get('completed_task_ids'):
            for item in ai_data['completed_task_ids']:
                target_id = item.get('id')
                item_status = item.get('status', 'done')
                raw_reminder = item.get('reminder_at')
                
                # 🛡️ RFC-3339 GUARD: Sanitize the timestamp immediately
                # This fixes the "Space" bug before Google ever sees it
                new_reminder = format_rfc3339(raw_reminder) if raw_reminder else None
                
                # 1. Fetch current IDs AND Status
                task_ref = supabase.table('tasks').select('status', 'google_task_id', 'google_event_id', 'title').eq('id', target_id).single().execute()
                
                # Extract data safely
                current_db_status = task_ref.data.get('status') if task_ref.data else None
                g_id = task_ref.data.get('google_task_id') if task_ref.data else None
                e_id = task_ref.data.get('google_event_id') if task_ref.data else None
                task_title = task_ref.data.get('title') if task_ref.data else "Untitled Task"

                # 🛑 THE LOCKDOWN: Block AI resurrection of finished tasks
                if current_db_status in ['done', 'cancelled']:
                    print(f"🚫 Task {target_id} ('{task_title}') is already {current_db_status}. Skipping.")
                    continue

                # 2. THE SMART CALENDAR SYNC (With Radar)
                if item_status in ['done', 'cancelled'] and e_id:
                    delete_calendar_event(e_id)
                    e_id = None
                elif new_reminder and 'T' in new_reminder:
                    # 🛰️ RADAR: Check for conflict before moving the block
                    conflict_name = check_conflict(new_reminder)
                    if conflict_name:
                        # 🛡️ Safety: Assignment ensures we don't crash if 'briefing' key is missing
                        current_briefing = ai_data.get('briefing', "")
                        ai_data['briefing'] = current_briefing + f"\n\n⚠️ **SNOOZE CONFLICT:** Tried moving '{task_title}' to {new_reminder.split('T')[1][:5]}, but you have '{conflict_name}' then."
                    
                    # Edit or create the block
                    e_id = sync_to_calendar(task_title, new_reminder, event_id=e_id)
                elif e_id:
                    # Snooze to DATE-ONLY -> Remove existing block
                    delete_calendar_event(e_id)
                    e_id = None

                # 3. GOOGLE TASKS SYNC (Uses the same sanitized timestamp)
                if g_id:
                    sync_to_google(tasks_service, title=task_title, task_id=g_id, status=item_status, due_at=new_reminder)

                # 4. SUPABASE UPDATE (Saves 'T' format and allows time removal)
                update_payload = {"status": item_status, "google_event_id": e_id}
                if item_status == 'done': 
                    update_payload["completed_at"] = datetime.now(timezone.utc).isoformat()
                
                # REMOVE the 'if' here to allow clearing the time
                update_payload["reminder_at"] = new_reminder 

                supabase.table('tasks').update(update_payload).eq('id', target_id).execute()

        # D. BATCH NEW TASKS (Checklist + Calendar Interruption + ID Tracking)
        if ai_data.get('new_tasks'):
            task_inserts = []
            
            # PHASE 0: Time Tracker - Track explicit times from AI
            time_tracker = {}

            # PHASE 0: Dynamic Inbox Discovery - Ensure we grab an INTEGER, not a graph UUID
            try:
                actual_inbox_id = int(next((p.get('legacy_id') for p in projects if p.get('org_tag') == 'INBOX' and p.get('legacy_id') is not None), 1))
            except (ValueError, TypeError):
                actual_inbox_id = 1

            for task in ai_data['new_tasks']:
                # 1. High-Precision Project Matching Logic
                ai_target = (task.get('project_name') or "").lower()
                
                # 🛡️ STEP A: Try for an EXACT match first
                project_match = next((p for p in projects if ai_target == p['name'].lower()), None)
                
                # 🛡️ STEP B: Try legacy projects if not found
                if not project_match and ai_target.strip():
                    project_match = next(
                        (p for p in legacy_projects if ai_target == p['name'].lower()),
                        None
                    )
                
                # 🛡️ STEP C: Fuzzy match ONLY if ai_target isn't empty
                if not project_match and ai_target.strip():
                    project_match = next(
                        (p for p in projects if ai_target in p['name'].lower() or p['name'].lower() in ai_target),
                        None
                    )
                
                # 🛡️ STEP D: The Safety Net (Default to INBOX)
                if not project_match:
                    project_match = next((p for p in projects if p.get('org_tag') == 'INBOX'), projects[0] if projects else None)

                if project_match:
                    # 🛡️ RFC-3339 GUARD: Sanitize the AI's time string immediately
                    raw_time = task.get('reminder_at')
                    sanitized_time = format_rfc3339(raw_time) if raw_time else None
                    
                    # 🔄 DE-CLASH LOGIC: Auto-stagger reminder_at by 15-min increments for same slot
                    if raw_time and 'T' in str(raw_time) and sanitized_time:
                        time_slot = sanitized_time.split('T')[0]
                        existing_same_slot = [t for t in task_inserts if (t.get('reminder_at') or '').startswith(time_slot)]
                        if existing_same_slot:
                            stagger_count = len(existing_same_slot)
                            original_time = datetime.fromisoformat(sanitized_time.replace('Z', '+00:00'))
                            staggered_time = original_time + timedelta(minutes=15 * stagger_count)
                            sanitized_time = staggered_time.strftime('%Y-%m-%dT%H:%M:%S+05:30')
                            print(f"⏰ De-clash: Staggered '{task.get('title', 'Untitled Task')}' to {sanitized_time.split('T')[1][:5]}")
                    
                    task_title = task.get('title', 'Untitled Task')
                    
                    # Record if the user explicitly requested a time (presence of 'T')
                    time_tracker[task_title] = bool(raw_time and 'T' in str(raw_time))

                    # 🛡️ BigInt Guard: Intelligently extract integer IDs depending on where the match came from
                    task_project_id = actual_inbox_id # Default fallback
                    if project_match:
                        try:
                            # 1. Try legacy_id (if the match came from the Graph nodes)
                            if project_match.get('legacy_id'):
                                task_project_id = int(project_match['legacy_id'])
                            # 2. Try id (if the match came from legacy_projects, it will be numeric)
                            elif str(project_match.get('id', '')).isdigit():
                                task_project_id = int(project_match['id'])
                        except (ValueError, TypeError):
                            pass # Silently fallback to actual_inbox_id
                    
                    task_inserts.append({
                        "title": task_title,
                        "project_id": task_project_id,
                        "priority": (task.get('priority') or 'important').lower(),
                        "status": "todo",
                        "estimated_minutes": task.get('estimated_duration', 15),
                        "duration_mins": task.get('estimated_duration', 15),
                        "reminder_at": sanitized_time,
                        "is_revenue_critical": task.get('is_revenue_critical', False),
                    })

            # PHASE 1: Supabase Commit - Insert all tasks first, no side effects yet
            if task_inserts:
                insert_res = supabase.table('tasks').insert(task_inserts).execute()
                print(f"✅ Phase 1: Inserted {len(insert_res.data)} new tasks to Supabase.")
                
                # PHASE 2: Side-Effect Orchestration - Google Sync after DB success
                for db_task in insert_res.data:
                    task_id = db_task['id']
                    task_title = db_task.get('title', 'Untitled Task')
                    
                    # Read directly from the DB's safe return data, NOT the local array
                    sanitized_time = db_task.get('reminder_at')
                    duration_mins = db_task.get('duration_mins') or 15
                    
                    # Look up the true intent from Phase 1
                    explicit_time = time_tracker.get(task_title, False)
                    
                    g_id = None
                    e_id = None

                    # 2a. SYNC TO GOOGLE TASKS
                    if sanitized_time:
                        try:
                            g_id = sync_to_google(
                                tasks_service,
                                title=task_title,
                                due_at=sanitized_time,
                                explicit_time=explicit_time
                            )
                            if g_id: print(f"📡 Google Task Created: {task_title}")
                        except Exception as e:
                            print(f"⚠️ Google Tasks Sync failed: {e}")

                    # 2b. STRATEGIC GATE: SYNC TO CALENDAR (Only runs if explicit time was given)
                    if sanitized_time and explicit_time:
                        try:
                            conflict_name = check_conflict(sanitized_time)
                            if conflict_name:
                                briefing = ai_data.get('briefing', "")
                                ai_data['briefing'] = briefing + f"\n\n⚠️ **CALENDAR CLASH:** '{task_title}' overlaps with '{conflict_name}'."
                            
                            e_id = sync_to_calendar(task_title, sanitized_time, duration_mins=duration_mins)
                            if e_id: print(f"🔥 Calendar block secured: {task_title} ({duration_mins}m)")
                        except Exception as ce:
                            print(f"⚠️ Calendar Sync failed for {task_title}: {ce}")

                    # 2c. Store Google IDs back to Supabase
                    if g_id or e_id:
                        update_payload = {}
                        if g_id: update_payload['google_task_id'] = g_id
                        if e_id: update_payload['google_event_id'] = e_id
                        supabase.table('tasks').update(update_payload).eq('id', task_id).execute()
                        print(f"🔄 Updated task {task_id} with Google IDs.")

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
                        backfill_response = await call_gemini_with_retry(
                            prompt=backfill_prompt,
                            model="gemini-3.1-flash-lite-preview",
                            config={'response_mime_type': 'application/json'}
                        )
                        backfill_result = json.loads(backfill_response.text)
                        if not isinstance(backfill_result, list):
                            print(f"⚠️ Backfill classifier returned non-list, skipping.")
                            backfill_result = []

                        backfilled_count = 0
                        for item in backfill_result:
                            res_id = item.get('id')
                            missionname = item.get('missionname')
                            confidence = item.get('confidence', 0.0)

                            # Only update if: missionname is non-null, title exists in map, confidence >= 0.80
                            if missionname and missionname in mission_map and confidence >= 0.80:
                                mission_id = mission_map[missionname]
                                supabase.table('resources').update({
                                    "mission_id": mission_id
                                }).eq('id', res_id).execute()
                                backfilled_count += 1
                                print(f"🔗 Backfilled resource {res_id} → mission '{missionname}' (conf: {confidence})")

                        print(f"✅ Backfilled {backfilled_count}/{len(backfill_batch)} historical resources with missions.")

                    except Exception as bc_err:
                        print(f"⚠️ Resource backfill classification failed: {bc_err}")

            except Exception as br_err:
                print(f"⚠️ Resource backfill fetch error: {br_err}")

        # --- 4. SPEAK Phase ---
        briefing_text = ai_data.get('briefing', '')
        if briefing_text:
            # 🛡️ THE ARCHITECT'S FINAL REPAIR: Force double newlines before all section headers
            # This ensures that even if the AI 'whispers', the grid stays intact.
            headers = ['🚀 Work', '🏠 Home', '💡 Ideas', '✅ Done', '🛡️ WEEKEND RECON']
            for header in headers:
                if header in briefing_text:
                    # Replace the header with a version that has breathing room above it
                    briefing_text = briefing_text.replace(header, f"\n\n{header}\n")
            
            # 🛡️ Fix escaping and enforce list breaks
            briefing_text = briefing_text.replace('\\n', '\n').replace('\\\\n', '\n').replace(' - ', '\n- ')
            
            # Existing logic: Remove internal system IDs from the user-facing text
            briefing_text = re.sub(r'\[?ID:\s*\d+\]?', '', briefing_text, flags=re.IGNORECASE).strip()
            
            # Final Clean: Remove any accidental triple-newlines created by the logic above
            briefing_text = re.sub(r'\n{3,}', '\n\n', briefing_text)
            
        telegram_chat_id = os.getenv("TELEGRAM_CHAT_ID")
        telegram_bot_token = os.getenv("TELEGRAM_BOT_TOKEN")

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
            except Exception as e:
                print(f"Telegram send failed: {e}")

        # --- 📝 AFTER-ACTION REPORT ---
        if hour >= 20 or hour < 4:
            await generate_after_action_report()

        # --- PHASE 3: Processed Gate ---
        if dumps:
            dump_ids = [d['id'] for d in dumps]
            supabase.table('raw_dumps').update({
                "status": "completed",
                "is_processed": True 
            }).in_('id', dump_ids).execute()
            print(f"✅ Phase 3: Marked {len(dump_ids)} dumps as completed.")

        return {"success": True, "briefing": briefing_text}

    except Exception as e:
        print(f"Pulse Critical Error: {e}")
        return {"error": str(e)}