# api/webhook.py
import os
import json
import asyncio
import httpx
import re
import base64
from email.mime.text import MIMEText
from supabase import create_client, Client
from datetime import datetime, timezone, timedelta
from google import genai
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.discovery_cache import base

# Import rate limiter
try:
    from core.rate_limiter import flash_lite_limiter
except ImportError:
    from rate_limiter import flash_lite_limiter

# Import audit_logger (with robust path handling for Vercel)
try:
    from audit_logger import audit_log_sync
except ImportError:
    try:
        # Try as core.audit_logger (for Vercel deployment)
        from core.audit_logger import audit_log_sync
    except ImportError:
        # Fallback: define local version
        def audit_log_sync(service, level, message, metadata=None):
            print(f"[{level}] {service}: {message}")
        import sys
        parent_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        if parent_dir not in sys.path:
            sys.path.insert(0, parent_dir)

# Import versioned_update from pulse (with robust path handling for vercel)
try:
    # Try direct import (works when both files are in same directory)
    from pulse import versioned_update, add_to_failed_queue
except ImportError:
    try:
        # Fallback: add parent directory to path
        import sys
        parent_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        if parent_dir not in sys.path:
            sys.path.insert(0, parent_dir)
        from pulse import versioned_update, add_to_failed_queue
    except ImportError:
        # If all fails, define a local fallback (shouldn't happen)
        def versioned_update(table_name, record_id, update_data):
            print(f"Warning: versioned_update not available, using direct update")
            supabase.table(table_name).update(update_data).eq('id', record_id).execute()
            return True
        
        async def add_to_failed_queue(source_table, source_id, operation, error_message):
            try:
                supabase.table('failed_queue').insert({
                    "source_table": source_table,
                    "source_id": str(source_id),
                    "operation": operation,
                    "error_message": str(error_message)[:500] if error_message else None,
                }).execute()
            except:
                pass

# Import conversation module (session management for Q&A)
from core.conversation import (
    get_or_create_session,
    get_history,
    log_exchange,
    format_history_for_prompt
)

supabase: Client = create_client(
    os.getenv("SUPABASE_URL"), 
    os.getenv("SUPABASE_SERVICE_ROLE_KEY")
)

class MemoryCache(base.Cache):
    _cache = {}
    def get(self, url):
        return self._cache.get(url)
    def set(self, url, content):
        self._cache[url] = content

def get_google_creds():
    """Unified credential handshake for Google services."""
    return Credentials(
        None,
        refresh_token=os.getenv("GOOGLE_REFRESH_TOKEN"),
        client_id=os.getenv("GOOGLE_CLIENT_ID"),
        client_secret=os.getenv("GOOGLE_CLIENT_SECRET"),
        token_uri="https://oauth2.googleapis.com/token"
    )

def normalize_title(title: str) -> str:
    """Normalize title for comparison: lowercase, strip punctuation, collapse whitespace."""
    import re
    # Lowercase
    normalized = title.lower()
    # Strip punctuation (keep alphanumeric and spaces)
    normalized = re.sub(r'[^a-z0-9\s]', '', normalized)
    # Collapse repeated whitespace
    normalized = re.sub(r'\s+', ' ', normalized).strip()
    return normalized


def is_already_in_tasks_table(title: str) -> bool:
    """Check if a similar task already exists in the tasks table.
    Uses strict matching: normalized exact match or extremely high similarity.
    Fails open on errors."""
    try:
        # First, try exact normalized match
        normalized_title = normalize_title(title)
        if not normalized_title:
            return False
        
        # Fetch active tasks for comparison
        result = supabase.table('tasks')\
            .select('id, title')\
            .not_.in_('status', ['done', 'cancelled'])\
            .execute()
        
        if not result.data:
            return False
        
        # Check for exact normalized match
        for task in result.data:
            existing_title = task.get('title', '')
            if normalize_title(existing_title) == normalized_title:
                print(f"⚠️ Duplicate guard: '{title}' matches "
                      f"existing task (id: {task['id']}, title: '{existing_title}'). "
                      f"Skipping.")
                return True
        
        # No exact match found
        return False
    except Exception as e:
        audit_log_sync("webhook", "WARNING", f"Duplicate guard check failed (failing open): {e}")
        return False  # Fail open — never block suggestion creation on DB error


UPDATE_TRIGGER_WORDS = {'update', 'reschedule', 'reschedule', 'change', 'move', 'push', 'postpone', 'delay', 'bring', 'advance'}


def check_task_overlap_for_update(text: str) -> list:
    """Check if message keywords overlap with active tasks (≥2 keyword match).
    Returns list of matched task dicts, empty if below threshold."""
    try:
        keywords = [w.lower() for w in text.split() if len(w) > 4]
        if len(keywords) < 2:
            return []
        active_keywords = keywords[:3]

        tasks_res = supabase.table('tasks')\
            .select('id, title')\
            .eq('is_current', True)\
            .not_.in_('status', ['done', 'cancelled'])\
            .execute()
        if not tasks_res.data:
            return []

        matched = []
        for task in tasks_res.data:
            existing = task.get('title', '').lower()
            count = sum(1 for kw in active_keywords if kw in existing)
            if count >= 2:
                matched.append(task)
        return matched
    except Exception as e:
        audit_log_sync("webhook", "WARNING", f"Task overlap check failed: {e}")
        return []


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


def is_recent_raw_dump(content: str, source: str) -> bool:
    """Check if identical content+source was inserted in the last 60 seconds.
    Used as idempotency guard against Telegram double-fires and user double-taps."""
    try:
        cutoff = (datetime.now(timezone.utc) - timedelta(seconds=60)).isoformat()
        dup = supabase.table('raw_dumps') \
            .select('id') \
            .eq('content', content) \
            .eq('source', source) \
            .gte('created_at', cutoff) \
            .limit(1) \
            .execute()
        if dup.data:
            print(f"Duplicate guard: Skipping '{content[:50]}...' — inserted within 60s")
            return True
        return False
    except Exception as e:
        audit_log_sync("webhook", "WARNING", f"Duplicate guard check failed (failing open): {e}")
        return False


OPPORTUNITY_PATTERNS = [
    r"new possible project",
    r"potential opportunity",
    r"opportunity with",
    r"we will be tasked",
    r"project opportunity",
    r"potential project",
    r"potential client",
    r"might work on",
    r"client called",
    r"there is a new",
    r"possible new",
]


def detect_opportunity_language(text: str) -> bool:
    text_lower = text.lower()
    for pattern in OPPORTUNITY_PATTERNS:
        if re.search(pattern, text_lower):
            return True
    return False


async def process_email_pending_decision(pending_id: int, decision: str, supabase_client=None) -> dict:
    """Process approve/reject for an email pending task (shared by Telegram + API).

    For 'approve': inserts into raw_dumps then sets danny_decision='approved'.
    For 'reject': sets danny_decision='rejected' and cleans up orphan drafts.

    Args:
        pending_id: ID in email_pending_tasks table.
        decision: 'approve' or 'reject'.
        supabase_client: Optional supabase client (defaults to module-level).

    Returns: dict with keys: success (bool), message (str), action (str|None).
    """
    client = supabase_client or supabase

    # Look up pending row
    row_res = client.table('email_pending_tasks')\
        .select('*')\
        .eq('id', pending_id)\
        .is_('danny_decision', 'null')\
        .limit(1)\
        .maybe_single()\
        .execute()

    if not row_res.data:
        decided = client.table('email_pending_tasks')\
            .select('id, danny_decision')\
            .eq('id', pending_id)\
            .not_.is_('danny_decision', 'null')\
            .limit(1)\
            .maybe_single()\
            .execute()
        if decided.data:
            return {
                "success": False, "action": "already_decided",
                "message": f"[{pending_id}] was already {decided.data['danny_decision']}."
            }
        return {
            "success": False, "action": "not_found",
            "message": f"No task found matching [{pending_id}]."
        }

    row = row_res.data
    title = row.get('suggested_title', '')
    email_id = row.get('email_id')
    is_human = row.get('is_human_sender', False)

    if decision == 'approve':
        if is_already_in_tasks_table(title):
            client.table('email_pending_tasks').update({'danny_decision': 'skipped'}).eq('id', row['id']).execute()
            return {
                "success": False, "action": "duplicate",
                "message": f"A similar task already exists on your board: [{title}]"
            }

        try:
            client.table('raw_dumps').insert([{
                "content": title,
                "source": "email",
                "status": "pending",
                "direction": "incoming",
                "sender": "user",
                "message_type": "task",
                "metadata": {
                    "email_id": email_id,
                    "is_human_sender": is_human
                }
            }]).execute()
        except Exception:
            return {
                "success": False, "action": "staging_failed",
                "message": f"Task staging failed for [{row['id']}]. You can retry."
            }

        client.table('email_pending_tasks').update({'danny_decision': 'approved'}).eq('id', row['id']).execute()
        print(f"Staged to raw_dumps via email approval: {title}")
        return {"success": True, "action": "approved", "message": f"Task staged: {title}"}

    elif decision == 'reject':
        client.table('email_pending_tasks').update({'danny_decision': 'rejected'}).eq('id', row['id']).execute()
        try:
            draft_res = supabase.table('email_drafts')\
                .select('id')\
                .eq('email_id', email_id)\
                .maybe_single()\
                .execute()
            if draft_res.data:
                supabase.table('email_drafts')\
                    .update({'danny_decision': 'skipped'})\
                    .eq('id', draft_res.data['id'])\
                    .execute()
        except Exception:
            pass
        return {"success": True, "action": "rejected", "message": f"Dropped: {title}"}

    else:
        return {
            "success": False, "action": "invalid_action",
            "message": f"Invalid decision: {decision}. Must be 'approve' or 'reject'."
        }


gemini_client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))

EMBEDDING_MODEL = "gemini-embedding-2-preview"
CLASSIFICATION_MODEL = "gemini-3.1-flash-lite-preview"
EMBEDDING_DIMENSION = 768


async def trigger_github_pulse() -> bool:
    """Trigger GitHub Actions workflow dispatch for pulse briefing."""
    try:
        github_token = os.getenv("GITHUB_TOKEN")
        if not github_token:
            print("ERROR: GITHUB_TOKEN not set")
            return False
        
        owner = os.getenv("GITHUB_OWNER", "Crayon-Biz-LLP")
        repo = os.getenv("GITHUB_REPO", "integrated-os")
        
        url = f"https://api.github.com/repos/{owner}/{repo}/dispatches"
        
        headers = {
            "Authorization": f"token {github_token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28"
        }
        
        payload = {
            "event_type": "trigger_pulse"
        }
        
        async with httpx.AsyncClient() as client:
            response = await client.post(url, json=payload, headers=headers, timeout=10)
            
            if response.status_code == 204:
                print("✓ GitHub Actions workflow triggered successfully")
                return True
            else:
                audit_log_sync("webhook", "ERROR", f"GitHub dispatch failed: {response.status_code}")
                return False
                
    except Exception as e:
        audit_log_sync("webhook", "ERROR", f"ERROR triggering GitHub pulse: {e}")
        return False


async def call_gemini_with_retry(prompt: str, model: str = None, config: dict = None, contents=None):
    """Call Gemini with retry logic (3 retries, exponential backoff for 503 errors)."""
    if model is None:
        model = CLASSIFICATION_MODEL
    
    max_retries = 3
    base_delay = 1
    
    for attempt in range(max_retries):
        try:
            # Rate limit: only for flash-lite model
            if "flash-lite" in model:
                await flash_lite_limiter.acquire_async()
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
            retryable_errors = ['503', '504', '500', 'timeout', 'deadline exceeded']
            should_retry = any(err in error_str for err in retryable_errors)
            if should_retry and attempt < max_retries - 1:
                delay = base_delay * (2 ** attempt)
                audit_log_sync("webhook", "WARNING", f"⚠️ Gemini 503 error, retrying in {delay}s (attempt {attempt + 1}/{max_retries})...")
                await asyncio.sleep(delay)
                continue
            else:
                raise


def get_embedding(text: str) -> list:
    try:
        # 🎯 Force the model to return 768 dimensions to match Supabase
        result = gemini_client.models.embed_content(
            model=EMBEDDING_MODEL,
            contents=text,
            config={
                'output_dimensionality': EMBEDDING_DIMENSION
            }
        )
        return result.embeddings[0].values
    except Exception as e:
        audit_log_sync("webhook", "ERROR", f"Embedding error: {e}")
        return [0] * EMBEDDING_DIMENSION


async def classify_intent(text: str, context: list, ist_hour: int = None, core_json: str = "[]", conversation_history: str = "") -> dict:
    ist_offset = timezone(timedelta(hours=5, minutes=30))
    now = datetime.now(ist_offset)
    current_hour = ist_hour if ist_hour is not None else now.hour
    
    if 4 <= current_hour < 12:
        time_phase = "morning"
    elif 12 <= current_hour < 18:
        time_phase = "afternoon"
    else:
        time_phase = "night"
    
    context_str = ""
    if context:
        context_str = f"\n\nPrevious messages for context:\n" + "\n".join([f"- {c['content']}" for c in context])
    
    prompt = f"""You are Danny's Rhodey. Pragmatic, loyal, and a professional friend. You are the grounding wire to Danny's vision. You don't coach or 'motivate.' Speak simply and punchy. If it's after 9 PM, append a dry command to sign off (e.g., 'Go be a dad').

    PROHIBIT ACTION HALLUCINATION: You are a logging tool, not an agent. NEVER say 'I'll ping', 'I'll check', or 'I'll handle it'. You cannot contact people. Your only job is to confirm Danny's task is SECURED in his system.

    Message: "{text}"{context_str}{conversation_history}
    CURRENT TIME CONTEXT: It's the {time_phase}.
    IDENTITY & BUSINESS CONTEXT: {core_json}

    Return ONLY valid JSON (no markdown, no explanation):
    {{
        "intent": "TASK|NOTE|NOISE|CLARIFICATION_NEEDED|DELEGATE|QUERY|DECLARE_PRACTICE|DAILY_BRIEF",
        "confidence": 0.0-1.0,
        "entity": "SOLVSTRAT|QHORD|PERSONAL|CHURCH|INBOX",
        "title": "extracted task title",
        "time_context": "time info if any",
        "clarification_question": "question if needed",
        "receipt": "Stealth status report (no entity names).",
        "possible_intents": ["TASK", "NOTE", "QUERY", "DAILY_BRIEF", "DELEGATE", "DECLARE_PRACTICE", "NOISE"],
        "reasoning": "brief logic"
    }}

    Rules:
    - STRICT TITLE FIDELITY: The title field must be a literal extraction of the task as spoken. NEVER add project names, infer entities, or change Danny's wording (e.g., if he says "this OS," do NOT change it to "Qhord OS").
    - PROJECT ROUTING: Route tasks about personal finances, bills, home, or family to PERSONAL. Only route to CRAYON if it relates to corporate governance, business taxes, or legal compliance. Route tech/client work to SOLVSTRAT.
    - STATUS vs TASK: If a message describes something that HAS HAPPENED (e.g., 'Lead generated', 'Meeting finished', 'Sent the file'), classify it as a NOTE. A TASK must imply an OUTSTANDING action for Danny to perform (e.g., 'Call the lead', 'Prepare the ERP plan'). If it's a win or a milestone, it's a NOTE for the Historian.
    - TASK: Any message that implies an action. Do not require a date or time.
    - NOTE: Ideas, insights, or learnings worth remembering.
    - QUERY: The user is asking a question to retrieve information from their past notes, tasks, or the vault (e.g., "What did the analyst say?", "When is my meeting?").
    - DISAMBIGUATION: If confidence < 0.8 and you're torn between multiple intents, list alternatives in "possible_intents". For example, if a message could be either a QUERY or a TASK, set intent to your best guess and possible_intents to ["TASK", "QUERY"]. Leave as an empty array if you're confident.
    - CONVERSATION HISTORY: Use the CONVERSATION HISTORY block above to disambiguate vague follow-ups. If Danny says "what about tomorrow?" after having just asked about today, route as DAILY_BRIEF. If he says "reschedule the 2pm" after discussing calendar, route as TASK. The history tells you what the current topic is.
    - DELEGATE: Research, competitor audits, or autonomous web research.
    - DECLARE_PRACTICE: If Danny says "I want to [activity] every [timeframe]", "I'm going to start [activity]", "Track [activity] for me", "I want to build a practice of [activity]", or expresses intent to establish a recurring behavior — classify as DECLARE_PRACTICE. Extract the practice name into the title field. Route to the most relevant entity (PERSONAL for health/personal routines, SOLVSTRAT for work practices, etc.).
    - DAILY_BRIEF: Danny is asking about today's schedule, calendar, or what's on his plate. Examples: "what's today?", "what's on my calendar?", "what do I have today?", "give me my day", "what's happening today?". Extract into title: "Daily Briefing". Entity: INBOX.
    - RECEIPT RULE: Receipts must be confirmation-only. Use: '[Subject] logged for [Time/Day].'
    - LITERAL SUBJECT RULE: Mirror Danny's verb. (e.g., 'Check with Vasanth' → 'Vasanth check-in logged').
    - ZERO DATA LOSS: Never drop qualifiers like 'Canadian project' or 'Zoho API'.
    - STEALTH ROUTING: Assign the entity in the JSON, but NEVER mention it (SOLVSTRAT, PERSONAL) in the receipt text.
    - DATE HANDSHAKE: If a time or day is mentioned, include it in the receipt for verification.
    - If it's night (Phase: night), confirm the entry first, THEN give the sign-off command. (e.g., 'Vasanth check-in logged. Now go be a dad.').
    - TONE GUARD: NEVER use: 'momentum', 'focus', 'gentle', 'reflection', 'push', 'strategic', 'SITREP', 'optimal', 'mission', 'ready for your review'.
    - PROHIBIT ACTION HALLUCINATION: You are a logging tool, not an agent. NEVER say 'I'll ping', 'I'll check', or 'I'll handle it'. You cannot contact people. Your only job is to confirm Danny's task is SECURED in his system.
    - STRATEGIC CORRECTIONS: If Danny starts a message with 'Record this for the Vault', 'Correction for the Historian', or 'Correction of Record', classify it immediately as a NOTE with 1.0 confidence. These are manual strategic overrides and must never be ignored.
    - META-SYSTEM CONTENT: Allow content that talks about 'Atna', 'Solvstrat', or 'Qhord' even if the message is long or complex. These are high-value strategic inputs."""

    try:
        response = await call_gemini_with_retry(
            prompt=prompt,
            model=CLASSIFICATION_MODEL,
            config={'response_mime_type': 'application/json'}
        )
        clean_json = response.text.replace('```json', '').replace('```', '').strip()
        result = json.loads(clean_json)
        return result
    except Exception as e:
        audit_log_sync("webhook", "ERROR", f"Classification parse error: {e}")
        return {"intent": "NOTE", "confidence": 0.8, "receipt": "Manual correction secured in the vault."}


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
- Pick emojis naturally: 💰 money, 🏠 home, 📋 admin, 🛠️ work, 🏛️ church, etc.
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


async def get_recent_context(limit: int = 2) -> list:
    try:
        res = supabase.table('raw_dumps')\
            .select('content')\
            .eq('is_processed', False)\
            .order('created_at', desc=True)\
            .limit(limit)\
            .execute()
        return res.data if res.data else []
    except:
        return []


async def download_telegram_file(file_id: str) -> tuple[bytes, str]:
    """Download file from Telegram and return (bytes, mime_type)."""
    try:
        bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
        
        async with httpx.AsyncClient() as client:
            file_info = await client.get(f"https://api.telegram.org/bot{bot_token}/getFile?file_id={file_id}")
            file_data = file_info.json()
            
            if not file_data.get('ok'):
                raise Exception(f"Telegram API error: {file_data}")
            
            file_path = file_data['result']['file_path']
            mime_type = file_data['result'].get('mime_type', 'application/octet-stream')
            
            download_url = f"https://api.telegram.org/bot{bot_token}/file/{file_path}"
            file_bytes = await client.get(download_url)
            
            return file_bytes.content, mime_type
    except Exception as e:
        raise Exception(f"Failed to download Telegram file {file_id}: {e}")


async def process_multimodal_content(file_bytes: bytes, mime_type: str, chat_id: int, ist_hour: int = None, core_json: str = "[]"):
    """Process audio, image, or document content and extract tasks and insights."""
    ist_offset = timezone(timedelta(hours=5, minutes=30))
    now = datetime.now(ist_offset)
    current_hour = ist_hour if ist_hour is not None else now.hour
    
    if 4 <= current_hour < 12:
        time_phase = "morning"
    elif 12 <= current_hour < 18:
        time_phase = "afternoon"
    else:
        time_phase = "night"
    
    prompt = f"""You are Danny's Rhodey. Pragmatic, loyal, and a professional friend. You are the grounding wire to Danny's vision. You don't coach or 'motivate.' Speak simply and punchy. If it's after 9 PM, append a dry command to sign off (e.g., 'Go be a dad').

    PROHIBIT ACTION HALLUCINATION: You are a logging tool, not an agent. NEVER say 'I'll ping', 'I'll check', or 'I'll handle it'. You cannot contact people. Your only job is to confirm Danny's task is SECURED in his system.

    CURRENT TIME CONTEXT: It's the {time_phase}.

    IDENTITY & BUSINESS CONTEXT: {core_json}

    THE STRATEGIC MAP: PROJECT ROUTING: Route tasks about personal finances, bills, home, or family to PERSONAL. Only route to CRAYON if the task specifically relates to corporate governance, business taxes, or legal compliance. Route tech/client work to SOLVSTRAT. Default to INBOX.

    ---
    MULTIMODAL INSTRUCTIONS:
    If an IMAGE: Transcribe text, analyze UI/Design patterns, identify strategic diagrams or URLs.
    If AUDIO: Extract explicit actions, deadlines, decisions, and research requests. 
    If DOCUMENT: Summarize intent, extract deliverables, legal obligations, and deadlines.

    RULES:
    - TASK: Any implied action (Send, Call, Fix). Do not require a date. 
    - NOTE: Strategic insights, facts, or observations worth remembering.
    - DELEGATE: Research requests, competitor audits, or dossier building.
    - RECEIPT RULE: Receipts must be confirmation-only. Use: '[Subject] logged for [Time/Day].'
    - LITERAL SUBJECT RULE: Mirror Danny's verb. (e.g., 'Check with Vasanth' → 'Vasanth check-in logged').
    - ZERO DATA LOSS: Never drop qualifiers like 'Canadian project' or 'Zoho API'.
    - STEALTH ROUTING: Assign the entity in the JSON, but NEVER mention it (SOLVSTRAT, PERSONAL) in the receipt text.
    - DATE HANDSHAKE: If a time or day is mentioned, include it in the receipt for verification.
    - If it's night (Phase: night), confirm the entry first, THEN give the sign-off command. (e.g., 'Vasanth check-in logged. Now go be a dad.').
    - TONE GUARD: NEVER use: 'momentum', 'focus', 'gentle', 'reflection', 'push', 'strategic', 'SITREP', 'optimal', 'mission', 'ready for your review'.
    - PROHIBIT ACTION HALLUCINATION: You are a logging tool, not an agent. NEVER say 'I'll ping', 'I'll check', or 'I'll handle it'. You cannot contact people. Your only job is to confirm Danny's task is SECURED in his system.

    OUTPUT:
    Return ONLY a valid JSON array of objects. For every item, identify the 'entity' (QHORD, SOLVSTRAT, etc.).
    Example: [{{"type": "TASK", "entity": "CRAYON", "content": "Send experience letters to Siva and Suriya by tomorrow"}}]

    Tone: No corporate polish. No "Starship" metaphors. Talk like a high-level partner who knows the time of day and what's at stake.
    """

    try:
        content_parts = [prompt]
        
        if mime_type.startswith('image/'):
            content_parts.append({"mime_type": mime_type, "data": file_bytes})
        elif mime_type.startswith('audio/') or mime_type == 'application/octet-stream':
            content_parts.append({"mime_type": mime_type, "data": file_bytes})
        elif mime_type in ['application/pdf', 'application/vnd.openxmlformats-officedocument.wordprocessingml.document']:
            content_parts.append({"mime_type": mime_type, "data": file_bytes})
        else:
            content_parts.append(file_bytes.decode('utf-8', errors='ignore'))
        
        response = await call_gemini_with_retry(
            contents=content_parts,
            model=CLASSIFICATION_MODEL,
            config={'response_mime_type': 'application/json'}
        )
        
        extracted = json.loads(response.text)
        
        task_count = 0
        note_count = 0
        
        for item in extracted:
            item_type = item.get('type', '').upper()
            content = item.get('content', '')
            
            if not content:
                continue
            
            if item_type == 'TASK':
                supabase.table('raw_dumps').insert([{
                    "content": content,
                    "status": "pending",
                    "direction": "incoming",
                    "sender": "user",  # All user messages have sender "user"
                    "message_type": "task",
                    "source": "multimodal",
                    "metadata": {
                        "source": "multimodal", 
                        "mime_type": mime_type,
                        "entity": item.get('entity')
                    }
                }]).execute()
                task_count += 1
                print(f"📋 Task extracted: {content[:50]}...")
            
            elif item_type == 'NOTE':
                supabase.table('raw_dumps').insert([{
                    "content": content,
                    "status": "pending",
                    "direction": "incoming",
                    "sender": "user",  # All user messages have sender "user"
                    "message_type": "note",
                    "source": "multimodal",
                    "metadata": {
                        "intent": "NOTE",
                        "source": "multimodal",
                        "mime_type": mime_type,
                        "entity": item.get('entity')
                    }
                }]).execute()
                note_count += 1
                print(f"📝 Note staged: {content[:50]}...")
            
            elif item_type == 'DELEGATE':
                supabase.table('agent_queue').insert({
                    "query": content,
                    "status": "pending",
                    "metadata": {"source": "multimodal", "mime_type": mime_type}
                }).execute()
                print(f"🕵️ Agent dispatched: {content[:50]}...")
        
        summary_parts = []
        if task_count > 0:
            summary_parts.append(f"{task_count} Task{'s' if task_count != 1 else ''}")
        if note_count > 0:
            summary_parts.append(f"{note_count} Insight{'s' if note_count != 1 else ''}")
        
        if summary_parts:
            summary = " & ".join(summary_parts)
            await send_telegram(chat_id, f"Logged {summary}.")
        else:
            await send_telegram(chat_id, f"Understood.")
        
        return {"tasks": task_count, "notes": note_count}
    
    except Exception as e:
        audit_log_sync("webhook", "ERROR", f"Multimodal processing error: {e}")
        ack = "Something went wrong. Try sending as text."
        await send_telegram(chat_id, f"⚠️ {ack}")
        return {"tasks": 0, "notes": 0}


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

    try:
        supabase.table('raw_dumps').insert([{
            "content": text,
            "status": "pending",
            "direction": "incoming",
            "sender": sender,
            "message_type": "task",
            "source": source,
            "metadata": meta
        }]).execute()
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


async def handle_confident_note(text: str, chat_id: int, receipt: str = None, source: str = "telegram", sender: str = "user"):
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
        "metadata": {"intent": "NOTE", "entity": None}
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
            "source": "webhook"
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
        audit_log_sync("webhook", "ERROR", f"Hybrid search error: {e}")
        return ""


INTENT_OPTIONS = {
    "t": ("TASK", "📋 Task — something to do"),
    "q": ("QUERY", "❓ Query — answer a question"),
    "n": ("NOTE", "📝 Note — record this"),
    "b": ("DAILY_BRIEF", "📅 Brief — what's on my schedule"),
    "r": ("DELEGATE", "🤖 Research — look something up"),
    "p": ("DECLARE_PRACTICE", "🏃 Practice — track a habit"),
    "x": ("NOISE", "👍 Nothing — just noise"),
}

INTENT_BY_KEYWORD = {}
for _sc, (_intent, _label) in INTENT_OPTIONS.items():
    INTENT_BY_KEYWORD[_intent.lower()] = _intent
    INTENT_BY_KEYWORD[_sc] = _intent


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
        await handle_confident_note(text, chat_id, receipt or "Note secured.", source=source, sender=sender)
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
- Pick emojis naturally: 💰 money, 🏠 home, 📋 admin, 🛠️ work, 🏛️ church, etc.
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


def _chunk_message(text: str, max_len: int = 4000) -> list[str]:
    if len(text) <= max_len:
        return [text]
    chunks = []
    while text:
        if len(text) <= max_len:
            chunks.append(text)
            break
        split_at = text.rfind('\n', 0, max_len)
        if split_at == -1:
            split_at = text.rfind(' ', 0, max_len)
        if split_at == -1:
            split_at = max_len
        chunks.append(text[:split_at])
        text = text[split_at:].lstrip()
    return chunks


async def send_telegram(chat_id: int, message_text: str, show_keyboard: bool = True):
    chunks = _chunk_message(message_text)
    total = len(chunks)
    telegram_bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
    url = f"https://api.telegram.org/bot{telegram_bot_token}/sendMessage"
    success = True
    last_failed = -1
    async with httpx.AsyncClient() as client:
        for i, chunk in enumerate(chunks):
            suffix = f"({i+1}/{total})"
            if total > 1:
                if i == 0:
                    nl = chunk.find('\n')
                    if nl != -1:
                        chunk = chunk[:nl] + " " + suffix + chunk[nl:]
                    else:
                        chunk = chunk + " " + suffix
                else:
                    chunk = suffix + "\n\n" + chunk
            payload = {
                "chat_id": chat_id,
                "text": chunk,
                "parse_mode": "Markdown",
                "disable_web_page_preview": True,
            }
            if show_keyboard and i == total - 1:
                payload["reply_markup"] = {
                    "keyboard": [
                        [{"text": "🔴 Urgent"}, {"text": "📋 Brief"}],
                        [{"text": "🚀 Mission"}, {"text": "📚 Library"}],
                        [{"text": "🧭 Season Context"}, {"text": "🔓 Vault"}],
                        [{"text": "📊 Status"}]
                    ],
                    "resize_keyboard": True,
                    "persistent": True,
                }
            # Send with one retry
            for attempt in range(2):
                try:
                    resp = await client.post(url, json=payload)
                    if resp.status_code == 400 and "can't parse entities" in resp.text.lower():
                        clean = chunk.replace('*', '').replace('_', '').replace('`', '').replace('[', '').replace(']', '')
                        payload["text"] = clean
                        payload.pop("parse_mode", None)
                        resp = await client.post(url, json=payload)
                    if resp.status_code == 200:
                        break
                    if attempt == 0:
                        await asyncio.sleep(1)
                except Exception as e:
                    if attempt == 0:
                        print(f"Telegram chunk {i+1}/{total} retrying: {e}")
                        await asyncio.sleep(1)
                    else:
                        print(f"Telegram chunk {i+1}/{total} failed after retry: {e}")
                        success = False
                        last_failed = i
    # Notify user if some chunks were lost
    if not success and last_failed >= 0 and last_failed < total - 1:
        try:
            note = f"⚠️ *Response incomplete* — part {last_failed+2}/{total} failed to send."
            async with httpx.AsyncClient() as client:
                await client.post(url, json={"chat_id": chat_id, "text": note, "parse_mode": "Markdown"})
        except Exception:
            pass
    return success


def get_gmail_service():
    creds = Credentials(
        None,
        refresh_token=os.getenv("GOOGLE_REFRESH_TOKEN"),
        client_id=os.getenv("GOOGLE_CLIENT_ID"),
        client_secret=os.getenv("GOOGLE_CLIENT_SECRET"),
        token_uri="https://oauth2.googleapis.com/token"
    )
    return build('gmail', 'v1', credentials=creds, cache=None)


async def send_draft_reply(draft_id: int) -> tuple:
    """Send an approved draft via Gmail or Outlook based on email source. Returns (success: bool, error: str|None)."""
    try:
        draft_res = supabase.table('email_drafts')\
            .select('id, email_id, draft_body, status, emails(sender_email, thread_id, source, subject, message_id)')\
            .eq('id', draft_id)\
            .eq('status', 'pending')\
            .maybe_single()\
            .execute()
        if not draft_res or not draft_res.data:
            return (False, "Draft not found or already processed.")
        draft = draft_res.data

        # Strip Subject line from draft_body if present (defensive fix for old drafts)
        body = draft.get('draft_body', '')
        if body.startswith('Subject:'):
            lines = body.split('\n')
            draft['draft_body'] = '\n'.join(lines[1:]).lstrip('\n')

        if not draft.get('emails'):
            return (False, "Associated email not found.")

        source = draft['emails'].get('source', 'gmail')

        if source == 'outlook':
            return await send_outlook_draft(draft)

        # Gmail send logic
        gmail_service = get_gmail_service()
        email = draft['emails']

        msg = MIMEText(draft['draft_body'])
        msg['To'] = email['sender_email']
        msg['From'] = os.getenv('GMAIL_SENDER_EMAIL', '')
        msg['Subject'] = f"Re: {email['subject']}"
        msg['In-Reply-To'] = email['thread_id']
        msg['References'] = email['thread_id']

        # Include original CC recipients (Reply All behavior)
        self_email = os.getenv('GMAIL_SENDER_EMAIL', '')
        try:
            original = gmail_service.users().messages().get(
                userId='me', id=email['message_id'],
                format='metadata', metadataHeaders=['Cc']
            ).execute()
            cc_headers = [
                h['value'] for h in original.get('payload', {}).get('headers', [])
                if h['name'].lower() == 'cc'
            ]
            if cc_headers:
                cc_addrs = [
                    a.strip() for a in cc_headers[0].split(',')
                    if a.strip() and self_email not in a
                ]
                if cc_addrs:
                    msg['Cc'] = ', '.join(cc_addrs)
        except Exception:
            pass  # Fall back to sender-only if original email unavailable

        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode('utf-8')
        send_body = {'raw': raw, 'threadId': email['thread_id']}

        # Update status to 'sent' BEFORE Gmail API call to prevent double-send
        supabase.table('email_drafts').update({'status': 'sent'}).eq('id', draft_id).execute()

        try:
            gmail_service.users().messages().send(userId='me', body=send_body).execute()
        except Exception as gmail_error:
            audit_log_sync("webhook", "ERROR", f"Gmail send failed for draft {draft_id}: {gmail_error}")
            print("Status remains 'sent' to prevent double-send attempts.")
            return (False, str(gmail_error))

        return (True, None)

    except Exception as e:
        audit_log_sync("webhook", "ERROR", f"Draft send error for draft {draft_id}: {e}")
        return (False, str(e))


async def send_outlook_draft(draft: dict) -> tuple:
    """Send an approved draft via Outlook Graph API. Returns (success: bool, error: str|None)."""
    try:
        email = draft['emails']
        to_email = email['sender_email']
        subject = email['subject']
        body = draft['draft_body']

        access_token = os.getenv("OUTLOOK_ACCESS_TOKEN")
        if not access_token:
            from core.skills.outlook_token_helper import refresh_outlook_token
            result = refresh_outlook_token(write_back=True)
            access_token = result["access_token"]

        # Include original CC recipients (Reply All behavior)
        cc_recipients = []
        try:
            async with httpx.AsyncClient(timeout=15) as cc_client:
                cc_resp = await cc_client.get(
                    f"https://graph.microsoft.com/v1.0/me/messages/{email['message_id']}",
                    headers={"Authorization": f"Bearer {access_token}"},
                    params={"$select": "ccRecipients"}
                )
                if cc_resp.status_code == 200:
                    cc_data = cc_resp.json()
                    for r in cc_data.get('ccRecipients', []):
                        addr = r.get('emailAddress', {}).get('address', '')
                        if addr and to_email not in addr:
                            cc_recipients.append({"emailAddress": {"address": addr}})
        except Exception:
            pass  # Fall back to sender-only if original email unavailable

        payload = {
            "message": {
                "subject": f"Re: {subject}",
                "body": {"contentType": "Text", "content": body},
                "toRecipients": [{"emailAddress": {"address": to_email}}],
                **({"ccRecipients": cc_recipients} if cc_recipients else {})
            },
            "saveToSentItems": True
        }

        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json"
        }

        # Update status to 'sent' BEFORE Outlook API call to prevent double-send
        supabase.table('email_drafts').update({'status': 'sent'}).eq('id', draft['id']).execute()

        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                "https://graph.microsoft.com/v1.0/me/sendMail",
                json=payload,
                headers=headers
            )

            if response.status_code == 202:
                return (True, None)

            if response.status_code == 401:
                from core.skills.outlook_token_helper import refresh_outlook_token
                result = refresh_outlook_token(write_back=True)
                access_token = result["access_token"]
                headers["Authorization"] = f"Bearer {access_token}"
                response = await client.post(
                    "https://graph.microsoft.com/v1.0/me/sendMail",
                    json=payload,
                    headers=headers
                )
                if response.status_code == 202:
                    return (True, None)

            print(f"Outlook send failed for draft {draft['id']}: HTTP {response.status_code}: {response.text}")
            print("Status remains 'sent' to prevent double-send attempts.")
            return (False, f"HTTP {response.status_code}: {response.text}")

    except Exception as e:
        audit_log_sync("webhook", "ERROR", f"Outlook send error for draft {draft['id']}: {e}")
        return (False, str(e))


async def handle_ed_command(text: str, chat_id: int):
    """Handle /ed, ed approve, ed reject, ed edit commands."""
    import re as _re

    # /ed — list pending drafts
    if text.strip() == '/ed':
        try:
            drafts_res = supabase.table('email_drafts')\
                .select('id, draft_body, status, email_id')\
                .eq('status', 'pending')\
                .order('created_at', desc=False)\
                .execute()
            drafts = drafts_res.data or []
            if not drafts:
                await send_telegram(chat_id, "✅ No pending drafts.")
                return

            email_ids = [d['email_id'] for d in drafts if d.get('email_id')]
            emails_map = {}
            if email_ids:
                emails_res = supabase.table('emails')\
                    .select('id, subject, sender_email, sender, source')\
                    .in_('id', email_ids)\
                    .execute()
                emails_map = {e['id']: e for e in (emails_res.data or [])}

            lines = ["📝 *Pending Draft(s)* — Review below:\n"]
            for d in drafts:
                email = emails_map.get(d.get('email_id'), {})
                sender = email.get('sender') or email.get('sender_email', '')
                email_addr = email.get('sender_email', '')
                subject = email.get('subject', '(No Subject)')
                body = d.get('draft_body', '')
                lines.append(
                    f"📝 *Draft {d['id']}* — Pending Approval\n"
                    f"📧 *To:* {sender} <{email_addr}>\n"
                    f"📌 *Re:* {subject}\n\n"
                    f"{body}\n\n"
                    f"Reply with:\n"
                    f"• `ed approve {d['id']}` — Send this draft\n"
                    f"• `ed reject {d['id']}` — Discard\n"
                    f"• `ed edit {d['id']} <new text>` — Replace and re-show\n"
                )
            await send_telegram(chat_id, "\n---\n".join(lines))
        except Exception as e:
            audit_log_sync("webhook", "ERROR", f"/ed list error: {e}")
            await send_telegram(chat_id, f"⚠️ Failed to fetch pending drafts: {e}")
        return

    # ed approve {id}
    approve_match = _re.match(r'^ed\s+approve\s+(\d+)$', text.strip(), _re.IGNORECASE)
    if approve_match:
        draft_id = int(approve_match.group(1))
        try:
            success, error = await send_draft_reply(draft_id)
            if success:
                draft_res = supabase.table('email_drafts')\
                    .select('email_id')\
                    .eq('id', draft_id)\
                    .maybe_single().execute()
                if draft_res and draft_res.data and draft_res.data.get('email_id'):
                    email_res = supabase.table('emails')\
                        .select('sender_email')\
                        .eq('id', draft_res.data['email_id'])\
                        .maybe_single().execute()
                    addr = email_res.data.get('sender_email', '') if email_res and email_res.data else ''
                else:
                    addr = ''
                await send_telegram(chat_id, f"✅ Draft [{draft_id}] sent to {addr}.")
            else:
                await send_telegram(chat_id, f"❌ Failed to send draft [{draft_id}]. Error: {error}")
        except Exception as e:
            audit_log_sync("webhook", "ERROR", f"ed approve error: {e}")
            await send_telegram(chat_id, f"❌ Failed to send draft [{draft_id}]. Error: {e}")
        return

    # ed reject {id}
    reject_match = _re.match(r'^ed\s+reject\s+(\d+)$', text.strip(), _re.IGNORECASE)
    if reject_match:
        draft_id = int(reject_match.group(1))
        try:
            res = supabase.table('email_drafts')\
                .update({'status': 'rejected'})\
                .eq('id', draft_id)\
                .eq('status', 'pending')\
                .execute()
            if res.data:
                await send_telegram(chat_id, f"🗑️ Draft [{draft_id}] rejected and discarded.")
            else:
                await send_telegram(chat_id, f"⚠️ Draft [{draft_id}] not found or already processed.")
        except Exception as e:
            audit_log_sync("webhook", "ERROR", f"ed reject error: {e}")
            await send_telegram(chat_id, f"⚠️ Failed to reject draft [{draft_id}]: {e}")
        return

    # ed edit {id} <new text>
    edit_match = _re.match(r'^ed\s+edit\s+(\d+)\s+(.+)$', text.strip(), _re.IGNORECASE | _re.DOTALL)
    if edit_match:
        draft_id = int(edit_match.group(1))
        new_body = edit_match.group(2).strip()
        try:
            upd = supabase.table('email_drafts')\
                .update({'draft_body': new_body})\
                .eq('id', draft_id)\
                .eq('status', 'pending')\
                .execute()
            if not upd.data:
                await send_telegram(chat_id, f"⚠️ Draft [{draft_id}] not found or already processed.")
                return

            draft_res = supabase.table('email_drafts')\
                .select('email_id')\
                .eq('id', draft_id)\
                .maybe_single().execute()
            if not draft_res or not draft_res.data or not draft_res.data.get('email_id'):
                await send_telegram(chat_id, f"✅ Draft [{draft_id}] updated.")
                return

            email_res = supabase.table('emails')\
                .select('subject, sender_email, sender')\
                .eq('id', draft_res.data['email_id'])\
                .maybe_single().execute()
            if not email_res or not email_res.data:
                await send_telegram(chat_id, f"✅ Draft [{draft_id}] updated.")
                return

            e = email_res.data
            await send_telegram(chat_id,
                f"📝 *Draft {draft_id}* — Pending Approval\n"
                f"📧 *To:* {e.get('sender') or e.get('sender_email', '')} <{e.get('sender_email', '')}>\n"
                f"📌 *Re:* {e.get('subject', '(No Subject)')}\n\n"
                f"{new_body}\n\n"
                f"Draft updated. Reply `ed approve {draft_id}` to send."
            )
        except Exception as e:
            audit_log_sync("webhook", "ERROR", f"ed edit error: {e}")
            await send_telegram(chat_id, f"⚠️ Failed to edit draft [{draft_id}]: {e}")
        return

    await send_telegram(chat_id, "⚠️ Unknown /ed command. Use: `/ed`, `ed approve {id}`, `ed reject {id}`, `ed edit {id} <text>`")


KEYBOARD = {
    "keyboard": [
        [{"text": "🔴 Urgent"}, {"text": "📋 Brief"}],
        [{"text": "🚀 Mission"}, {"text": "📚 Library"}],
        [{"text": "🧭 Season Context"}, {"text": "🔓 Vault"}],
        [{"text": "📊 Status"}]
    ],
    "resize_keyboard": True,
    "persistent": True
}

async def process_webhook(update: dict):
    try:
        update_id = update.get('update_id')
        # Skip deduplication for web UI messages (update_id is a string like "web_123")
        # Only check for Telegram updates (numeric update_ids)
        if update_id and isinstance(update_id, (int, float)):
            try:
                supabase.table('processed_updates').insert({"update_id": int(update_id)}).execute()
                # Cleanup: delete update IDs older than 72 hours
                try:
                    cutoff = (datetime.now(timezone.utc) - timedelta(hours=72)).isoformat()
                    supabase.table('processed_updates').delete().lt('processed_at', cutoff).execute()
                except Exception as cleanup_e:
                    audit_log_sync("webhook", "WARNING", f"⚠️ Dedup cleanup failed (non-critical): {cleanup_e}")
            except Exception as e:
                error_msg = str(e)
                if "23505" in error_msg or "already exists" in error_msg.lower():
                    print(f"♻️ Telegram retry detected for update {update_id}. Skipping.")
                    return {"success": True, "message": "Already processed"}
                else:
                    audit_log_sync("webhook", "WARNING", f"⚠️ Deduplication check error: {error_msg}")
                    pass

        ist_offset = timezone(timedelta(hours=5, minutes=30))
        now = datetime.now(ist_offset)

        # 🚀 ADD THIS: JOURNAL SYNC HANDLER (Cloud Signal)
        intent_signal = update.get('intent')
        auth_secret = update.get('auth_secret')

        if intent_signal == 'JOURNAL_SYNC':
            # Verify the secret matches your .env PULSE_SECRET
            if auth_secret != os.getenv("PULSE_SECRET"):
                print("⛔ Unauthorized Journal Sync attempt.")
                return {"status": "unauthorized", "message": "Invalid Secret"}
            
            print("📂 JOURNAL_SYNC signal received from Google Sheets.")
            # Trigger the GitHub Action which now runs Ingest -> Backfill -> Briefing
            triggered = await trigger_github_pulse()
            
            if triggered:
                # Optional: Send a notification to your Telegram so you know it's working
                owner_id = os.getenv("TELEGRAM_CHAT_ID")
                if owner_id:
                    await send_telegram(owner_id, "📂 *Journal signal received.* Synchronizing archive and re-wiring graph...")
                return {"success": True, "message": "Sync pipeline triggered"}
            else:
                return {"success": False, "message": "GitHub trigger failed"}

        # --- Standard Telegram Logic continues below ---
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
            print(f"⛔ Unauthorized access from Chat ID: {chat_id}")
            return {"message": "Unauthorized"}

        if not text:
            photo = message.get('photo')
            voice = message.get('voice')
            audio = message.get('audio')
            document = message.get('document')
            
            if photo:
                file_id = photo[-1].get('file_id')
                await send_telegram(chat_id, "🖼️ Processing image...")
                file_bytes, mime = await download_telegram_file(file_id)
                await process_multimodal_content(file_bytes, mime, chat_id, ist_hour=now.hour, core_json=core_json)
                return {"success": True}
            
            elif voice or audio:
                file_id = voice.get('file_id') or audio.get('file_id')
                await send_telegram(chat_id, "🎙️ Processing audio...")
                file_bytes, mime = await download_telegram_file(file_id)
                await process_multimodal_content(file_bytes, mime, chat_id, ist_hour=now.hour, core_json=core_json)
                return {"success": True}
            
            elif document:
                file_id = document.get('file_id')
                mime = document.get('mime_type', '')
                
                if mime in ['application/pdf', 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'] or mime.startswith('text/'):
                    await send_telegram(chat_id, "📄 Processing document...")
                    file_bytes, mime = await download_telegram_file(file_id)
                    await process_multimodal_content(file_bytes, mime, chat_id, ist_hour=now.hour, core_json=core_json)
                    return {"success": True}
                else:
                    await send_telegram(chat_id, "⚠️ Unsupported file type. Send as PDF, DOCX, or text.")
                    return {"success": True}
            
            await send_telegram(chat_id, "⚠️ I can only process text, images, audio, and documents.")
            return {"success": True}
        
        # Reject extremely long text before it hits Gemini
        MAX_TEXT_LENGTH = 10000
        if len(text) > MAX_TEXT_LENGTH:
            await send_telegram(chat_id, f"⚠️ Message too long ({len(text)} chars). Please send shorter messages (max {MAX_TEXT_LENGTH} chars).")
            return {"success": True}
        
        # 📨 SHORTCODE REPLY HANDLER — must run before classify_intent()
        import re as _re
        _approve_match = _re.match(r'^(\d+)\s+(yes|approve|do it|yep|add it)$', text.strip(), _re.IGNORECASE)
        _reject_match = _re.match(r'^(\d+)\s+(drop|no|reject|skip|dismiss)$', text.strip(), _re.IGNORECASE)
        
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

                # 🏃 Practice dismissal via shortcode — email not found + "drop"
                if not _is_approve and result['action'] == 'not_found':
                    try:
                        _node_res = supabase.table('graph_nodes') \
                            .select('id, label, metadata') \
                            .eq('id', int(_shortcode)) \
                            .eq('type', 'practice') \
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
                            await send_telegram(chat_id, f"🗑️ Dismissed: {_n.get('label', '')}")
                            print(f"📍 SHORTCODE DROP: Dismissed practice '{_n.get('label', '')}' via shortcode.")
                            return {"success": True}
                    except Exception as _sc_practice_err:
                        audit_log_sync("webhook", "WARNING", f"Shortcode practice fallback error: {_sc_practice_err}")

                # Standard failure handling
                await send_telegram(chat_id, f"⚠️ {result['message']}")
                if result['action'] in ('staging_failed',):
                    raise Exception(result['message'])

                return {"success": True}

            except Exception as _sc_err:
                audit_log_sync("webhook", "WARNING", f"Shortcode handler error: {_sc_err}")
                await send_telegram(chat_id, "⚠️ Something went wrong. Try again or use /ep to retry.")
                return {"success": True}

        # 📝 /ed DRAFT APPROVAL HANDLER — must run before classify_intent()
        if text.strip().startswith('ed '):
            await handle_ed_command(text, chat_id)
            return {"success": True}
        
        # Initialize conversation session early so shortcuts share the same session_id as the main flow
        session_id, history = get_or_create_session(chat_id)

        # 📋 /today prefix — on-demand daily briefing, skip classify_intent()
        if text.strip().lower() in ('/today', '/brief', '/day'):
            history_text = format_history_for_prompt(history)
            log_exchange(session_id, 'user', 'DAILY_BRIEF', text, chat_id)
            await handle_daily_brief(text, chat_id, session_id=session_id, conversation_history=history_text)
            return {"success": True}

        # ? prefix shortcut — handle as QUERY directly, skip classify_intent()
        if text.startswith('?'):
            query = text[1:].strip()
            if query:
                history_text = format_history_for_prompt(history)
                log_exchange(session_id, 'user', 'QUERY', text, chat_id)
                await interrogate_brain(query, chat_id, session_id=session_id, conversation_history=history_text)
                return {"success": True}

        # 🏃 /drop-{practice} HANDLER — dismiss a practice permanently
        import re as _re_drop
        _drop_match = _re_drop.match(r'^/drop-(.+)$', text.strip(), _re_drop.IGNORECASE)
        if _drop_match:
            practice_name = _drop_match.group(1).strip().replace('-', ' ')
            try:
                # Find practice node by label (case-insensitive)
                node_res = supabase.table('graph_nodes') \
                    .select('id, label, metadata') \
                    .eq('type', 'practice') \
                    .ilike('label', practice_name) \
                    .limit(1) \
                    .execute()
                if not node_res.data:
                    await send_telegram(chat_id, f"⚠️ No practice found matching '{practice_name}'.")
                    return {"success": True}

                node = node_res.data[0]
                raw_meta = node.get('metadata', {})
                if isinstance(raw_meta, str):
                    try:
                        raw_meta = json.loads(raw_meta)
                    except:
                        raw_meta = {}

                # Mark as dismissed
                raw_meta['status'] = 'dismissed'
                raw_meta['dismissed_at'] = datetime.now(timezone(timedelta(hours=5, minutes=30))).strftime('%Y-%m-%d')

                supabase.table('graph_nodes') \
                    .update({'metadata': raw_meta}) \
                    .eq('id', node['id']) \
                    .execute()

                # Add variants to global exclusion list
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
                await send_telegram(chat_id, f"🗑️ Dismissed: {label}")
                print(f"📍 DROP: Dismissed practice '{label}' — {len(new_entries)} variants excluded.")

            except Exception as _drop_err:
                audit_log_sync("webhook", "WARNING", f"/drop error: {_drop_err}")
                await send_telegram(chat_id, "⚠️ Failed to dismiss practice. Try again.")
            return {"success": True}

        history_text = format_history_for_prompt(history)

        context = await get_recent_context(limit=2)
        classification = await classify_intent(text, context, ist_hour=now.hour, core_json=core_json, conversation_history=history_text)
        
        intent = classification.get('intent', 'TASK')
        confidence = classification.get('confidence', 0.5)
        
        print(f"🎯 Intent: {intent} ({confidence:.0%}) - {text[:50]}...")

        # Log user message to conversation history
        log_exchange(session_id, 'user', intent, text, chat_id)
        
        # Detect if message is from web UI (fake update_id from send-message endpoint)
        is_web_source = update.get('update_id') and str(update.get('update_id')).startswith('web_')
        source = "web" if is_web_source else "telegram"
        sender = "user"  # All user messages (web/telegram) have sender "user"
        
        # Note: User message will be saved by classification functions (handle_confident_task/note)
        # with the correct message_type (task/note) and status (pending for Pulse)
        # No need to save as "chat" separately - avoids duplicates
        
        if text.startswith('/') or text in ['🔴 Urgent', '📋 Brief', '🧭 Season Context', '🔓 Vault', '📚 Library', '📊 Status']:
            return await handle_command(text, chat_id)
        
        if text.startswith('N:') or text.startswith('Note:'):
            note_content = text[2:].strip() if text.startswith('N:') else text[5:].strip()
            if note_content:
                receipt = "Note vaulted."
                await handle_confident_note(note_content, chat_id, receipt, source=source)
            return {"success": True}
        
        # 🔙 UNDO SUBCOMMAND HANDLER — undo n, undo t, undo d
        if _re.match(r'^undo\s+(n(?:ote)?|t(?:ask)?|d(?:elete)?)\s*$', text.strip(), _re.IGNORECASE):
            return await handle_undo_command(text, chat_id)
        
        receipt = classification.get('receipt')
        
        CONFIDENCE_HIGH = 0.8
        CONFIDENCE_LOW = 0.5
        possible_intents = classification.get('possible_intents', [])

        # Check if we're responding to a pending disambiguation
        try:
            last_history = get_history(session_id, max_tokens=1)
            if last_history:
                last_bot = last_history[-1].get('bot', {})
                if last_bot.get('intent') == 'CLARIFICATION':
                    meta = json.loads(last_bot.get('content', '{}'))
                    if isinstance(meta, dict):
                        if meta.get('confirmation') == 'task_or_note':
                            if await resolve_task_note_confirmation(text, chat_id, session_id, meta):
                                return {"success": True}
                        elif meta.get('confirmation') == 'task_update':
                            if await resolve_task_update_confirmation(text, chat_id, session_id, meta):
                                return {"success": True}
                        elif meta.get('possible_intents'):
                            if await resolve_disambiguation(text, chat_id, session_id, meta):
                                return {"success": True}
        except Exception:
            pass

        # --- OPPORTUNITY LANGUAGE CONFIRMATION ---
        if intent == 'TASK' and confidence >= CONFIDENCE_HIGH and detect_opportunity_language(text):
            print(f"🧐 Opportunity language detected — asking confirmation for: {text[:50]}...")
            await ask_task_or_note_confirmation(text, classification, chat_id, session_id)
            return {"success": True}

        # --- TASK UPDATE DISAMBIGUATION ---
        if intent == 'TASK' and confidence >= CONFIDENCE_HIGH:
            first_word = text.strip().lower().split()[0] if text.strip() else ''
            if first_word in UPDATE_TRIGGER_WORDS:
                matched = check_task_overlap_for_update(text)
                if matched:
                    print(f"🔄 Task update overlap detected — asking: {text[:50]}...")
                    await ask_task_update_confirmation(text, classification, chat_id, session_id, matched)
                    return {"success": True}

        if confidence >= CONFIDENCE_HIGH:
            await route_by_intent(intent, text, chat_id, session_id, classification=classification, source=source, sender=sender)
        elif possible_intents and len(possible_intents) >= 2 and confidence >= CONFIDENCE_LOW:
            print(f"🧐 Ambiguous ({possible_intents}) — asking user")
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
                await send_telegram(chat_id, "⚠️ *Something went wrong.*\n\n_Try again or report this._")
        except Exception:
            pass
        return {"error": str(e), "status": 500}


async def handle_practices_command(chat_id: int):
    """Query and display all practice nodes grouped by status."""
    try:
        practices_res = supabase.table('graph_nodes') \
            .select('id, label, metadata') \
            .eq('type', 'practice') \
            .execute()
        all_practices = practices_res.data or []

        if not all_practices:
            await send_telegram(chat_id, "🏃 No practices tracked yet.")
            return

        active = []
        drifting = []
        dormant = []
        inactive = []

        for p in all_practices:
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

            label = p.get('label', '')
            status = meta.get('status', 'active')
            health_score = meta.get('health_score', 50)
            occurrence_count = meta.get('occurrence_count', 0)

            if health_score >= 80:
                trend = "✓"
            elif health_score >= 50:
                trend = "→"
            else:
                trend = "↓"

            is_drifting = status == 'active' and health_score < 50

            entry = {
                'label': label,
                'health_score': health_score,
                'trend': trend,
                'occurrence_count': occurrence_count,
                'status': status
            }

            if status == 'dormant':
                dormant.append(entry)
            elif status == 'inactive':
                inactive.append(entry)
            elif is_drifting:
                drifting.append(entry)
            else:
                active.append(entry)

        active.sort(key=lambda x: x['health_score'], reverse=True)
        drifting.sort(key=lambda x: x['health_score'])
        dormant.sort(key=lambda x: x['occurrence_count'], reverse=True)

        lines = ["🏃 *PRACTICES*\n"]

        if active:
            lines.append(f"━ Active ({len(active)}) ━")
            for e in active:
                bar_len = e['health_score'] // 10
                bar = "█" * bar_len + "░" * (10 - bar_len)
                lines.append(f"{e['label']:20s} {bar} {e['health_score']:3d}%  {e['trend']}")

        if drifting:
            lines.append("")
            lines.append(f"━ Drifting ({len(drifting)}) ━")
            for e in drifting:
                bar_len = e['health_score'] // 10
                bar = "█" * bar_len + "░" * (10 - bar_len)
                lines.append(f"{e['label']:20s} {bar} {e['health_score']:3d}%  {e['trend']} ↓")

        if dormant:
            lines.append("")
            lines.append(f"━ Dormant ({len(dormant)}) ━")
            for e in dormant:
                lines.append(f"⏸️ {e['label']} — {e['occurrence_count']} occurrences")

        if inactive:
            lines.append("")
            lines.append(f"━ Inactive ({len(inactive)}) ━")
            for e in inactive:
                lines.append(f"💤 {e['label']}")

        total = len(all_practices)
        active_count = len(active)
        avg_health = sum(e['health_score'] for e in active) // max(len(active), 1) if active else 0
        lines.append(f"\n_{total} total · {active_count} active · Avg health {avg_health}%_")

        await send_telegram(chat_id, "\n".join(lines))

    except Exception as e:
        audit_log_sync("webhook", "ERROR", f"/practices error: {e}")
        await send_telegram(chat_id, f"⚠️ Practices query failed: {e}")


async def handle_status_command(chat_id: int):
    """Pure DB snapshot. No LLM. No Pulse trigger."""
    try:
        ist_offset = timezone(timedelta(hours=5, minutes=30))
        now = datetime.now(ist_offset)
        stale_cutoff = (now - timedelta(days=7)).isoformat()

        # Urgent tasks
        urgent_res = supabase.table('tasks')\
            .select('id', count='exact')\
            .eq('priority', 'urgent')\
            .in_('status', ['todo', 'in_progress'])\
            .execute()
        urgent_count = urgent_res.count or 0

        # Important tasks
        important_res = supabase.table('tasks')\
            .select('id', count='exact')\
            .eq('priority', 'important')\
            .in_('status', ['todo', 'in_progress'])\
            .execute()
        important_count = important_res.count or 0

        # Stale tasks (no update in 7+ days, still open)
        stale_res = supabase.table('tasks')\
            .select('id', count='exact')\
            .in_('status', ['todo', 'in_progress'])\
            .lt('updated_at', stale_cutoff)\
            .execute()
        stale_count = stale_res.count or 0

        # Pending email decisions
        pending_email_res = supabase.table('email_pending_tasks')\
            .select('id', count='exact')\
            .is_('danny_decision', 'null')\
            .execute()
        pending_email_count = pending_email_res.count or 0

        # Pending drafts
        pending_drafts_res = supabase.table('email_drafts')\
            .select('id', count='exact')\
            .eq('status', 'pending')\
            .execute()
        pending_drafts_count = pending_drafts_res.count or 0

        # Unprocessed raw dumps
        raw_dumps_res = supabase.table('raw_dumps')\
            .select('id', count='exact')\
            .in_('status', ['pending', 'staged'])\
            .execute()
        raw_dumps_count = raw_dumps_res.count or 0

        # Agent queue (pending research tasks)
        agent_res = supabase.table('agent_queue')\
            .select('id', count='exact')\
            .eq('status', 'pending')\
            .execute()
        agent_count = agent_res.count or 0

        lines = ["*BOARD STATUS*\n"]

        lines.append(f"🔴 Urgent: {urgent_count} task{'s' if urgent_count != 1 else ''}")
        lines.append(f"🟡 Important: {important_count} task{'s' if important_count != 1 else ''}")

        stale_flag = " ⚠️" if stale_count >= 3 else ""
        lines.append(f"⏳ Stale (7d+): {stale_count} task{'s' if stale_count != 1 else ''}{stale_flag}")

        lines.append(f"\n📨 Pending email decisions: {pending_email_count}")
        lines.append(f"📝 Pending drafts: {pending_drafts_count}")

        if raw_dumps_count > 0:
            lines.append(f"📥 Unprocessed captures: {raw_dumps_count}")
        if agent_count > 0:
            lines.append(f"🕵️ Research queue: {agent_count}")

        timestamp = now.strftime("%d %b, %I:%M %p")
        lines.append(f"\n_As of {timestamp} IST_")

        await send_telegram(chat_id, "\n".join(lines))

    except Exception as e:
        audit_log_sync("webhook", "ERROR", f"/status error: {e}")
        await send_telegram(chat_id, f"⚠️ Status check failed: {e}")


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


async def handle_undo_command(text: str, chat_id: int):
    import re as _re

    # Bare /undo → show most recent entry
    if text.strip() == '/undo':
        try:
            recent = supabase.table('raw_dumps') \
                .select('id, content, message_type, status, created_at') \
                .eq('direction', 'incoming') \
                .eq('sender', 'user') \
                .not_.in_('message_type', ['acknowledgment', 'briefing', 'response', 'clarification']) \
                .order('created_at', desc=True) \
                .limit(1) \
                .maybe_single() \
                .execute()

            if not recent or not recent.data:
                await send_telegram(chat_id, "Nothing to undo.")
                return {"success": True}

            r = recent.data
            content = r.get('content', '')
            msg_type = r.get('message_type', 'unknown')
            status = r.get('status', 'unknown')

            lines = [
                f"🧐 *Last entry:*",
                f"\n_{content[:200]}..._",
                f"\n📌 Type: `{msg_type}` · Status: `{status}`",
                f"\n`undo n` — Flip to note",
                f"`undo t` — Flip to task",
                f"`undo d` — Delete",
            ]
            await send_telegram(chat_id, "\n".join(lines))
            return {"success": True}
        except Exception as e:
            audit_log_sync("webhook", "ERROR", f"/undo fetch error: {e}")
            await send_telegram(chat_id, f"⚠️ Failed to fetch last entry: {e}")
            return {"success": True}

    # Parse subcommands
    undo_n = _re.match(r'^undo\s+n(?:ote)?\s*$', text.strip(), _re.IGNORECASE)
    undo_t = _re.match(r'^undo\s+t(?:ask)?\s*$', text.strip(), _re.IGNORECASE)
    undo_d = _re.match(r'^undo\s+d(?:elete)?\s*$', text.strip(), _re.IGNORECASE)

    if not (undo_n or undo_t or undo_d):
        await send_telegram(chat_id, "Usage: `/undo` to see last entry, `undo n`, `undo t`, or `undo d` to act.")
        return {"success": True}

    # Fetch the most recent entry
    try:
        recent = supabase.table('raw_dumps') \
            .select('id, content, message_type, status') \
            .eq('direction', 'incoming') \
            .eq('sender', 'user') \
            .not_.in_('message_type', ['acknowledgment', 'briefing', 'response', 'clarification']) \
            .order('created_at', desc=True) \
            .limit(1) \
            .maybe_single() \
            .execute()

        if not recent or not recent.data:
            await send_telegram(chat_id, "Nothing to undo.")
            return {"success": True}

        r = recent.data
        dump_id = r['id']
        content = r.get('content', '')
        current_type = r.get('message_type', '')
        current_status = r.get('status', '')

        if undo_d:
            supabase.table('raw_dumps').update({
                "status": "cancelled",
                "is_processed": True,
            }).eq('id', dump_id).execute()
            # Best-effort cancel any task Pulse may have created
            try:
                supabase.table('tasks').update({"status": "cancelled"}) \
                    .ilike('title', content[:100]) \
                    .in_('status', ['todo', 'in_progress']) \
                    .execute()
            except Exception:
                pass
            await send_telegram(chat_id, f"🗑️ Deleted: _{content[:80]}..._")
            return {"success": True}

        if undo_n:
            supabase.table('raw_dumps').update({
                "message_type": "note",
                "status": "staged",
            }).eq('id', dump_id).execute()
            # Process as note inline
            embedding = await asyncio.to_thread(get_embedding, content)
            if embedding and any(embedding):
                try:
                    supabase.table('memories').insert({
                        "content": content,
                        "memory_type": "note",
                        "embedding": embedding,
                        "embedding_status": "success",
                        "source": "webhook_undo"
                    }).execute()
                    supabase.table('raw_dumps').update({
                        "status": "processed",
                        "is_processed": True,
                    }).eq('id', dump_id).execute()
                except Exception:
                    pass
            # Best-effort cancel any task Pulse may have created
            try:
                supabase.table('tasks').update({"status": "cancelled"}) \
                    .ilike('title', content[:100]) \
                    .in_('status', ['todo', 'in_progress']) \
                    .execute()
            except Exception:
                pass
            await send_telegram(chat_id, f"📝 Flipped to note: _{content[:80]}..._")
            return {"success": True}

        if undo_t:
            supabase.table('raw_dumps').update({
                "message_type": "task",
                "status": "pending",
            }).eq('id', dump_id).execute()
            # If it was in memories, remove it
            try:
                supabase.table('memories').delete() \
                    .eq('content', content) \
                    .eq('source', 'webhook_undo') \
                    .execute()
            except Exception:
                pass
            await send_telegram(chat_id, f"📋 Flipped to task: _{content[:80]}..._")
            return {"success": True}

    except Exception as e:
        audit_log_sync("webhook", "ERROR", f"Undo action error: {e}")
        await send_telegram(chat_id, f"⚠️ Undo failed: {e}")
        return {"success": True}


async def handle_command(text: str, chat_id: int):
    reply = ""
    
    if text.startswith('/mission') or text == '🚀 Mission':
        params = text.replace('/mission', '').replace('🚀 Mission', '').strip()
        if not params:
            m_res = supabase.table('graph_nodes').select('label').eq('type', 'mission').execute()
            active_missions = [m for m in (m_res.data or []) if json.loads(m.get('metadata', '{}')).get('status') == 'active']
            if active_missions:
                m_list = "\n".join([f"• {m['label']}" for m in active_missions])
                reply = f"🚀 **ACTIVE MISSIONS:**\n\n{m_list}\n\n_To start a new one, type /mission [Goal]_"
            else:
                reply = "🚀 No active missions. Type `/mission [Goal]` to start hunting."
        else:
            try:
                existing_mission = (
                    supabase.table('graph_nodes')
                    .select('id')
                    .eq('type', 'mission')
                    .ilike('label', params)
                    .maybe_single()
                    .execute()
                )
                if existing_mission.data:
                    reply = f"⚠️ Mission '{params}' already exists. Type `/mission [different goal]` to start a new one."
                else:
                    supabase.table('graph_nodes').insert({
                        "label": params,
                        "type": "mission",
                        "metadata": {"status": "active", "origin": "webhook_command"}
                    }).execute()
                    reply = f"🚀 **MISSION DECLARED:** {params}\n\nI am now hunting for components and 'Sparks' related to this goal."
            except Exception as e:
                reply = f"❌ Error: {str(e)}"

    elif text in ['/library', '📚 Library']:
        lib_res = supabase.table('resources').select('title, url, category').order('created_at', desc=True).limit(10).execute()
        items = lib_res.data or []
        if items:
            formatted = [f"🔖 **[{i.get('title') or 'Untitled'}]({i.get('url')})**" for i in items]
            reply = f"📚 **RESOURCE LIBRARY (Last 10):**\n\n" + "\n\n".join(formatted)
        else:
            reply = "The library is empty. Save some links first!"

    elif text in ['/vault', '🔓 Vault']:
        vault_url = "https://danny-integrated-os.streamlit.app"
        reply = f"🔓 **COMMAND CENTER ONLINE**\n\nYour strategic overview and research library are live.\n\n👉 [Access Secure Vault]({vault_url})"

    elif text.startswith('/season') or text == '🧭 Season Context':
        params = text.replace('/season', '').replace('🧭 Season Context', '').strip()
        if not params:
            season_res = supabase.table('core_config').select('content').eq('key', 'current_season').limit(1).execute()
            if season_res.data:
                reply = f"🧭 **CURRENT NORTH STAR:**\n\n{season_res.data[0]['content']}"
            else:
                reply = "⚠️ No Season Context found. Set one using `/season text...`"
        else:
            if len(params) < 10:
                reply = "❌ **Error:** Definition too short."
            else:
                try:
                    supabase.table('core_config').update({"content": params}).eq('key', 'current_season').execute()
                    reply = "✅ **Season Updated.**\nTarget Locked."
                except:
                    reply = "❌ Database Error"

    elif text in ['/urgent', '🔴 Urgent']:
        now_ist = datetime.now(timezone(timedelta(hours=5, minutes=30)))
        now_iso = now_ist.strftime('%Y-%m-%dT%H:%M:%S+05:30')
        fire_res = supabase.table('tasks').select('*').eq('priority', 'urgent').eq('status', 'todo').eq('is_current', True).or_(f"reminder_at.is.null,reminder_at.lte.{now_iso}").limit(1).execute()
        if fire_res.data:
            fire = fire_res.data[0]
            reply = f"🔴 **ACTION REQUIRED:**\n\n🔥 {fire.get('title')}\n⏱️ Est: {fire.get('estimated_minutes')} mins"
        else:
            reply = "✅ No active fires. You are strategic."

    elif text in ['/brief', '📋 Brief']:
        triggered = await trigger_github_pulse()
        if triggered:
            reply = "Understood. Offloading heavy intel to the remote server. Sit tight, the SITREP will arrive in about 60 seconds."
        else:
            reply = "⚠️ Could not trigger remote briefing. Try again or check system config."

    elif text in ['/status', '📊 Status']:
        await handle_status_command(chat_id)
        return {"success": True}

    elif text in ['/practices', '🏃 Practices']:
        await handle_practices_command(chat_id)
        return {"success": True}

    elif text in ['/ep']:
        try:
            pending = supabase.table('email_pending_tasks')\
                .select('id, suggested_title, suggested_project')\
                .is_('danny_decision', 'null')\
                .order('created_at', desc=False)\
                .limit(10)\
                .execute()
            if pending.data:
                lines = [f"📨 Pending email tasks ({len(pending.data)}):"]
                for row in pending.data:
                    project = row.get('suggested_project') or 'Unknown'
                    lines.append(f"[{row['id']}] {row['suggested_title'][:60]} — {project}")
                lines.append('"[id] yes" to approve · "[id] drop" to reject')
                reply = "\n\n".join(lines)
            else:
                reply = "✅ No pending email decisions. Inbox is clean."
        except Exception as ep_err:
            reply = f"⚠️ Error fetching pending emails: {ep_err}"
        await send_telegram(chat_id, reply)
        return {"success": True}

    elif text.startswith('/ed'):
        await handle_ed_command(text, chat_id)
        return {"success": True}

    elif text in ['/undo']:
        return await handle_undo_command(text, chat_id)

    else:
        await send_telegram(chat_id, "⚠️ Unknown command. Type /help or tap the menu to see available commands.")

    await send_telegram(chat_id, reply)
    return {"success": True}
