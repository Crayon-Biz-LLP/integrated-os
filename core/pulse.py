import os
import json
import re
import asyncio
import httpx
import time
import random
import hashlib
from datetime import datetime, timedelta, timezone
from supabase import create_client, Client
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.discovery_cache import base
from google import genai
from pydantic import BaseModel, Field
from typing import List, Optional

# Import audit logger
import sys
sys.path.insert(0, os.path.dirname(__file__))
from audit_logger import info, warning, error, critical, audit_log_sync

# Import temporal lineage
from temporal_lineage import (
    create_versioned_memory,
    create_versioned_task,
    create_versioned_project,
    get_memory_history,
    detect_drift,
    get_state_at_time
)


def format_error(e: Exception) -> str:
    """Format exception for logging."""
    import traceback
    return traceback.format_exc() if hasattr(e, '__traceback__') else str(e)


def versioned_update(table_name: str, record_id: int, update_data: dict, user_id=None, change_source=None, change_reason=None):
    """
    Update a record using versioned insert pattern.
    Marks old record as is_current=FALSE, inserts new version.
    
    Args:
        table_name: 'tasks', 'memories', 'projects', 'resources'
        record_id: ID of record to update
        update_data: Fields to update
        user_id: Optional user making the change (for audit)
        change_source: Optional source of change (e.g., 'pulse_task_update')
        change_reason: Optional reason for change
    """
    try:
        # Get current record
        current = supabase.table(table_name).select('*').eq('id', record_id).execute()
        if not current.data:
            audit_log_sync("pulse", "WARNING", f"Record {record_id} not found in {table_name}")
            return False
        
        old_record = current.data[0]
        
        # Prepare new version
        new_record = {
            **{k: v for k, v in old_record.items() 
               if k not in ['id', 'created_at', 'version', 'is_current', 'supersedes_id', 'updated_at']},
            **{k: v for k, v in update_data.items() if v is not None},
            **{k: v for k, v in update_data.items() if v is None},
            'is_current': True
        }
        
        # Get next version number
        old_version = old_record.get('version',0) or 0
        new_record['version'] = old_version + 1
        new_record['supersedes_id'] = record_id
        
        # Insert new version FIRST (so failure doesn't orphan the old record)
        result = supabase.table(table_name).insert(new_record).execute()
        
        # Mark old as not current (only after new insert succeeds)
        supabase.table(table_name).update({"is_current": False}).eq('id', record_id).execute()
        
        # Log the change
        if change_source or change_reason:
            audit_log_sync("pulse", "INFO", 
                f"Versioned update: {table_name}:{record_id} v{new_record['version']}", 
                {"source": change_source, "reason": change_reason, "user_id": user_id})
        
        return bool(result.data)
        
    except Exception as e:
        # Fallback to regular update — include is_current=True to avoid orphaned records
        audit_log_sync("pulse", "WARNING", f"Versioned update failed for {table_name}:{record_id}, falling back to update: {e}")
        fallback_data = {**update_data, 'is_current': True}
        supabase.table(table_name).update(fallback_data).eq('id', record_id).execute()
        return True

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
OPENROUTER_BASE_URL = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1/chat/completions")
PULSE_ENABLE_OPENROUTER_FALLBACK = os.getenv("PULSE_ENABLE_OPENROUTER_FALLBACK", "true").lower() == "true"
PULSE_HTTP_REFERER = os.getenv("PULSE_HTTP_REFERER", "http://localhost:8000")
PULSE_APP_NAME = os.getenv("PULSE_APP_NAME", "Pulse")

GEMMA_FALLBACK_MODEL = "gemma-4-31b-it"
GEMMA_SPEED_MODEL = "gemma-4-26b-a4b-it"
OPENROUTER_MODEL = "nvidia/nemotron-3-super-120b-a12b:free"

RETRYABLE_ERRORS = ['503', '504', '500', 'disconnected', 'timeout', 'deadline exceeded', 'unavailable', 'overloaded', 'rate limit']
NON_RETRYABLE_ERRORS = ['401', '403', '400', 'invalid']

supabase: Client = create_client(
    os.getenv("SUPABASE_URL"), 
    os.getenv("SUPABASE_SERVICE_ROLE_KEY")
)

def is_already_in_email_queue(title: str) -> bool:
    """Check if a task title already exists in email_pending_tasks or tasks table."""
    try:
        keywords = [w for w in title.lower().split() if len(w) > 4]
        if not keywords:
            return False
        for kw in keywords[:3]:
            # Check pending queue
            result = supabase.table('email_pending_tasks')\
                .select('id')\
                .ilike('suggested_title', f'%{kw}%')\
                .is_('danny_decision', 'null')\
                .limit(1)\
                .execute()
            if result.data:
                audit_log_sync("pulse", "WARNING", f"⚠️  Duplicate guard: '{title}' matches pending email task (keyword: '{kw}'). Skipping.")
                return True
            # Check active tasks on board (only current versions)
            result = supabase.table('tasks')\
                .select('id')\
                .ilike('title', f'%{kw}%')\
                .eq('is_current', True)\
                .not_.in_('status', ['done', 'cancelled'])\
                .limit(1)\
                .execute()
            if result.data:
                audit_log_sync("pulse", "WARNING", f"⚠️  Duplicate guard: '{title}' matches existing task on board (keyword: '{kw}'). Skipping.")
                return True
        
        # Secondary: Semantic embedding check (high threshold to avoid false positives)
        embedding = get_embedding(title)
        similarity_res = supabase.rpc('match_memories', {
            'query_embedding': embedding,
            'match_count': 1,
            'match_threshold': 0.88
        }).execute()
        if similarity_res.data:
            score = similarity_res.data[0].get('similarity')
            if isinstance(score, (int, float)) and score > 0:
                audit_log_sync("pulse", "WARNING", f"⚠️ Semantic duplicate guard: '{title}' is semantically similar to an existing memory. Skipping.")
                return True
        
        return False
    except Exception as e:
        audit_log_sync("pulse", "WARNING", f"Duplicate guard check failed: {e}")
        return False  # Fail open — don't block task creation if check errors

gemini_client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))

EMBEDDING_MODEL = "gemini-embedding-2-preview"
EMBEDDING_DIMENSION = 768

BRIEFING_MODEL = "gemini-3-flash-preview"


def get_project_name(project: dict) -> str:
    """Normalize project object — handles both DB rows (name) and graph_nodes rows (label)."""
    if not isinstance(project, dict):
        return ""
    return (project.get("name") or project.get("label") or "").strip()


def build_routing_context(legacy_projects: list) -> str:
    """
    Dynamically builds project routing instructions from the DB.
    No hardcoded client names — new projects auto-register on next Pulse run.
    """
    lines = []

    id_to_name = {p['id']: p['name'] for p in legacy_projects}

    sorted_projects = sorted(
        legacy_projects,
        key=lambda p: (0 if p.get('parent_project_id') else 1, p.get('name', ''))
    )

    for p in sorted_projects:
        if p.get('status') not in ('active',):
            continue

        name = p.get('name', '').strip()
        if not name:
            audit_log_sync("pulse", "WARNING", f"⚠️ Project ID {p.get('id')} has no name, skipping routing context entry.")
            continue

        parent_id = p.get('parent_project_id')
        parent_name = id_to_name.get(parent_id) if parent_id else None
        parent_str = f" [child of {parent_name}]" if parent_name else ""

        desc = (p.get('description') or '').strip()
        detail = f"{name}{parent_str} | {desc}"

        keywords = p.get('keywords') or []
        if keywords:
            detail += f" | Keywords: {', '.join(keywords)}"

        lines.append(detail)

    return '\n'.join(f'  - {line}' for line in lines)


async def write_graph_edges_for_task(task_id: int, task_title: str, project_id: int = None, task_description: str = None, people_cache=None):
    """
    Add-on: Writes graph edges after a task is created.
    Non-blocking. If this fails, the task is already saved — no rollback needed.
    """
    try:
        task_node = supabase.table('graph_nodes') \
            .select('id') \
            .eq('type', 'task') \
            .filter('metadata->>task_id', 'eq', str(task_id)) \
            .maybe_single() \
            .execute()

        if task_node.data:
            task_node_id = task_node.data['id']
        else:
            new_node = supabase.table('graph_nodes').insert({
                "label": task_title,
                "type": "task",
                "metadata": {
                    "source": "tasks_table",
                    "task_id": task_id,
                    "project_id": project_id
                }
            }).execute()
            task_node_id = new_node.data[0]['id']

        if project_id:
            proj_node = supabase.table('graph_nodes') \
                .select('id') \
                .eq('type', 'project') \
                .filter('metadata->>project_id', 'eq', str(project_id)) \
                .maybe_single() \
                .execute()

            if proj_node and proj_node.data:
                existing = supabase.table('graph_edges') \
                    .select('id') \
                    .eq('source_node_id', task_node_id) \
                    .eq('target_node_id', proj_node.data['id']) \
                    .eq('relationship', 'BELONGS_TO') \
                    .maybe_single() \
                    .execute()

                if not existing.data:
                    supabase.table('graph_edges').insert({
                        "source_node_id": task_node_id,
                        "target_node_id": proj_node.data['id'],
                        "relationship": "BELONGS_TO",
                        "weight": 1.0,
                        "metadata": {"source": "task_engine", "task_id": task_id}
                    }).execute()

        search_text = f"{task_title} {task_description or ''}".lower()

        # Use cache if provided, otherwise fetch
        if people_cache is not None:
            all_people = people_cache
        else:
            all_people = supabase.table('people').select('id, name').execute().data or []

        for person in (all_people or []):
            if person['name'].lower() in search_text:
                person_node = supabase.table('graph_nodes') \
                    .select('id') \
                    .eq('type', 'person') \
                    .filter('metadata->>people_id', 'eq', str(person['id'])) \
                    .maybe_single() \
                    .execute()

                if person_node and person_node.data:
                    existing_edge = supabase.table('graph_edges') \
                        .select('id') \
                        .eq('source_node_id', task_node_id) \
                        .eq('target_node_id', person_node.data['id']) \
                        .eq('relationship', 'INVOLVES') \
                        .maybe_single() \
                        .execute()

                    if not existing_edge.data:
                        supabase.table('graph_edges').insert({
                            "source_node_id": task_node_id,
                            "target_node_id": person_node.data['id'],
                            "relationship": "INVOLVES",
                            "weight": 1.0,
                            "metadata": {
                                "source": "task_engine",
                                "task_id": task_id,
                                "matched_name": person['name']
                            }
                        }).execute()

        print(f"🕸️ Graph edges written for task {task_id}: '{task_title}'")

    except Exception as e:
        audit_log_sync("pulse", "WARNING", f"⚠️ Graph edge write failed (non-critical): {e}")


async def write_outcome_memory(task_title: str, project_name: str = None):
    """
    Writes a type:outcome memory when a task is completed.
    Non-blocking. Mirrors the same pattern as reflection writes in AAR.
    """
    try:
        label = f"Completed: {task_title}"
        if project_name:
            label += f" on {project_name}"
        
        embedding = await asyncio.to_thread(get_embedding, label)
        status = 'success' if embedding and any(embedding) else 'failed'
        supabase.table('memories').insert({
            "content": label,
            "memory_type": "outcome",
            "embedding": embedding,
            "embedding_status": status,
            "source": "pulse_outcome"
        }).execute()
        print(f"🧠 Outcome memory written: {label}")
    except Exception as e:
        audit_log_sync("pulse", "WARNING", f"⚠️ Outcome memory write failed (non-critical): {e}")


# 🛡️ CLEAN MODELS (Removed Config blocks to prevent API rejection)
class CompletedTask(BaseModel):
    id: int
    status: str
    reminder_at: Optional[str] = None

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

            should_retry = any(err in error_str for err in RETRYABLE_ERRORS)
            if should_retry and attempt < max_retries - 1:
                delay = base_delay * (2 ** attempt)
                audit_log_sync("pulse", "WARNING", f"⚠️ API Hiccup ({error_str}), retrying in {delay}s...")
                await asyncio.sleep(delay)
                continue
            else:
                raise


class SimpleResponse:
    """Simple response wrapper for OpenRouter responses."""
    def __init__(self, text: str):
        self.text = text


def _jitter(delay: float) -> float:
    """Add jitter to delay: +/- 25%"""
    return delay * (0.75 + random.random() * 0.5)


def parse_json_response(response_text: str) -> any:
    """Robust JSON parsing with extraction fallback."""
    if not response_text:
        raise ValueError("Empty response")
    
    text = response_text.strip()
    
    text = re.sub(r'^```json\n?', '', text)
    text = re.sub(r'\n?```$', '', text).strip()
    
    text = re.sub(r',\s*([}\]])', r'\1', text)
    
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    
    match = re.search(r'\{[\s\S]*\}|\[[\s\S]*\]', text)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            pass
    
    raise ValueError(f"Could not parse JSON from response: {text[:100]}...")


async def call_llm_with_fallback(
    prompt: str,
    model: str = None,
    config: dict = None,
    contents=None,
    is_critical: bool = True,
    require_json: bool = False
):
    """
    Multi-provider LLM call with fallback chain.
    
    Provider chain:
    1. Primary: Gemini (gemini-3-flash-preview)
    2. Fallback: Gemma (gemma-4-31b-it) 
    3. Fallback: OpenRouter (nvidia/nemotron-3-super-120b-a12b:free)
    
    Args:
        prompt: The prompt to send
        model: Override primary model (default: BRIEFING_MODEL)
        config: Generation config (temperature, system instruction, etc.)
        contents: Multi-modal contents instead of text prompt
        is_critical: If false, use faster fallback for non-critical ops
        require_json: If true, ensure JSON output parsing
    
    Returns:
        Response object with .text attribute
    
    Raises:
        Exception if all providers fail
    """
    if model is None:
        model = BRIEFING_MODEL
    
    max_retries_per_provider = 3 if is_critical else 2
    base_delay = 10 if is_critical else 6
    
    providers = [
        {
            "provider": "gemini",
            "model": model,
            "fn": lambda p, c, cfg: gemini_client.models.generate_content(
                model=model,
                contents=c if c else p,
                config=cfg or {}
            )
        },
        {
            "provider": "gemma",
            "model": GEMMA_FALLBACK_MODEL,
            "fn": lambda p, c, cfg: gemini_client.models.generate_content(
                model=GEMMA_FALLBACK_MODEL,
                contents=c if c else p,
                config=cfg or {}
            )
        },
    ]
    
    if PULSE_ENABLE_OPENROUTER_FALLBACK and OPENROUTER_API_KEY:
        providers.append({
            "provider": "openrouter",
            "model": OPENROUTER_MODEL,
            "fn": lambda p, c, cfg: _call_openrouter(p, cfg or {})
        })
    
    last_error = None
    
    for provider_idx, prov in enumerate(providers):
        start_time = time.time()
        provider_name = prov["provider"]
        model_name = prov["model"]
        
        for attempt in range(max_retries_per_provider):
            try:
                response = prov["fn"](prompt, contents, config)
                elapsed = time.time() - start_time
                
                # Log to model_registry
                try:
                    input_tokens = len(prompt) // 4 if prompt else 0  # Rough estimate
                    output_tokens = 0
                    if hasattr(response, 'text'):
                        output_tokens = len(response.text) // 4
                    
                    supabase.table('model_registry').insert({
                        "model_name": model_name,
                        "provider": provider_name,
                        "input_tokens": input_tokens,
                        "output_tokens": output_tokens,
                        "latency_ms": int(elapsed * 1000),
                        "success": True
                    }).execute()
                except Exception as log_err:
                    pass  # Don't fail main flow if logging fails
                
                if hasattr(response, 'text'):
                    response_text = response.text
                else:
                    response_text = str(response)
                
                if require_json:
                    try:
                        parsed = parse_json_response(response_text)
                    except ValueError as pe:
                        audit_log_sync("pulse", "WARNING", f"⚠️ LLM parse failed provider={provider_name} model={model_name}: {pe}")
                        if provider_idx == len(providers) - 1:
                            raise
                        continue
                
                print(f"✓ LLM success provider={provider_name} model={model_name} elapsed={elapsed:.1f}s")
                return response
                
            except Exception as e:
                error_str = str(e).lower()
                elapsed = time.time() - start_time
                
                is_retryable = any(err in error_str for err in RETRYABLE_ERRORS)
                is_non_retryable = any(err in error_str for err in NON_RETRYABLE_ERRORS)
                
                if is_non_retryable:
                    audit_log_sync("pulse", "ERROR", f"✗ LLM non-retryable error provider={provider_name}: {e}")
                    raise
                
                if is_retryable and attempt < max_retries_per_provider - 1:
                    delay = _jitter(base_delay * (2 ** attempt))
                    audit_log_sync("pulse", "WARNING", f"⚠️ LLM retry provider={provider_name} model={model_name} attempt={attempt+1} delay={delay:.0f}s error={error_str[:50]}")
                    await asyncio.sleep(delay)
                    continue
                
                audit_log_sync("pulse", "WARNING", f"⚠️ LLM provider failed provider={provider_name} model={model_name}: {error_str[:80]}")
                last_error = e
                break
        
        if provider_idx < len(providers) - 1:
            print(f"🔄 LLM fallback -> {providers[provider_idx + 1]['provider']}")
    
    raise last_error or Exception("All LLM providers failed")


async def _call_openrouter(prompt: str, config: dict) -> any:
    """Call OpenRouter API."""
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": PULSE_HTTP_REFERER,
        "X-Title": PULSE_APP_NAME
    }
    
    system_instruction = config.get('system_instruction') if config else None
    temperature = config.get('temperature', 0.7)
    response_mime_type = config.get('response_mime_type')
    
    messages = []
    if system_instruction:
        messages.append({"role": "system", "content": system_instruction})
    messages.append({"role": "user", "content": prompt})
    
    body = {
        "model": OPENROUTER_MODEL,
        "messages": messages,
        "temperature": temperature
    }
    
    if response_mime_type == "application/json":
        body["response_format"] = {"type": "json_object"}
    
    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.post(OPENROUTER_BASE_URL, json=body, headers=headers)
        resp.raise_for_status()
        data = resp.json()
        
        if 'choices' in data and len(data['choices']) > 0:
            return SimpleResponse(text=data['choices'][0]['message']['content'])
        
        return SimpleResponse(text=data.get('content', '') or json.dumps(data))


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
        audit_log_sync("pulse", "ERROR", f"Embedding error: {e}")
        return [0] * EMBEDDING_DIMENSION


def cosine_similarity(a: list, b: list) -> float:
    """Cosine similarity between two vectors."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = sum(x * x for x in a) ** 0.5
    norm_b = sum(x * x for x in b) ** 0.5
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


async def get_recent_memories_for_briefing(tasks: list, max_memories: int = 5) -> str:
    """
    Retrieve recent memories semantically related to today's tasks.
    Uses task titles to query match_memories RPC for relevant past insights.
    """
    if not tasks:
        return ""
    
    # Collect unique project contexts
    project_ids = list(set([
        t.get('project_id') for t in tasks 
        if t.get('project_id') and t.get('status') not in ['done', 'cancelled']
    ]))
    
    if not project_ids:
        return ""
    
    # Build query from task titles
    query_text = " ".join([
        t.get('title', '') for t in tasks[:5]  # Top 5 tasks
        if t.get('title')
    ])
    
    if not query_text.strip():
        return ""
    
    try:
        # Generate embedding for the query
        query_embedding = await asyncio.to_thread(get_embedding, query_text)
        
        if not query_embedding or all(v == 0 for v in query_embedding):
            return ""
        
        # Semantic search for relevant memories (last 30 days)
        from datetime import timedelta
        thirty_days_ago = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        
        memories_res = supabase.rpc('match_memories', {
            'query_embedding': query_embedding,
            'match_threshold': 0.7,
            'match_count': max_memories,
        }).execute()
        if memories_res.data:
            cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
            memories_res.data = [m for m in memories_res.data
                                 if m.get('created_at')
                                 and m['created_at'] >= cutoff]
        
        if not memories_res.data:
            return ""
        
        # Format memories for briefing context
        memory_entries = []
        for m in memories_res.data:
            memory_type = m.get('memory_type', 'note')
            content = m.get('content', '')[:200]  # Truncate to 200 chars
            memory_entries.append(f"[{memory_type.upper()}] {content}")
        
        result = "\n".join(memory_entries)
        print(f"🧠 Retrieved {len(memories_res.data)} relevant memories for briefing")
        return result
        
    except Exception as e:
        audit_log_sync("pulse", "WARNING", f"Recent memories retrieval failed: {e}")
        return ""


async def hybrid_search_graph(query: str) -> str:
    """Graph-first search: Find primary entity and its connections."""
    try:
        nodes_res = supabase.table('graph_nodes').select('id, label').ilike('label', f'%{query}%').limit(1).execute()
        
        # TODO: If match_graph_nodes RPC does not exist yet in Supabase,
        # create it mirroring the match_memories pattern for graph_nodes table.
        if not nodes_res.data:
            try:
                query_embedding = await asyncio.to_thread(get_embedding, query)
                vector_res = supabase.rpc('match_graph_nodes', {
                    'query_embedding': query_embedding,
                    'match_count': 1,
                    'match_threshold': 0.65
                }).execute()
                if vector_res.data:
                    nodes_res = vector_res
            except Exception as vector_err:
                print(f"Vector fallback search failed (RPC may not exist): {vector_err}")
        
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
        audit_log_sync("pulse", "WARNING", f"⚠️ Graph task context fetch failed (non-critical): {e}")
        return ""
    

# ── AGENT 1: DEPENDENCY AGENT ──────────────────────────────────────────────

async def check_task_dependencies(active_tasks: list) -> str:
    """
    DEPENDENCY AGENT: Uses graph_edges to detect when a task (B) has an uncompleted
    dependency on another task (A). Flags blockers before Danny starts work.
    """
    try:
        if not active_tasks:
            return ""
        
        lines = []
        blocked_tasks = []
        
        # Build task_id → task map
        task_map = {t['id']: t for t in active_tasks}
        
        for task in active_tasks:
            task_id = task.get('id')
            task_title = task.get('title', '')
            
            # Get the graph node for this task
            task_node_res = supabase.table('graph_nodes') \
                .select('id') \
                .eq('type', 'task') \
                .filter('metadata->>task_id', 'eq', str(task_id)) \
                .maybe_single() \
                .execute()
            
            if not task_node_res or not task_node_res.data:
                continue
            
            task_node_id = task_node_res.data['id']
            
            # Find edges where this task DEPENDS_ON another task
            dep_edges = supabase.table('graph_edges') \
                .select('source_node_id, target_node_id, relationship, metadata') \
                .eq('source_node_id', task_node_id) \
                .execute()
            
            for edge in (dep_edges.data or []):
                relationship = edge.get('relationship', '').upper()
                # Look for dependency relationships
                if relationship in ['DEPENDS_ON', 'BLOCKED_BY', 'REQUIRES']:
                    target_id = edge.get('target_node_id')
                    
                    # Find the target node's task_id from metadata
                    target_node_res = supabase.table('graph_nodes') \
                        .select('id, label, metadata') \
                        .eq('id', target_id) \
                        .maybe_single() \
                        .execute()
                    
                    if target_node_res and target_node_res.data:
                        meta = target_node_res.data.get('metadata', {})
                        if isinstance(meta, str):
                            try:
                                meta = json.loads(meta)
                            except:
                                meta = {}
                        dep_task_id = meta.get('task_id')
                        
                        if dep_task_id and int(dep_task_id) in task_map:
                            dep_task = task_map[int(dep_task_id)]
                            dep_status = dep_task.get('status', '')
                            
                            if dep_status not in ['done', 'cancelled']:
                                blocked_tasks.append({
                                    'task': task_title,
                                    'depends_on': dep_task.get('title', ''),
                                    'dep_status': dep_status
                                })
        
        if blocked_tasks:
            lines.append("⚠️ DEPENDENCY ALERTS (from graph_edges):")
            for b in blocked_tasks[:5]:  # Cap at 5
                lines.append(f"  - {b['task']} BLOCKED by '{b['depends_on']}' (status: {b['dep_status']})")
            return "\n".join(lines)
        
        return ""
    
    except Exception as e:
        audit_log_sync("pulse", "WARNING", f"⚠️ Dependency Agent failed (non-critical): {e}")
        return ""


# ── AGENT 2: SOCIAL GRAPH OPTIMIZER ───────────────────────────────────────

async def analyze_communication_patterns(people: list) -> str:
    """
    SOCIAL GRAPH OPTIMIZER: Analyzes people + graph_edges to suggest communication
    batching and identify over/under-communicated relationships.
    """
    try:
        if not people:
            return ""
        
        lines = []
        comm_suggestions = []
        
        for person in people:
            person_name = person.get('name', '')
            person_id = person.get('id')
            strategic_weight = person.get('strategic_weight', 5)
            
            if not person_name or not person_id:
                continue
            
            # Get person node
            person_node_res = supabase.table('graph_nodes') \
                .select('id') \
                .eq('type', 'person') \
                .filter('metadata->>people_id', 'eq', str(person_id)) \
                .maybe_single() \
                .execute()
            
            if not person_node_res or not person_node_res.data:
                continue
            
            person_node_id = person_node_res.data['id']
            
            # Count INVOLVES edges (task involvements)
            involves_edges = supabase.table('graph_edges') \
                .select('source_node_id, target_node_id') \
                .eq('relationship', 'INVOLVES') \
                .or_(f'source_node_id.eq.{person_node_id},target_node_id.eq.{person_node_id}') \
                .execute()
            
            task_count = len(involves_edges.data or [])
            
            # Get recent email count for this person
            email_count = 0
            try:
                email_res = supabase.table('emails') \
                    .select('id', count='exact') \
                    .or_(f'sender.ilike.%{person_name}%,linked_person_id.eq.{person_id}') \
                    .execute()
                email_count = email_res.count or 0
            except:
                pass
            
            # High-strategic person with low communication = suggestion
            if strategic_weight >= 7 and email_count < 3 and task_count < 3:
                comm_suggestions.append(f"  - {person_name}: Low communication (emails: {email_count}, tasks: {task_count}). Consider a sync.")
            elif strategic_weight >= 5 and email_count == 0 and task_count > 0:
                comm_suggestions.append(f"  - {person_name}: Has {task_count} tasks but no recent emails. May need update.")
        
        if comm_suggestions:
            lines.append("👥 SOCIAL GRAPH INSIGHTS:")
            lines.extend(comm_suggestions[:5])  # Cap at 5
            return "\n".join(lines)
        
        return ""
    
    except Exception as e:
        audit_log_sync("pulse", "WARNING", f"⚠️ Social Graph Optimizer failed (non-critical): {e}")
        return ""


# ── AGENT 3: TEMPORAL PATTERN DETECTOR ────────────────────────────────

async def detect_temporal_patterns() -> str:
    """
    TEMPORAL PATTERN DETECTOR: Surfaces 'On this day' insights from memories
    and detects seasonal patterns in productivity/mood.
    """
    try:
        from datetime import date
        
        today = date.today()
        today_str = today.strftime("%B %d")
        
        # Search memories from same month/day in previous years
        memories_res = supabase.table('memories') \
            .select('content, memory_type, created_at') \
            .or_(f"created_at::text.ilike.%{today.month:02}-{today.day:02}%") \
            .order('created_at', desc=True) \
            .limit(10) \
            .execute()
        
        if not memories_res.data:
            return ""
        
        lines = [f"📅 TEMPORAL PATTERNS (On this day {today_str}):"]
        seen = set()
        
        for m in memories_res.data:
            content = m.get('content', '')[:100]
            mem_type = m.get('memory_type', '')
            created = m.get('created_at', '')[:4]  # Just the year
            
            if content in seen:
                continue
            seen.add(content)
            
            lines.append(f"  - {created}: [{mem_type.upper()}] {content}...")
        
        if len(lines) > 1:
            return "\n".join(lines[:6])  # Cap at 5 memories + header
        
        return ""
    
    except Exception as e:
        audit_log_sync("pulse", "WARNING", f"⚠️ Temporal Pattern Detector failed (non-critical): {e}")
        return ""


# ── AGENT 4: SERENDIPITY ENGINE ────────────────────────────────

async def serendipity_engine(active_tasks: list, people: list, resources: list) -> str:
    """
    SERENDIPITY ENGINE: Surfaces unexpected connections and cross-domain insights.
    Finds non-obvious links between tasks, people, resources, and past memories
    that could spark new ideas or reveal hidden opportunities.
    """
    try:
        insights = []
        
        # 1. Cross-domain task connections
        # Find tasks from different org_tags that share keywords
        if len(active_tasks) >= 2:
            from collections import defaultdict
            keyword_tasks = defaultdict(list)
            
            for t in active_tasks[:20]:  # Limit to avoid token bloat
                title_words = set(t.get('title', '').lower().split())
                for word in title_words:
                    if len(word) > 4:  # Only meaningful keywords
                        keyword_tasks[word].append(t.get('title', ''))
            
            # Find keywords that appear in tasks from different domains
            for keyword, task_titles in keyword_tasks.items():
                if len(task_titles) >= 2:
                    insights.append(f"🔗 Keyword '{keyword}' connects: {' | '.join(task_titles[:3])}")
        
        # 2. People + Resources serendipity
        # Find resources that mention people but aren't directly linked
        if people and resources:
            for person in people[:5]:
                person_name = person.get('name', '')
                if not person_name:
                    continue
                related_resources = [
                    (r.get('title', '') or '') for r in resources[:30]
                    if person_name.lower() in ((r.get('title', '') or '') + (r.get('strategic_note', '') or '')).lower()
                ]
                if len(related_resources) >= 2:
                    insights.append(f"👤 {person_name} appears in: {' | '.join(related_resources[:3])}")
        
        # 3. Temporal serendipity - resources created on same day as memories
        try:
            from datetime import date
            today = date.today()
            thirty_days_ago = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
            
            recent_resources = [r for r in resources if r.get('created_at', '') > thirty_days_ago]
            if recent_resources and len(recent_resources) >= 2:
                insights.append(f"📚 Recent additions ({len(recent_resources)} resources in 30d) may have hidden connections to current tasks")
        except:
            pass
        
        if insights:
            lines = ["✨ SERENDIPITY FINDS:"]
            lines.extend(insights[:5])  # Cap at 5 insights
            return "\n".join(lines)
        
        return ""
    
    except Exception as e:
        audit_log_sync("pulse", "WARNING", f"⚠️ Serendipity Engine failed (non-critical): {e}")
        return ""


# ── AGENT 5: ADAPTIVE BRIEFING LEARNER ────────────────────────────────

async def adaptive_briefing_learner(briefing_history: list = None) -> str:
    """
    ADAPTIVE BRIEFING LEARNER: Learns from past briefings to improve future ones.
    Tracks which insights were useful, adjusts briefing style, and personalizes
    the briefing based on Danny's interaction patterns.
    """
    try:
        # For now, implement basic pattern tracking
        # In future, this could read from a 'briefing_feedback' table
        
        insights = []
        
        # 1. Check briefing mode effectiveness
        # Track which briefing modes (morning/afternoon/night) produce more actionable insights
        try:
            # Look at recent memories to see which time of day produced more reflections
            recent_memories = supabase.table('memories') \
                .select('content, memory_type, created_at') \
                .gte('created_at', (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()) \
                .execute()
            
            if recent_memories.data:
                morning_count = sum(1 for m in recent_memories.data 
                                   if m.get('created_at', '').startswith('0') or 
                                   m.get('created_at', '').startswith('1') or
                                   m.get('created_at', '').startswith('2'))
                evening_count = sum(1 for m in recent_memories.data
                                  if m.get('created_at', '').startswith('1') or
                                  m.get('created_at', '').startswith('2'))
                
                if morning_count > evening_count * 2:
                    insights.append("🌅 Morning briefings seem more reflective — consider adding deeper synthesis")
                elif evening_count > morning_count * 2:
                    insights.append("🌙 Evening briefings generate more insights — consider longer night briefings")
        except:
            pass
        
        # 2. Section density learning
        # Track if certain sections are consistently empty and suggest hiding them
        try:
            recent_tasks = supabase.table('tasks') \
                .select('org_tag, priority, status') \
                .eq('status', 'active') \
                .execute()
            
            if recent_tasks.data:
                tag_counts = {}
                for t in recent_tasks.data:
                    tag = t.get('org_tag', 'INBOX')
                    tag_counts[tag] = tag_counts.get(tag, 0) + 1
                
                # Suggest hiding sections with < 2 tasks
                sparse_tags = [tag for tag, count in tag_counts.items() if count < 2]
                if sparse_tags:
                    insights.append(f"📊 Sparse sections detected: {', '.join(sparse_tags)} — consider condensing")
        except:
            pass
        
        # 3. Prompt token optimization suggestion
        insights.append("🎯 Tip: Keep briefings under 3 bullets per section for maximum clarity")
        
        if insights:
            lines = ["🧠 ADAPTIVE LEARNING:"]
            lines.extend(insights[:4])  # Cap at 4 insights
            return "\n".join(lines)
        
        return ""
    
    except Exception as e:
        audit_log_sync("pulse", "WARNING", f"⚠️ Adaptive Briefing Learner failed (non-critical): {e}")
        return ""


async def retrieve_hindsight_memories(task_inputs: list, active_tasks: list, top_k: int = 5, entity_terms: list = None) -> tuple:
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
        
        # Entity-seeded queries from graph traversal (if provided)
        if entity_terms:
            for term in entity_terms[:5]:  # cap at 5 to avoid token bloat
                search_queries.append((f"entity:{term}", term))
        
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
                audit_log_sync("pulse", "ERROR", f"Hindsight query error ({query_name}): {e}")
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
        audit_log_sync("pulse", "ERROR", f"High-Res Hindsight error: {e}")
    return ([], None)


async def generate_after_action_report() -> str:
    """Generate an After-Action Report on the day's activities and save to memories."""
    try:
        now = datetime.now(timezone(timedelta(hours=5, minutes=30)))
        today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
        
        completed_tasks_res = supabase.table('tasks').select('title').eq('status', 'done').gte('completed_at', today_start).execute()
        completed_count = len(completed_tasks_res.data) if completed_tasks_res.data else 0
        
        open_tasks_res = supabase.table('tasks').select('id').eq('status', 'todo').eq('is_current', True).execute()
        open_count = len(open_tasks_res.data) if open_tasks_res.data else 0
        
        prompt = f"""You are Danny's Rhodey. Provide a dry After-Action Report (AAR). 1-2 sentences max. Focus on loops closed vs. open.
        - Loops closed today: {completed_count}
        - Loops still open: {open_count}"""
        
        response = await call_llm_with_fallback(
            prompt=prompt,
            is_critical=False,
            require_json=False
        )
        
        lesson = response.text.strip()
        
        if lesson and len(lesson) > 10:
            embedding = await asyncio.to_thread(get_embedding, lesson)
            status = 'success' if embedding and any(embedding) else 'failed'
            if status == 'failed':
                audit_log_sync("pulse", "WARNING", f"Warning: zero-vector embedding for daily reflection — storing with failed status")
            supabase.table('memories').insert({
                "content": lesson,
                "memory_type": "reflection",
                "embedding": embedding,
                "embedding_status": status,
                "source": "pulse_reflection"
            }).execute()
            print(f"📝 Daily Reflection saved: {lesson[:50]}...")
            return lesson
    except Exception as e:
        audit_log_sync("pulse", "ERROR", f"Daily reflection error: {e}")
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
        audit_log_sync("pulse", "ERROR", f"Scraper error for {url}: {e}")
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
        response = await call_llm_with_fallback(
            prompt=prompt,
            model="gemini-3.1-flash-lite-preview",
            config={'response_mime_type': 'application/json'},
            is_critical=False,
            require_json=True
        )
        parsed = parse_json_response(response.text)
        
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
            embedding = await asyncio.to_thread(get_embedding, embedding_text)
            if all(v == 0 for v in embedding):
                audit_log_sync("pulse", "WARNING", f"Warning: zero-vector embedding for daily reflection — storing anyway")
            
            # Versioned update for resources
            versioned_update('resources', item['id'], {
                "title": title,
                "summary": item.get('description'),
                "strategic_note": strategic_note,
                "category": item.get('category', 'MARKET_TREND'),
                "enriched_at": enriched_at,
                "embedding": embedding
            })
        
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
                        # Use versioned_update for mission linking (creates history)
                        versioned_update(
                            table_name='resources',
                            record_id=resource['id'],
                            update_data={"mission_id": mission['id']},
                            user_id=None,
                            change_source='pulse_mission_resolver',
                            change_reason=f"Linked to mission: {mission['title']}"
                        )
                        audit_log_sync("pulse", "INFO", 
                            f"🔗 Linked resource '{resource.get('title')}' → mission '{mission['title']}'")
                        break
        except Exception as e:
            audit_log_sync("pulse", "WARNING", f"⚠️ Mission resolver error: {e}")

        return parsed
    except Exception as e:
        audit_log_sync("pulse", "ERROR", f"Batch enrichment error: {e}")
        return []


# --- 🛰️ LAYER 1: GOOGLE INTEGRATION HELPERS ---

def sync_completed_tasks_from_google(supabase_client, tasks_service):
    """Pulls completed status from Google Tasks and updates Supabase. Returns list of (title, proj_name) for completed tasks."""
    completed = []
    try:
        result = supabase_client.table('tasks')\
            .select('id, title, google_task_id, status')\
            .eq('status', 'todo')\
            .eq('is_current', True)\
            .not_.is_('google_task_id', None)\
            .execute()
        
        tasks_to_sync = result.data or []
        if not tasks_to_sync:
            print("📋 No Google Tasks to sync.")
            return completed
        
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
                    # Versioned insert for task completion
                    try:
                        current = supabase.table('tasks').select('*').eq('id', task_id).execute()
                        if current.data:
                            old_task = current.data[0]
                            new_payload = {
                                **{k: v for k, v in old_task.items() if k not in ['id', 'created_at', 'version', 'is_current', 'supersedes_id']},
                                'status': 'done',
                                'completed_at': datetime.now(timezone.utc).isoformat()
                            }
                            create_versioned_task(
                                title=new_payload.get('title'),
                                project_id=new_payload.get('project_id'),
                                old_task_id=task_id,
                                **new_payload
                            )
                    except Exception as ve:
                        # Fallback to versioned update
                        versioned_update('tasks', task_id, {
                            'status': 'done',
                            'completed_at': datetime.now(timezone.utc).isoformat()
                        })
                    
                    # 🧠 Collect for outcome memory — caller will fire as background tasks
                    proj_name = None
                    proj_id = task.get('project_id')
                    if proj_id:
                        proj_lookup = supabase_client.table('projects').select('name').eq('id', proj_id).maybe_single().execute()
                        proj_name = proj_lookup.data['name'] if proj_lookup.data else None
                    completed.append((title, proj_name))
                    
                    print(f"✅ Synced from Google: '{title}' (ID: {task_id})")
                    synced_count += 1
                    
            except Exception as e:
                if 'notFound' in str(e):
                    audit_log_sync("pulse", "WARNING", f"⚠️ Google Task {google_task_id} not found, skipping.")
                else:
                    audit_log_sync("pulse", "WARNING", f"⚠️ Error checking Google Task {google_task_id}: {e}")
        
        print(f"📊 Google→Supabase Sync complete: {synced_count}/{len(tasks_to_sync)} tasks marked done.")
        
    except Exception as e:
        audit_log_sync("pulse", "ERROR", f"❌ sync_completed_tasks_from_google failed: {e}")
    
    return completed

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
        audit_log_sync("pulse", "WARNING", f"⚠️ Conflict check failed: {e}")
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
            audit_log_sync("pulse", "WARNING", f"⚠️ Event ID {event_id} invalid. Attempting fresh creation...")
            return sync_to_calendar(title, start_iso, event_id=None)
        audit_log_sync("pulse", "ERROR", f"❌ CRITICAL: Calendar sync failed: {e}")
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
        audit_log_sync("pulse", "WARNING", f"⚠️ Note: Calendar delete failed (likely already gone).")

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
        audit_log_sync("pulse", "WARNING", f"⚠️ Google Tasks API error: {e}")
        return None

async def fetch_hybrid_graph_context(people: list, graph_node_projects: list, task_inputs: list) -> str:
    """Hybrid graph search using entity terms from people+projects, filtering by task_inputs."""
    try:
        entity_terms = [p['name'] for p in people if p.get('name')] + [p.get('name') for p in graph_node_projects if p.get('name')]
        
        if not entity_terms or not task_inputs:
            return ""
        
        dump_text = " ".join(task_inputs).lower()
        
        matched_terms = [term for term in entity_terms if term.lower() in dump_text]
        
        query_terms = matched_terms if matched_terms else entity_terms[:8]
        
        results = await asyncio.gather(*[hybrid_search_graph(term) for term in query_terms])
        
        all_lines = []
        for result in results:
            if result:
                all_lines.extend(result.split("\n"))
        
        if not all_lines:
            return ""
        
        deduplicated = list(dict.fromkeys(all_lines))
        return "GRAPH CONTEXT (routing awareness only — do NOT list in briefing):\n" + "\n".join(deduplicated)
    
    except Exception as e:
        audit_log_sync("pulse", "WARNING", f"⚠️ Hybrid graph context fetch failed (non-critical): {e}")
        return ""


async def fetch_graph_task_context(people: list, active_tasks: list) -> str:
    """
    Fetches graph edges connecting people to active tasks.
    Returns formatted context showing who is involved in which tasks.
    """
    try:
        if not people or not active_tasks:
            return ""
        
        lines = []
        task_map = {t['id']: t for t in active_tasks}
        
        # Get all person nodes
        people_ids = {p['id']: p['name'] for p in people}
        person_nodes = supabase.table('graph_nodes') \
            .select('id, label, metadata') \
            .eq('type', 'person') \
            .execute()
        
        # Build node_id → person_name map
        node_to_person = {}
        for node in (person_nodes.data or []):
            meta = node.get('metadata', {})
            if isinstance(meta, str):
                try:
                    meta = json.loads(meta)
                except:
                    continue
            people_id = meta.get('people_id')
            if people_id and int(people_id) in people_ids:
                node_to_person[node['id']] = people_ids[int(people_id)]
        
        # Find INVOLVES edges linking person nodes to task nodes
        task_nodes = supabase.table('graph_nodes') \
            .select('id, metadata') \
            .eq('type', 'task') \
            .execute()
        
        task_node_ids = []
        task_node_map = {}  # node_id → task_id
        for node in (task_nodes.data or []):
            meta = node.get('metadata', {})
            if isinstance(meta, str):
                try:
                    meta = json.loads(meta)
                except:
                    continue
            task_id = meta.get('task_id')
            if task_id and int(task_id) in task_map:
                task_node_ids.append(node['id'])
                task_node_map[node['id']] = int(task_id)
        
        if not task_node_ids or not node_to_person:
            return ""
        
        # Get INVOLVES edges
        edges_res = supabase.table('graph_edges') \
            .select('source_node_id, target_node_id, relationship') \
            .in_('relationship', ['INVOLVES', 'MANAGES', 'ASSIGNED_TO']) \
            .execute()
        
        context_lines = []
        seen = set()
        
        for edge in (edges_res.data or []):
            source = edge.get('source_node_id')
            target = edge.get('target_node_id')
            rel = edge.get('relationship')
            
            # Check if this connects a person to a task
            person_name = None
            task_id = None
            
            if source in node_to_person and target in task_node_map:
                person_name = node_to_person[source]
                task_id = task_node_map[target]
            elif target in node_to_person and source in task_node_map:
                person_name = node_to_person[target]
                task_id = task_node_map[source]
            
            if person_name and task_id and task_id in task_map:
                task_title = task_map[task_id]['title']
                key = f"{person_name}:{task_id}"
                if key not in seen:
                    seen.add(key)
                    context_lines.append(f"[{person_name}] --{rel}--> [{task_title}]")
        
        if context_lines:
            return "GRAPH TASK CONTEXT:\n" + "\n".join(context_lines[:10])  # Cap at 10
        return ""
    
    except Exception as e:
        audit_log_sync("pulse", "WARNING", f"⚠️ Graph task context fetch failed (non-critical): {e}")
        return ""

# --- 🏥 HEARTBEAT / HEALTH CHECK ---
async def update_heartbeat():
    """Update the last successful Pulse run timestamp."""
    try:
        supabase.table('core_config').upsert({
            "key": "pulse_last_success",
            "content": datetime.now(timezone.utc).isoformat()
        }, on_conflict="key").execute()
        print("💓 Heartbeat updated.")
    except Exception as e:
        error("pulse", f"Heartbeat update failed: {e}", format_error(e))

async def check_pipeline_health() -> str:
    """
    Returns a health report of the memory pipeline.
    Checks: pending/processing dumps, null embeddings, failed items.
    """
    lines = []
    try:
        #         Check for stuck dumps (pending/staged > 2 hours)
        two_hours_ago = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        
        # Check pending dumps
        stuck_res = supabase.table('raw_dumps') \
            .select('id', count='exact') \
            .in_('status', ['pending', 'staged']) \
            .lt('created_at', two_hours_ago) \
            .execute()
        stuck_count = stuck_res.count or 0
        if stuck_count > 0:
            lines.append(f"⚠️ {stuck_count} raw_dumps stuck in 'pending'/'staged' > 2h")
        
        # Check processing dumps (stuck > 10 minutes)
        ten_mins_ago = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
        processing_res = supabase.table('raw_dumps') \
            .select('id', count='exact') \
            .eq('status', 'processing') \
            .lt('created_at', ten_mins_ago) \
            .execute()
        processing_count = processing_res.count or 0
        if processing_count > 0:
            lines.append(f"⚠️ {processing_count} raw_dumps stuck in 'processing' > 10min")
            # Send Telegram alert
            try:
                telegram_chat_id = os.getenv("TELEGRAM_CHAT_ID")
                telegram_bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
                if telegram_chat_id and telegram_bot_token:
                    import httpx
                    url = f"https://api.telegram.org/bot{telegram_bot_token}/sendMessage"
                    payload = {
                        "chat_id": int(telegram_chat_id),
                        "text": f"⚠️ HEALTH ALERT: {processing_count} raw_dumps stuck in 'processing' > 10min",
                        "parse_mode": "Markdown"
                    }
                    httpx.post(url, json=payload, timeout=10)
            except Exception as alert_e:
                audit_log_sync("pulse", "WARNING", f"Failed to send Telegram alert: {alert_e}")
        
        # Check for null embeddings in recent memories
        seven_days_ago = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
        null_emb_res = supabase.table('memories') \
            .select('id', count='exact') \
            .is_('embedding', 'null') \
            .gte('created_at', seven_days_ago) \
            .execute()
        null_emb_count = null_emb_res.count or 0
        if null_emb_count > 0:
            lines.append(f"⚠️ {null_emb_count} memories with NULL embeddings (last 7 days)")
        
        # Check last Pulse success
        last_run_res = supabase.table('core_config') \
            .select('content') \
            .eq('key', 'pulse_last_success') \
            .maybe_single() \
            .execute()
        if last_run_res and last_run_res.data:
            last_run = datetime.fromisoformat(last_run_res.data['content'])
            hours_ago = (datetime.now(timezone.utc) - last_run).total_seconds() / 3600
            if hours_ago > 24:
                lines.append(f"⚠️ Pulse hasn't run in {hours_ago:.1f} hours!")
            else:
                lines.append(f"✅ Pulse last ran {hours_ago:.1f} hours ago")
        else:
            lines.append("⚠️ No Pulse heartbeat found")
        
        if not lines:
            return "✅ Pipeline health: All clear!"
        return "PIPELINE HEALTH REPORT:\n" + "\n".join(lines)
    except Exception as e:
        return f"⚠️ Health check failed: {e}"


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


async def retry_failed_operations(max_retries: int = 5):
    """Retry operations in the failed_queue with exponential backoff."""
    try:
        # Fetch items that haven't exceeded max retries
        failed_items = supabase.table('failed_queue') \
            .select('*') \
            .lt('retry_count', max_retries) \
            .order('created_at', desc=False) \
            .limit(20) \
            .execute()
        
        if not failed_items.data:
            return "✅ No failed items to retry."
        
        print(f"🔄 Retrying {len(failed_items.data)} failed operations...")
        retried = 0
        failed_again = 0
        
        for item in failed_items.data:
            queue_id = item['id']
            source_table = item['source_table']
            source_id = item['source_id']
            operation = item['operation']
            
            try:
                if operation == 'embedding' and source_table == 'memories':
                    # Retry embedding generation
                    mem_res = supabase.table('memories') \
                        .select('id, content') \
                        .eq('id', int(source_id)) \
                        .maybe_single() \
                        .execute()
                    
                    if mem_res and mem_res.data:
                        embedding = await asyncio.to_thread(get_embedding, mem_res.data['content'])
                        if embedding and any(embedding):
                            # Versioned update for memories
                            versioned_update('memories', int(source_id), {
                                "embedding": embedding,
                                "embedding_status": "success"
                            })
                            
                            # Remove from failed queue on success
                            supabase.table('failed_queue') \
                                .delete() \
                                .eq('id', queue_id) \
                                .execute()
                            retried += 1
                        else:
                            raise Exception("Embedding generation returned zero vector")
                
                elif operation == 'memory_insert':
                    # Would need the original content - skip for now
                    audit_log_sync("pulse", "WARNING", f"   ⚠️ Cannot retry memory_insert without original content: {queue_id}")
                    continue
                
                # Update retry count (metadata update, no versioning needed)
                supabase.table('failed_queue') \
                    .update({
                        "retry_count": item['retry_count'] + 1,
                        "last_retry_at": datetime.now(timezone.utc).isoformat()
                    }) \
                    .eq('id', queue_id) \
                    .execute()
            
            except Exception as e:
                # Update retry count and last_retry_at
                try:
                    supabase.table('failed_queue') \
                        .update({
                            "retry_count": item['retry_count'] + 1,
                            "last_retry_at": datetime.now(timezone.utc).isoformat(),
                            "error_message": str(e)[:500]
                        }) \
                        .eq('id', queue_id) \
                        .execute()
                except:
                    pass
                failed_again += 1
        
        return f"🔄 Retry complete: ✅ {retried} succeeded, ❌ {failed_again} still failing"
    
    except Exception as e:
        return f"⚠️ Retry process failed: {e}"


async def detect_practices():
    """
    Passive practice detection. Runs during weekend pulses.

    Two-pass approach:
    1. Embedding clustering (cosine similarity >= 0.75) to find candidate groups
    2. Gemini batch verification for identity resolution + canonical naming

    Discovers recurring behaviors from raw_dumps + memories entries.
    Creates practice nodes in graph_nodes when patterns are detected.
    Handles declared practice merge, lifecycle transitions, and exclusion list.

    Side effects:
    - Creates/updates graph_nodes of type 'practice'
    - Creates ASSOCIATED_WITH edges to entity nodes
    - Updates core_config exclusion list for dismissed practices
    """
    ist_offset = timezone(timedelta(hours=5, minutes=30))
    now = datetime.now(ist_offset)
    fourteen_days_ago = (now - timedelta(days=14)).isoformat()

    try:
        # ── Step 0: Initialize core_config key if missing ──
        supabase.table('core_config').upsert({
            "key": "dismissed_practice_variants",
            "content": "[]"
        }, on_conflict="key").execute()

        # ── Step 1: Load exclusion list ──
        exclusion_res = supabase.table('core_config') \
            .select('content') \
            .eq('key', 'dismissed_practice_variants') \
            .maybe_single() \
            .execute()
        exclusion_list = json.loads(exclusion_res.data.get('content', '[]')) if exclusion_res.data else []

        # ── Step 2: Load existing practice nodes ──
        practices_res = supabase.table('graph_nodes') \
            .select('id, label, metadata') \
            .eq('type', 'practice') \
            .execute()
        existing_practices = practices_res.data or []

        # Build metadata maps
        existing_practice_nodes = []
        for p in existing_practices:
            raw_meta = p.get('metadata')
            if isinstance(raw_meta, str):
                try:
                    meta = json.loads(raw_meta)
                except:
                    meta = {}
            elif isinstance(raw_meta, dict):
                meta = raw_meta
            else:
                meta = {}
            existing_practice_nodes.append({
                'id': p['id'],
                'label': p['label'],
                'metadata': meta
            })

        # Build set of all existing variant texts for novel candidate filtering
        all_variant_texts = set()
        for pn in existing_practice_nodes:
            for v in pn['metadata'].get('variants', []):
                all_variant_texts.add(v.lower().strip())
        for v in exclusion_list:
            all_variant_texts.add(v.lower().strip())

        # ── Step 3: Collect candidates from last 14 days ──
        raw_res = supabase.table('raw_dumps') \
            .select('id, content, created_at, metadata, message_type') \
            .gte('created_at', fourteen_days_ago) \
            .in_('message_type', ['task', 'note']) \
            .execute()

        mem_res = supabase.table('memories') \
            .select('id, content, created_at, memory_type') \
            .gte('created_at', fourteen_days_ago) \
            .in_('memory_type', ['note', 'outcome']) \
            .execute()

        candidates = []
        seen_texts = set()

        for item in (raw_res.data or []):
            text = (item.get('content') or '').strip()
            if not text or len(text) < 5 or len(text) > 500:
                continue
            text_lower = text.lower()
            if text_lower in seen_texts:
                continue
            seen_texts.add(text_lower)

            raw_meta = item.get('metadata', {})
            if isinstance(raw_meta, str):
                try:
                    raw_meta = json.loads(raw_meta)
                except:
                    raw_meta = {}
            entity = raw_meta.get('entity') if isinstance(raw_meta, dict) else None

            candidates.append({
                'text': text,
                'timestamp': item.get('created_at'),
                'entity': entity,
                'source': 'raw_dumps',
                'source_id': item.get('id')
            })

        for item in (mem_res.data or []):
            text = (item.get('content') or '').strip()
            if not text or len(text) < 5 or len(text) > 500:
                continue
            text_lower = text.lower()
            if text_lower in seen_texts:
                continue
            seen_texts.add(text_lower)

            candidates.append({
                'text': text,
                'timestamp': item.get('created_at'),
                'entity': None,
                'source': 'memories',
                'source_id': item.get('id')
            })

        if len(candidates) < 3:
            print("📍 detect_practices: Too few candidates (<3), skipping.")
            return

        # ── Step 4: Generate embeddings for all candidates ──
        print(f"📍 detect_practices: Generating embeddings for {len(candidates)} candidates...")
        for c in candidates:
            c['embedding'] = await asyncio.to_thread(get_embedding, c['text'])

        # ── Step 5: Cluster by cosine similarity ──
        clusters = []
        assigned = set()

        for i in range(len(candidates)):
            if i in assigned:
                continue
            cluster_indices = [i]
            assigned.add(i)
            for j in range(i + 1, len(candidates)):
                if j in assigned:
                    continue
                sim = cosine_similarity(candidates[i]['embedding'], candidates[j]['embedding'])
                if sim >= 0.75:
                    cluster_indices.append(j)
                    assigned.add(j)
            clusters.append(cluster_indices)

        print(f"📍 detect_practices: Found {len(clusters)} candidate clusters.")

        # ── Step 6: Process each cluster ──
        new_practice_nodes = {}
        for cluster_indices in clusters:
            if len(cluster_indices) < 3:
                continue

            cluster_candidates = [candidates[i] for i in cluster_indices]
            cluster_texts = [c['text'] for c in cluster_candidates]
            timestamps = []
            entities_set = set()
            for c in cluster_candidates:
                ts = c.get('timestamp')
                if ts:
                    timestamps.append(ts)
                if c.get('entity'):
                    entities_set.add(c['entity'])

            # Check: must span at least 2 calendar weeks
            if len(timestamps) >= 2:
                try:
                    parsed_dates = []
                    for ts in timestamps[:10]:
                        cleaned = str(ts).replace('Z', '+00:00').replace(' ', 'T')
                        dt = datetime.fromisoformat(cleaned)
                        if dt.tzinfo is None:
                            dt = dt.replace(tzinfo=timezone.utc)
                        parsed_dates.append(dt.astimezone(ist_offset))
                    week_numbers = sorted(set(d.isocalendar()[1] for d in parsed_dates))
                    if len(week_numbers) < 2:
                        continue
                except Exception:
                    continue

            # Check if cluster matches an existing practice node (declared merge guard + update)
            cluster_centroid = candidates[cluster_indices[0]]['embedding']
            matched_existing = None
            best_sim = 0.0

            for pn in existing_practice_nodes:
                pn_label = pn['label']
                pn_embedding = await asyncio.to_thread(get_embedding, pn_label)
                sim = cosine_similarity(cluster_centroid, pn_embedding)
                if sim >= 0.75 and sim > best_sim:
                    best_sim = sim
                    matched_existing = pn

            if matched_existing:
                meta = matched_existing['metadata']
                existing_variants = set(v.lower() for v in meta.get('variants', []))
                new_texts = [t for t in cluster_texts if t.lower() not in existing_variants]

                if new_texts:
                    meta['variants'] = meta.get('variants', []) + new_texts

                # Update occurrence counts
                old_count = meta.get('occurrence_count', 0)
                meta['occurrence_count'] = old_count + len(cluster_indices)

                # Update last_occurrence
                sorted_ts = sorted(timestamps, reverse=True) if timestamps else []
                if sorted_ts:
                    meta['last_occurrence'] = str(sorted_ts[0])[:10]

                # Update entities
                existing_entities = set(meta.get('entities', []))
                new_entities = entities_set - existing_entities
                if new_entities:
                    meta['entities'] = list(existing_entities | entities_set)

                # Update typical_time (rolling average)
                all_times = meta.get('_all_times', [])
                for ts in timestamps[:20]:
                    try:
                        cleaned = str(ts).replace('Z', '+00:00').replace(' ', 'T')
                        dt = datetime.fromisoformat(cleaned)
                        if dt.tzinfo is None:
                            dt = dt.replace(tzinfo=timezone.utc)
                        dt_ist = dt.astimezone(ist_offset)
                        all_times.append(dt_ist.hour * 60 + dt_ist.minute)
                    except:
                        pass
                all_times = all_times[-50:]
                meta['_all_times'] = all_times
                if all_times:
                    avg_mins = int(sum(all_times) / len(all_times))
                    h, m = divmod(avg_mins, 60)
                    meta['typical_time'] = f"{h:02d}:{m:02d}-{(h+1):02d}:{m:02d}"

                # Update typical_days
                existing_days = set(meta.get('typical_days', []))
                for ts in timestamps[:20]:
                    try:
                        cleaned = str(ts).replace('Z', '+00:00').replace(' ', 'T')
                        dt = datetime.fromisoformat(cleaned)
                        if dt.tzinfo is None:
                            dt = dt.replace(tzinfo=timezone.utc)
                        dt_ist = dt.astimezone(ist_offset)
                        existing_days.add(dt_ist.strftime('%a'))
                    except:
                        pass
                meta['typical_days'] = sorted(existing_days)

                # Update frequency_observed
                total_days = 14
                meta['frequency_observed'] = f"{meta['occurrence_count']}/{total_days}days"

                # Persist
                supabase.table('graph_nodes') \
                    .update({'metadata': meta}) \
                    .eq('id', matched_existing['id']) \
                    .execute()

                print(f"📍 detect_practices: Updated practice '{matched_existing['label']}' (+{len(cluster_indices)} occurrences)")
                continue

            # ── Step 6b: Check against exclusion list before creating ──
            skip_due_to_exclusion = False
            for t in cluster_texts:
                t_lower = t.lower()
                if any(excluded in t_lower for excluded in exclusion_list):
                    skip_due_to_exclusion = True
                    break
            if skip_due_to_exclusion:
                continue

            # ── Step 7: Gemini batch verification for novel clusters ──
            truncated_texts = [t[:100] for t in cluster_texts]

            verify_prompt = f"""You are a practice detector. Determine if the entries below all represent the same recurring activity or practice.

Entries:
{json.dumps(truncated_texts, indent=2)}

Rules:
- If ALL entries describe the same recurring activity (e.g., "morning run", "went for a jog", "ran 5k"), return is_same_activity: true and suggest a short canonical_name.
- If they describe DIFFERENT activities, return is_same_activity: false.
- Only return true if you are confident ALL entries refer to the same underlying practice.
- canonical_name must be short and natural (e.g., "Morning Run", "Client Lunch", "Weekly Review").

Return ONLY valid JSON:
{{"is_same_activity": true, "canonical_name": "Morning Run"}}"""

            try:
                response = await call_llm_with_fallback(
                    prompt=verify_prompt,
                    model="gemini-3.1-flash-lite-preview",
                    config={'response_mime_type': 'application/json'},
                    is_critical=False,
                    require_json=True
                )
                result = parse_json_response(response.text)
                if not result.get('is_same_activity') or not result.get('canonical_name'):
                    continue

                canonical_name = result['canonical_name'].strip()

                # ── Step 8: Create practice node ──
                # Double-check: embedding overlap with existing nodes at tighter threshold
                name_embedding = await asyncio.to_thread(get_embedding, canonical_name)
                too_similar = False
                for pn in existing_practice_nodes:
                    pn_embedding = await asyncio.to_thread(get_embedding, pn['label'])
                    if cosine_similarity(name_embedding, pn_embedding) >= 0.85:
                        too_similar = True
                        print(f"📍 detect_practices: Skipping '{canonical_name}' — too similar to existing '{pn['label']}'")
                        break
                if too_similar:
                    continue

                # Build metadata
                distinct_entities = list(entities_set) if entities_set else []
                primary_entity = distinct_entities[0] if distinct_entities else None

                first_detected = min(ts for ts in timestamps if ts) if timestamps else now.isoformat()
                last_occurrence = max(ts for ts in timestamps if ts) if timestamps else now.isoformat()

                metadata = {
                    "declared": False,
                    "canonical_name_set_at": now.strftime('%Y-%m-%d'),
                    "frequency_observed": f"{len(cluster_indices)}/14days",
                    "frequency_baseline": f"{len(cluster_indices)}/14days",
                    "baseline_source": "bootstrap",
                    "baseline_weeks_of_data": 2,
                    "typical_time": None,
                    "typical_days": [],
                    "confidence": 0.85,
                    "last_occurrence": str(last_occurrence)[:10],
                    "first_detected": str(first_detected)[:10],
                    "occurrence_count": len(cluster_indices),
                    "status": "active",
                    "resumed_at": None,
                    "entity": primary_entity,
                    "entities": distinct_entities,
                    "variants": list(set(cluster_texts)),
                    "health_score": 100,
                    "health_score_raw": 100
                }

                # Calculate typical_time from timestamps
                time_minutes = []
                for ts in timestamps[:30]:
                    try:
                        cleaned = str(ts).replace('Z', '+00:00').replace(' ', 'T')
                        dt = datetime.fromisoformat(cleaned)
                        if dt.tzinfo is None:
                            dt = dt.replace(tzinfo=timezone.utc)
                        dt_ist = dt.astimezone(ist_offset)
                        time_minutes.append(dt_ist.hour * 60 + dt_ist.minute)
                    except:
                        pass
                if time_minutes:
                    avg_mins = int(sum(time_minutes) / len(time_minutes))
                    h, m = divmod(avg_mins, 60)
                    metadata['typical_time'] = f"{h:02d}:{m:02d}-{(h+1):02d}:{m:02d}"

                # Calculate typical_days
                day_set = set()
                for ts in timestamps[:30]:
                    try:
                        cleaned = str(ts).replace('Z', '+00:00').replace(' ', 'T')
                        dt = datetime.fromisoformat(cleaned)
                        if dt.tzinfo is None:
                            dt = dt.replace(tzinfo=timezone.utc)
                        dt_ist = dt.astimezone(ist_offset)
                        day_set.add(dt_ist.strftime('%a'))
                    except:
                        pass
                metadata['typical_days'] = sorted(day_set)

                # Insert node
                node_res = supabase.table('graph_nodes').insert({
                    "label": canonical_name,
                    "type": "practice",
                    "metadata": metadata
                }).execute()

                if node_res.data:
                    node_id = node_res.data[0]['id']
                    new_practice_nodes[canonical_name] = node_id
                    print(f"📍 detect_practices: Created practice node '{canonical_name}' (id: {node_id})")

                    # Create ASSOCIATED_WITH edges for distinct entities
                    for entity_text in distinct_entities:
                        if not entity_text:
                            continue
                        entity_node = supabase.table('graph_nodes') \
                            .select('id') \
                            .ilike('label', f'%{entity_text}%') \
                            .limit(1) \
                            .execute()
                        if entity_node.data:
                            supabase.table('graph_edges').insert({
                                "source_node_id": node_id,
                                "target_node_id": entity_node.data[0]['id'],
                                "relationship": "ASSOCIATED_WITH",
                                "weight": 1.0,
                                "metadata": {"source": "practice_detection"}
                            }).execute()

                    # Track this node for lifecycle processing
                    existing_practice_nodes.append({
                        'id': node_id,
                        'label': canonical_name,
                        'metadata': metadata
                    })

            except Exception as e:
                audit_log_sync("pulse", "WARNING", f"Practice verification error: {e}")
                continue

        # ── Step 9: Lifecycle transitions ──
        for pn in existing_practice_nodes:
            meta = pn['metadata']
            if meta.get('status') not in ['active', 'dormant']:
                continue

            last_occ = meta.get('last_occurrence')
            if not last_occ:
                continue

            try:
                last_dt = datetime.fromisoformat(str(last_occ))
                if last_dt.tzinfo is None:
                    last_dt = last_dt.replace(tzinfo=ist_offset)
                days_since = (now - last_dt).days

                if days_since >= 84 and meta.get('status') == 'active':
                    meta['status'] = 'inactive'
                    variants = meta.get('variants', [])
                    meta['variants'] = variants[:5]
                    meta['_compact_notice'] = f"Compacted from {len(variants)} variants at {now.strftime('%Y-%m-%d')}"
                    supabase.table('graph_nodes') \
                        .update({'metadata': meta}) \
                        .eq('id', pn['id']) \
                        .execute()
                    print(f"📍 detect_practices: Marked '{pn['label']}' as inactive ({days_since}d). Variants compacted.")

                elif days_since >= 28 and meta.get('status') == 'active':
                    meta['status'] = 'dormant'
                    variants = meta.get('variants', [])
                    if len(variants) > 10:
                        meta['variants'] = variants[:10]
                        meta['_compact_notice'] = f"Compacted from {len(variants)} variants at {now.strftime('%Y-%m-%d')}"
                    supabase.table('graph_nodes') \
                        .update({'metadata': meta}) \
                        .eq('id', pn['id']) \
                        .execute()
                    print(f"📍 detect_practices: Marked '{pn['label']}' as dormant ({days_since}d).{' Variants compacted.' if len(variants) > 10 else ''}")

            except Exception as e:
                audit_log_sync("pulse", "WARNING", f"Lifecycle transition failed for '{pn['label']}': {e}")
                continue

        print("📍 detect_practices: Complete.")
        return new_practice_nodes

    except Exception as e:
        audit_log_sync("pulse", "ERROR", f"detect_practices failed: {e}")
        import traceback
        traceback.print_exc()
        return {}


async def build_practice_edges():
    """
    Detect PRECEDES/FOLLOWED_BY relationships between active practices.

    For each pair of active practices, checks temporal ordering based on
    their typical_time ranges and typical_days overlap.
    Creates graph_edges with relationship 'PRECEDES' and 'FOLLOWED_BY'.

    4-hour window: A precedes B if A's typical time is 0-4 hours before B's
    on at least 3 co-occurring days.
    """
    try:
        practices_res = supabase.table('graph_nodes') \
            .select('id, label, metadata') \
            .eq('type', 'practice') \
            .execute()
        all_practices = practices_res.data or []
        if len(all_practices) < 2:
            return

        practices = []
        for p in all_practices:
            raw_meta = p.get('metadata')
            if isinstance(raw_meta, str):
                try:
                    meta = json.loads(raw_meta)
                except:
                    continue
            elif isinstance(raw_meta, dict):
                meta = raw_meta
            else:
                continue

            if meta.get('status', 'active') != 'active':
                continue

            all_times = meta.get('_all_times', [])
            if not all_times or len(all_times) < 3:
                continue

            typical_days = meta.get('typical_days', [])
            if not typical_days:
                continue

            practices.append({
                'id': p['id'],
                'label': p['label'],
                'avg_time': sum(all_times) / len(all_times),
                'all_times': all_times,
                'typical_days': set(typical_days),
                'occurrence_count': meta.get('occurrence_count', 0)
            })

        edges_created = 0
        for i in range(len(practices)):
            for j in range(len(practices)):
                if i == j:
                    continue

                a, b = practices[i], practices[j]

                # A must precede B: A's avg_time before B's, gap within 4h
                gap = b['avg_time'] - a['avg_time']
                if not (0 < gap <= 240):
                    continue

                # Must share at least 2 typical days
                shared = a['typical_days'] & b['typical_days']
                if len(shared) < 2:
                    continue

                # Count co-occurrences: for each of A's times, is there a B time within 4h?
                co_count = 0
                for ta in a['all_times']:
                    for tb in b['all_times']:
                        if 0 < tb - ta <= 240:
                            co_count += 1
                            break

                if co_count < 3:
                    continue

                # Check existing edge
                existing = supabase.table('graph_edges') \
                    .select('id') \
                    .eq('source_node_id', a['id']) \
                    .eq('target_node_id', b['id']) \
                    .eq('relationship', 'PRECEDES') \
                    .limit(1) \
                    .execute()
                if existing.data:
                    continue

                # Confidence: co-occurrence count, gap tightness, day overlap
                gap_ratio = 1 - (gap / 240)
                day_ratio = len(shared) / max(len(a['typical_days'] | b['typical_days']), 1)
                confidence = min(1.0, (co_count / 10) * 0.5 + gap_ratio * 0.3 + day_ratio * 0.2)
                confidence = round(confidence, 2)

                meta_json = json.dumps({
                    "source": "practice_detection",
                    "avg_gap_minutes": int(gap),
                    "co_occurrences": co_count,
                    "shared_days": sorted(shared)
                })

                supabase.table('graph_edges').insert({
                    "source_node_id": a['id'],
                    "target_node_id": b['id'],
                    "relationship": "PRECEDES",
                    "weight": confidence,
                    "metadata": meta_json
                }).execute()

                supabase.table('graph_edges').insert({
                    "source_node_id": b['id'],
                    "target_node_id": a['id'],
                    "relationship": "FOLLOWED_BY",
                    "weight": confidence,
                    "metadata": meta_json
                }).execute()

                edges_created += 2
                print(f"📍 practice_edges: {a['label']} → {b['label']} "
                      f"(gap: {gap:.0f}min, co-occur: {co_count}, confidence: {confidence})")

        if edges_created:
            print(f"📍 practice_edges: Created {edges_created} edges.")

    except Exception as e:
        audit_log_sync("pulse", "WARNING", f"build_practice_edges failed: {e}")
        import traceback
        traceback.print_exc()


async def build_practice_correlations() -> list:
    """
    Surface correlations between practice adherence and task completion.

    Thresholds:
    - Each practice must have >=20 occurrences (metadata.occurrence_count)
    - System-wide must have >=50 completed tasks (status in done/cancelled)

    Returns list of insight strings comparing task completion on practice
    typical_days vs other days over the last 30 days.
    """
    try:
        completed_res = supabase.table('tasks') \
            .select('id', count='exact') \
            .in_('status', ['done', 'cancelled']) \
            .execute()
        total_completed = completed_res.count or 0
        if total_completed < 50:
            return []

        practices_res = supabase.table('graph_nodes') \
            .select('id, label, metadata') \
            .eq('type', 'practice') \
            .execute()
        all_practices = practices_res.data or []

        thirty_days_ago = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        tasks_res = supabase.table('tasks') \
            .select('completed_at') \
            .in_('status', ['done', 'cancelled']) \
            .gte('completed_at', thirty_days_ago) \
            .execute()

        from collections import defaultdict
        day_tasks = defaultdict(int)
        for t in (tasks_res.data or []):
            ts = t.get('completed_at')
            if ts:
                try:
                    dt = datetime.fromisoformat(str(ts).replace('Z', '+00:00'))
                    day_tasks[dt.strftime('%a')] += 1
                except Exception:
                    pass

        total_recent = sum(day_tasks.values())
        if total_recent < 10:
            return []

        day_counts = defaultdict(int)
        for i in range(30):
            d = (datetime.now(timezone.utc) - timedelta(days=i)).strftime('%a')
            day_counts[d] += 1

        all_days = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']
        insights = []

        for p in all_practices:
            raw_meta = p.get('metadata')
            if isinstance(raw_meta, str):
                try:
                    meta = json.loads(raw_meta)
                except Exception:
                    continue
            elif isinstance(raw_meta, dict):
                meta = raw_meta
            else:
                continue

            if meta.get('occurrence_count', 0) < 20:
                continue

            typical_days = meta.get('typical_days', [])
            if not typical_days:
                continue

            practice_day_tasks = sum(day_tasks.get(d, 0) for d in typical_days)
            practice_day_count = sum(day_counts.get(d, 0) for d in typical_days)

            non_days = [d for d in all_days if d not in typical_days]
            non_tasks = sum(day_tasks.get(d, 0) for d in non_days)
            non_count = sum(day_counts.get(d, 0) for d in non_days)

            p_rate = practice_day_tasks / max(practice_day_count, 1)
            np_rate = non_tasks / max(non_count, 1)

            if practice_day_tasks >= 3 and non_tasks >= 3:
                if p_rate > np_rate * 1.25:
                    pct = int(((p_rate / np_rate) - 1) * 100)
                    insights.append(f"\U0001F4CA *{p['label']}*: {p_rate:.1f} tasks/day on practice days vs {np_rate:.1f} overall (+{pct}%)")
                elif np_rate > p_rate * 1.25:
                    pct = int((1 - p_rate / np_rate) * 100)
                    insights.append(f"\U0001F4CA *{p['label']}*: {p_rate:.1f} tasks/day on practice days vs {np_rate:.1f} overall ({pct}% fewer)")

        return insights

    except Exception as e:
        audit_log_sync("pulse", "WARNING", f"build_practice_correlations failed: {e}")
        return []


async def sync_practice_canonical_pages():
    """
    Create or update canonical_pages entries for active/dormant practices.

    For each practice node (excluding dismissed), generates a structured
    markdown page with metrics, variants, typical schedule, and entity
    associations. Uses versioned insert pattern (insert new, mark old
    is_current=False) matching brain_synth.py conventions.
    """
    try:
        practices_res = supabase.table('graph_nodes') \
            .select('id, label, metadata') \
            .eq('type', 'practice') \
            .execute()
        all_practices = practices_res.data or []
        if not all_practices:
            return

        now_iso = datetime.now(timezone.utc).isoformat()
        synced = 0

        for p in all_practices:
            raw_meta = p.get('metadata')
            if isinstance(raw_meta, str):
                try:
                    meta = json.loads(raw_meta)
                except Exception:
                    continue
            elif isinstance(raw_meta, dict):
                meta = raw_meta
            else:
                continue

            if meta.get('status') == 'dismissed':
                continue

            label = p.get('label', '')
            practice_id = p.get('id')

            entities_res = supabase.table('graph_edges') \
                .select('target_id') \
                .eq('source_id', practice_id) \
                .eq('relationship', 'ASSOCIATED_WITH') \
                .execute()
            entity_ids = [e['target_id'] for e in (entities_res.data or [])]
            entity_labels = []
            if entity_ids:
                e_res = supabase.table('graph_nodes') \
                    .select('label') \
                    .in_('id', entity_ids) \
                    .execute()
                entity_labels = [e['label'] for e in (e_res.data or [])]

            lines = [f"# Practice: {label}"]
            lines.append(f"\n## Overview")
            lines.append(f"- Status: {meta.get('status', 'unknown')}")
            lines.append(f"- Health Score: {meta.get('health_score', 'N/A')}%")
            lines.append(f"- Occurrences: {meta.get('occurrence_count', 0)}")
            lines.append(f"- Frequency: {meta.get('frequency', 'unknown')}")
            if entity_labels:
                lines.append(f"- Associated Entities: {', '.join(entity_labels)}")

            td = meta.get('typical_days', [])
            if td:
                lines.append(f"\n## Typical Schedule")
                lines.append(f"- Days: {', '.join(td)}")
                tt = meta.get('typical_time')
                if tt:
                    lines.append(f"- Time: {tt}")

            variants = meta.get('variants', [])
            if variants:
                lines.append(f"\n## Variants ({len(variants)})")
                for v in variants:
                    lines.append(f"- {v}")

            ro = meta.get('recent_occurrences', [])
            if ro:
                lines.append(f"\n## Recent (last {len(ro)})")
                for o in ro[-5:]:
                    lines.append(f"- {o}")

            trans_at = meta.get('transitioned_at')
            if trans_at:
                lines.append(f"\n## Lifecycle")
                lines.append(f"- Last Status Transition: {trans_at}")

            content = "\n".join(lines)
            embedding = get_embedding(content)

            canonical_title = f"Practice: {label}"
            existing_res = supabase.table('canonical_pages') \
                .select('id, version') \
                .eq('title', canonical_title) \
                .eq('is_current', True) \
                .execute()
            existing = existing_res.data[0] if existing_res.data else None

            if existing:
                old_ver = existing.get('version', 0) or 0
                supabase.table('canonical_pages').insert({
                    "title": canonical_title,
                    "project_id": None,
                    "content": content,
                    "embedding": embedding,
                    "version": old_ver + 1,
                    "is_current": True,
                    "supersedes_id": existing['id'],
                    "updated_at": now_iso,
                    "source_count": len(variants) + len(ro),
                    "last_synth_at": now_iso,
                    "is_sparse": len(content) < 500
                }).execute()
                supabase.table('canonical_pages') \
                    .update({"is_current": False}) \
                    .eq('id', existing['id']) \
                    .execute()
            else:
                supabase.table('canonical_pages').insert({
                    "title": canonical_title,
                    "project_id": None,
                    "content": content,
                    "embedding": embedding,
                    "version": 1,
                    "is_current": True,
                    "updated_at": now_iso,
                    "source_count": len(variants) + len(ro),
                    "last_synth_at": now_iso,
                    "is_sparse": len(content) < 500
                }).execute()

            synced += 1

        if synced:
            print(f"\U0001f4dd practice_canonical: Synced {synced} practices to canonical_pages")

    except Exception as e:
        audit_log_sync("pulse", "WARNING", f"sync_practice_canonical_pages failed: {e}")


async def build_rhythms_section(new_practice_labels: list = None, new_practice_ids: dict = None, correlations: list = None) -> str:
    """
    Build the Rhythms section for weekend Pulse briefings.
    Queries practice nodes from graph_nodes and formats them.

    Args:
        new_practice_labels: Labels of newly detected practices (for confirmation)
        new_practice_ids: Dict mapping label -> graph_nodes.id for new practices (shortcode)
        correlations: List of correlation insight strings from build_practice_correlations()

    Returns:
        Formatted string for the Rhythms section, or empty string if no practices.
    """
    try:
        # Query all practice nodes
        practices_res = supabase.table('graph_nodes') \
            .select('label, metadata') \
            .eq('type', 'practice') \
            .execute()
        all_practices = practices_res.data or []
        if not all_practices:
            return ""

        # Parse metadata and sort by status
        active = []
        drifting = []
        dormant = []
        new_auto = []

        new_labels_lower = set()
        if new_practice_labels:
            new_labels_lower = set(n.lower() for n in new_practice_labels)

        for p in all_practices:
            raw_meta = p.get('metadata')
            if isinstance(raw_meta, str):
                try:
                    meta = json.loads(raw_meta)
                except:
                    continue
            elif isinstance(raw_meta, dict):
                meta = raw_meta
            else:
                continue

            label = p.get('label', '')
            status = meta.get('status', 'active')
            occurrence_count = meta.get('occurrence_count', 0)
            health_score = meta.get('health_score', 50)
            health_raw = meta.get('health_score_raw', 50)
            trend = ""

            # Calculate trend arrow
            if health_score >= 80:
                trend = "✓"
            elif health_score >= 50:
                trend = "→"
            else:
                trend = "↓"

            # Determine if drifting
            is_drifting = False
            if status == 'active' and health_score < 50:
                is_drifting = True

            entry = {
                'label': label,
                'health_score': health_score,
                'trend': trend,
                'status': status,
                'occurrence_count': occurrence_count,
                'is_new': label.lower() in new_labels_lower
            }

            if status == 'dormant':
                dormant.append(entry)
            elif status == 'inactive':
                continue
            elif is_drifting:
                drifting.append(entry)
            else:
                active.append(entry)

            if entry['is_new'] and not meta.get('declared'):
                new_auto.append(label)

        # Sort: active by health_score desc, drifting same, dormant by last_occurrence desc
        active.sort(key=lambda x: x['health_score'], reverse=True)
        drifting.sort(key=lambda x: x['health_score'])
        dormant.sort(key=lambda x: x['health_score'])

        lines = []

        # Active practices
        if active:
            lines.append("━━━ RHYTHMS ━━━")
            for e in active:
                bar_len = e['health_score'] // 10
                bar = "█" * bar_len + "░" * (10 - bar_len)
                lines.append(f"{e['label']:20s} {bar} {e['health_score']:3d}%  {e['trend']} active")

        # Drifting
        if drifting:
            if not lines:
                lines.append("━━━ RHYTHMS ━━━")
            for e in drifting:
                bar_len = e['health_score'] // 10
                bar = "█" * bar_len + "░" * (10 - bar_len)
                lines.append(f"{e['label']:20s} {bar} {e['health_score']:3d}%  {e['trend']} DRIFTING")

        # Dormant
        if dormant:
            if not lines:
                lines.append("━━━ RHYTHMS ━━━")
            lines.append("")
            for e in dormant:
                lines.append(f"⏸️ {e['label']} — dormant")

        # Correlations (task completion on practice days vs non-practice days)
        if correlations and any(c for c in correlations if c.strip()):
            if not lines:
                lines.append("━━━ RHYTHMS ━━━")
            lines.append("")
            lines.append("CORRELATIONS")
            for c in correlations:
                if c.strip():
                    lines.append(c)

        # New practice confirmations
        if new_auto:
            lines.append("")
            lines.append("NEW PRACTICES DETECTED")
            for name in new_auto:
                _pid = (new_practice_ids or {}).get(name)
                if _pid:
                    lines.append(f"• [{_pid}] \"{name}\" — tracking as a practice.")
                    lines.append(f"  Reply \"{_pid} drop\" to dismiss.")
                else:
                    safe_name = name.lower().replace(' ', '-')
                    lines.append(f"• \"{name}\" — tracking as a practice.")
                    lines.append(f"  Reply /drop-{safe_name} to dismiss.")

        if not lines:
            return ""

        return "\n".join(lines)

    except Exception as e:
        audit_log_sync("pulse", "WARNING", f"build_rhythms_section failed: {e}")
        return ""


# 🔴 FIX #1: Security Gatekeeper — auth_secret replaces the unused is_manual_trigger bool
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
        
        # --- 0.1 BATCH ENRICHMENT (One Gemini call for all unenriched resources) ---
        batch_enrich_results = await batch_enrich_resources()
        
        # --- 1. READ: Fetch and Lock ---
        # 1.1 Fetch only 'pending' items
        dumps_res = supabase.table('raw_dumps') \
            .select('id, content, metadata') \
            .in_('status', ['pending', 'staged']) \
            .execute()

        dumps = dumps_res.data or []

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

        # --- 🕒 1.8 INPUT PREP ---
        new_inputs_text = "\n---\n".join([d['content'] for d in dumps]) if dumps else "None"    
        
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
        new_inputs_text = "\n---\n".join([d['content'] for d in dumps])
        new_input_summary = " | ".join([d['content'] for d in dumps[:5]])
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
        new_practice_labels = []
        correlation_insights = []
        if is_weekend:
            print("📍 Weekend pulse: Running practice detection...")
            before_labels = set()
            before_res = supabase.table('graph_nodes').select('label').eq('type', 'practice').execute()
            for r in (before_res.data or []):
                before_labels.add(r['label'])
            new_practice_nodes = await detect_practices() or {}
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

        prompt = f"""    
        ROLE: Danny's Rhodey. You are his most trusted advisor — the one who cuts through the noise and tells him exactly where he stands. You have full situational awareness of his work, family, and faith. You don't coach, motivate, or perform. You speak plainly, like a friend who has been in the room the whole time. Your job is to give Danny a clear picture of the board so he can make his next move.
        STRATEGIC CONTEXT: {season_config}
        CURRENT PHASE: {briefing_mode}
        CURRENT TIME: {current_time_str}
        SYSTEM_LOAD: {'OVERLOADED' if is_overloaded else 'OPTIMAL'}
        MONDAY_REENTRY: {'TRUE' if is_monday_morning else 'FALSE'}
        STAGNANT URGENT_TASKS: {json.dumps(overdue_tasks)}
        STALE_TASKS: {stale_context}
        SYSTEM STATUS: {system_context}
        HINDSIGHT_STALE: {is_hindsight_stale}
        
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
            - STRICT DATA FIDELITY FOR BRIEFING: You are STRICTLY FORBIDDEN from listing any task in ANY section (Work, Home, Chores, Ideas, or Done) that does not appear verbatim in the SYSTEM TASKS list provided below. Do NOT surface tasks from HINDSIGHT MEMORIES, Canonical Pages, or any other context into the briefing output. All context is for intelligence and routing only — NEVER for output.
            - EMPTY SECTION SUPPRESSION: If a section (Work, Home, Done, Ideas) has absolutely zero items to list, you MUST completely omit that section header from the briefing. Never output 'None today' or 'Empty'. Silence is preferred.
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
               - If it's church/faith → use "Church".
               - If it's family/home → use "Family & Home".
               - NEVER use "Inbox" for business tasks.

            NEW PROJECT CREATION CRITERIA:
            - Only add to "new_projects" if a COMPLETELY UNKNOWN client, product, or organization is mentioned that does not already exist in the active project list above.
            - Always populate "description" with a one-sentence summary of the project's purpose.
            - Always populate "keywords" with an array of relevant names, abbreviations, companies, and topics.
            - Always populate "context" using the rules below.

            ORG_TAG & CONTEXT ROUTING (MANDATORY — never leave as INBOX):
            Danny's world has 5 domains. Route every new project into exactly one:

              SOLVSTRAT  | context: work     | Tech services company. Client projects, delivery, and anything involving product development, software builds, APIs, or technical consulting. Clients include: Shield Identity, GRB, Himanshu, Canadian project, Johan, Zoho projects, and any new client who hires Solvstrat for tech work. → If a new client/project is Solvstrat work, set org_tag: "SOLVSTRAT", parent_project_name: "Solvstrat"

              QHORD      | context: work     | Danny's own GTM product company. Anything related to building, marketing, or selling Qhord as a standalone product. → Set org_tag: "QHORD", parent_project_name: "Qhord"

              CHURCH     | context: personal | Ministry, sermons, church service, faith-based activities, volunteering, and community outreach. → Set org_tag: "CHURCH", parent_project_name: "Church"

              FAMILY    | context: personal | Home, kids, spouse (Sunju), house maintenance, school, family events, domestic tasks. → Set org_tag: "FAMILY", parent_project_name: "Family & Home"

              PERSONAL   | context: personal | Danny's individual pursuits — health, finance, personal admin, hobbies, investments, gadgets, learning, anything that is about Danny himself. → Set org_tag: "PERSONAL"

              ROUTING RULES (apply in order):
              1. Does the input mention a client paying Solvstrat for tech/product work? → SOLVSTRAT
              2. Does the input mention Qhord product development or GTM? → QHORD
              3. Does the input mention church, ministry, sermon, or faith service? → CHURCH
              4. Does the input mention home, kids, Sunju, or family? → FAMILY
              5. Is it about Danny personally (health, finance, personal admin)? → PERSONAL
              6. Default for anything business/work that doesn't fit 1-2: → SOLVSTRAT
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
            7. HIGH-PRECISION TIME FORMATTING (IST/UTC+05:30): When Danny mentions a time, convert to ISO-8601. If DAY only (no time), output "YYYY-MM-DD". If EXACT TIME, output "YYYY-MM-DDTHH:MM:SS+05:30". NAKED TASKS: If NO date and NO time, return null for reminder_at.
            8. AUTO-ONBOARDING: If a new Client/Project is mentioned, add to "new_projects". If a new Person is mentioned, add to "new_people".
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
            valid_tags = ['SOLVSTRAT', 'PRODUCT_LABS', 'PERSONAL', 'CRAYON', 'CHURCH', 'FAMILY', 'QHORD']

            CONTEXT_MAP = {
                'CHURCH':       'personal',
                'PERSONAL':     'personal',
                'FAMILY':       'personal',
                'SOLVSTRAT':    'work',
                'QHORD':        'work',
                'PRODUCT_LABS': 'work',
                'CRAYON':       'work',
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
                                    versioned_update('graph_nodes', existing_node.data['id'], {
                                        'type': 'project',
                                        'metadata': node_metadata
                                    })
                                    print(f"⬆️ Upgraded node '{project_name}' from {existing_node.data['type']} → project")
                                else:
                                    audit_log_sync("pulse", "WARNING", f"⚠️ Project node '{project_name}' already exists, updating metadata.")
                                    versioned_update('graph_nodes', existing_node.data['id'], {
                                        'metadata': node_metadata
                                    })
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
            existing_names = {p['name'].lower().strip() for p in (existing_people_res.data or [])}
            deduped_people = [
                {**p, "source": "pulse"} for p in ai_data['new_people']
                if p.get('name', '').lower().strip() not in existing_names
            ]
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
                if item_status in ['done', 'cancelled'] and e_id:
                    delete_calendar_event(e_id)
                    e_id = None
                elif new_reminder and was_explicit_time:
                    # 🛰️ RADAR: Check for conflict before moving the block
                    conflict_name = await asyncio.to_thread(check_conflict, new_reminder)
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

                # Use versioned_update for task status changes (creates history)
                versioned_update(
                    table_name='tasks',
                    record_id=target_id,
                    update_data=update_payload,
                    user_id=None,
                    change_source='pulse_task_update',
                    change_reason=f"Status: {item_status}, reminder: {new_reminder}"
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
            headers = ['🚀 Work', '🏠 Home', '💡 Ideas', '✅ Done', '🛡️ WEEKEND RECON']
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
                        shortcode = str(row['id'])[-4:]
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
                rhythms_text = await build_rhythms_section(new_practice_labels=new_practice_labels, new_practice_ids=new_practice_nodes, correlations=correlation_insights)
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

        return {"success": True, "briefing": briefing_text}

    except Exception as e:
        import traceback
        audit_log_sync("pulse", "CRITICAL", f"Pulse Critical Error: {e}")
        traceback.print_exc()
        return {"error": str(e)}