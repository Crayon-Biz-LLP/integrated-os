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

supabase: Client = create_client(
    os.getenv("SUPABASE_URL"), 
    os.getenv("SUPABASE_SERVICE_ROLE_KEY")
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
            versioned_update('email_pending_tasks', row['id'], {'danny_decision': 'skipped'})
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

        versioned_update('email_pending_tasks', row['id'], {'danny_decision': 'approved'})
        print(f"Staged to raw_dumps via email approval: {title}")
        return {"success": True, "action": "approved", "message": f"Task staged: {title}"}

    elif decision == 'reject':
        versioned_update('email_pending_tasks', row['id'], {'danny_decision': 'rejected'})
        try:
            versioned_update('email_drafts', row['id'], {'danny_decision': 'skipped'})
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


async def classify_intent(text: str, context: list, ist_hour: int = None, core_json: str = "[]") -> dict:
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

    Message: "{text}"{context_str}
    CURRENT TIME CONTEXT: It's the {time_phase}.
    IDENTITY & BUSINESS CONTEXT: {core_json}

    Return ONLY valid JSON (no markdown, no explanation):
    {{
        "intent": "TASK|NOTE|NOISE|CLARIFICATION_NEEDED|DELEGATE|QUERY|DECLARE_PRACTICE",
        "confidence": 0.0-1.0,
        "entity": "SOLVSTRAT|QHORD|PERSONAL|CHURCH|INBOX",
        "title": "extracted task title",
        "time_context": "time info if any",
        "clarification_question": "question if needed",
        "receipt": "Stealth status report (no entity names).",
        "reasoning": "brief logic"
    }}

    Rules:
    - STRICT TITLE FIDELITY: The title field must be a literal extraction of the task as spoken. NEVER add project names, infer entities, or change Danny's wording (e.g., if he says "this OS," do NOT change it to "Qhord OS").
    - PROJECT ROUTING: Route tasks about personal finances, bills, home, or family to PERSONAL. Only route to CRAYON if it relates to corporate governance, business taxes, or legal compliance. Route tech/client work to SOLVSTRAT.
    - STATUS vs TASK: If a message describes something that HAS HAPPENED (e.g., 'Lead generated', 'Meeting finished', 'Sent the file'), classify it as a NOTE. A TASK must imply an OUTSTANDING action for Danny to perform (e.g., 'Call the lead', 'Prepare the ERP plan'). If it's a win or a milestone, it's a NOTE for the Historian.
    - TASK: Any message that implies an action. Do not require a date or time.
    - NOTE: Ideas, insights, or learnings worth remembering.
    - QUERY: The user is asking a question to retrieve information from their past notes, tasks, or the vault (e.g., "What did the analyst say?", "When is my meeting?").
    - DELEGATE: Research, competitor audits, or autonomous web research.
    - DECLARE_PRACTICE: If Danny says "I want to [activity] every [timeframe]", "I'm going to start [activity]", "Track [activity] for me", "I want to build a practice of [activity]", or expresses intent to establish a recurring behavior — classify as DECLARE_PRACTICE. Extract the practice name into the title field. Route to the most relevant entity (PERSONAL for health/personal routines, SOLVSTRAT for work practices, etc.).
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


# 1. Update your handle_confident_task signature to accept entity
async def handle_confident_task(text: str, title: str, time_context: str, chat_id: int, receipt: str = None, entity: str = None, source: str = "telegram", sender: str = "user"):
    # ── Idempotency guard: skip if identical content+source inserted within 60s ──
    if is_recent_raw_dump(text, source):
        ack = receipt or "Logged."
        await send_telegram(chat_id, f"{ack}")
        return

    try:
        supabase.table('raw_dumps').insert([{
            "content": text,
            "status": "pending",
            "direction": "incoming",
            "sender": sender,  # "user" for all user messages
            "message_type": "task",
            "source": source,
            "metadata": {
                "intent": "TASK",
                "title": title, 
                "time_context": time_context,
                "entity": entity
            }
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


async def handle_clarification(text: str, question: str, chat_id: int, receipt: str = None):
    ack = receipt or "Copy that. I need one more detail to log this."
    reply = f"{ack}\n\n{question}\n\n_Context: \"{text[:100]}...\"_"
    await send_telegram(chat_id, reply)
    
    supabase.table('raw_dumps').insert([{
        "content": text,
        "direction": "incoming",
        "sender": "telegram",
        "message_type": "clarification",
        "metadata": {"awaiting_clarification": True}
    }]).execute()


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


async def interrogate_brain(query: str, chat_id: int):
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
            resources_res = supabase.table('resources').select('title, url, category, content').execute()
            resources = resources_res.data or []
        except:
            resources = []
        
        all_context = []
        
        if tactical_map:
            all_context.append(f"TACTICAL MAP:\n{tactical_map}")
        
        for item in combined_results:
            source = item.get('source', 'memory').upper()
            content = item.get('content', '')
            link = item.get('link', '')
            all_context.append(f"[{source}] {content}" + (f" | Link: {link}" if link else ""))
        
        for r in resources[:3]:
            title = r.get('title', 'Untitled')
            url = r.get('url', '')
            category = r.get('category', 'resource')
            content = r.get('content', title)
            all_context.append(f"[{category.upper()}] {content}" + (f" | Link: {url}" if url else ""))
        
        if not all_context:
            await send_telegram(chat_id, "🔍 *No relevant memories found.*\n\n_Try a different query._")
            return
        
        context_str = "\n\n".join(all_context)
        
        prompt = f"""You are Danny's Rhodey. Danny is asking about a node in your network. Use the TACTICAL MAP to identify dependencies and potential bottlenecks. Give a direct, logic-based answer. If you don't know the answer, say so. Cite the source if possible.

{context_str}

Question: {query}

Provide a clear, concise answer. Format with Markdown. If referencing a specific memory, cite it like [MEMORY] or [RESOURCE]."""
        
        response = await call_gemini_with_retry(prompt=prompt, model=CLASSIFICATION_MODEL)
        
        answer = response.text.strip()
        
        await send_telegram(chat_id, f"🧠 *Brain Interrogation:*\n\n{answer}")
        
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


async def send_telegram(chat_id: int, message_text: str, show_keyboard: bool = True):
    telegram_bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
    url = f"https://api.telegram.org/bot{telegram_bot_token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": message_text,
        "parse_mode": "Markdown"
    }
    if show_keyboard:
        payload["reply_markup"] = {
            "keyboard": [
                [{"text": "🔴 Urgent"}, {"text": "📋 Brief"}],
                [{"text": "🚀 Mission"}, {"text": "📚 Library"}],
                [{"text": "🧭 Season Context"}, {"text": "🔓 Vault"}],
                [{"text": "📊 Status"}]
            ],
            "resize_keyboard": True,
            "persistent": True
        }
        payload["disable_web_page_preview"] = True
    
    try:
        async with httpx.AsyncClient() as client:
            await client.post(url, json=payload)
        return True
    except Exception as e:
        print(f"Telegram send failed to {chat_id}: {e}")
        return False


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
            .select('id, email_id, draft_body, status, emails(sender_email, thread_id, source, subject)')\
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

        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode('utf-8')
        send_body = {'raw': raw, 'threadId': email['thread_id']}

        # Update status to 'sent' BEFORE Gmail API call to prevent double-send
        # Use versioned update for email_drafts
        versioned_update('email_drafts', draft_id, {'status': 'sent'})

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

        payload = {
            "message": {
                "subject": f"Re: {subject}",
                "body": {"contentType": "Text", "content": body},
                "toRecipients": [{"emailAddress": {"address": to_email}}]
            },
            "saveToSentItems": True
        }

        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json"
        }

        # Update status to 'sent' BEFORE Outlook API call to prevent double-send
        # Use versioned update for email_drafts
        versioned_update('email_drafts', draft['id'], {'status': 'sent'})

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
                cutoff = (datetime.now(timezone.utc) - timedelta(hours=72)).isoformat()
                supabase.table('processed_updates').delete().lt('created_at', cutoff).execute()
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
        
        # ? prefix shortcut — handle as QUERY directly, skip classify_intent()
        if text.startswith('?'):
            query = text[1:].strip()
            if query:
                await interrogate_brain(query, chat_id)
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

        context = await get_recent_context(limit=2)
        classification = await classify_intent(text, context, ist_hour=now.hour, core_json=core_json)
        
        intent = classification.get('intent', 'TASK')
        confidence = classification.get('confidence', 0.5)
        
        print(f"🎯 Intent: {intent} ({confidence:.0%}) - {text[:50]}...")
        
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
        
        receipt = classification.get('receipt')
        
        if intent == 'TASK' and confidence >= 0.6:
            print(f"📋 WORK LOGGED: {text[:80]}...")
            await handle_confident_task(
                text,
                classification.get('title', text),
                classification.get('time_context', ''),
                chat_id,
                receipt,
                entity=classification.get('entity'),
                source=source,
                sender=sender  # Pass the sender ("user" for all user messages)
            )
        elif intent == 'QUERY' and confidence >= 0.6:
            print(f"🧠 QUERY DETECTED: Routing to brain...")
            await interrogate_brain(text, chat_id)
        elif intent == 'NOTE' and confidence >= 0.6:
            if text.startswith('http') or 'www.' in text:
                supabase.table('resources').insert({
                    "url": text,
                    "title": classification.get('title', 'New Resource'),
                    "category": classification.get('entity', 'INBOX')
                }).execute()
                await send_telegram(chat_id, "🔖 Resource saved to Library.")
            else:
                # Use handle_confident_note which saves to raw_dumps (for Pulse) and memories (with embedding)
                await handle_confident_note(
                    text, 
                    chat_id, 
                    receipt or "Note secured.", 
                    source=source, 
                    sender=sender
                )
        elif intent == 'DELEGATE':
            supabase.table('agent_queue').insert({
                "query": text,
                "status": "pending"
            }).execute()
            ack = receipt or "The intern is on it. I'll ping you when the research is ready."
            await send_telegram(chat_id, f"✓ {ack}")
        elif intent == 'DECLARE_PRACTICE' and confidence >= 0.6:
            print(f"🏃 PRACTICE DECLARED: {classification.get('title', text)}")
            await handle_declare_practice(text, chat_id, classification)
        elif intent == 'NOISE':
            await handle_noise(chat_id)
        else:
            await handle_clarification(
                text,
                classification.get('clarification_question', 'Could you provide more details?'),
                chat_id,
                receipt
            )

        return {"success": True}

    except Exception as e:
        audit_log_sync("webhook", "ERROR", f"Webhook Error: {e}")
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

    else:
        await send_telegram(chat_id, "⚠️ Unknown command. Type /help or tap the menu to see available commands.")

    await send_telegram(chat_id, reply)
    return {"success": True}
