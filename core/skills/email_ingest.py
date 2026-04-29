import os
import sys
import json
import asyncio
import base64
import re
from email.utils import parsedate_to_datetime
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from core.constants import EmailStatus

load_dotenv()
load_dotenv('.env.local')
from supabase import create_client, Client
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.discovery_cache import base
from google import genai

supabase: Client = create_client(
    os.getenv("SUPABASE_URL"),
    os.getenv("SUPABASE_SERVICE_ROLE_KEY")
)

def build_active_task_keyword_set() -> set:
    """Fetch all active task titles ONCE and extract keywords into a set."""
    try:
        result = supabase.table('tasks')\
            .select('title')\
            .not_.in_('status', ['done', 'cancelled'])\
            .execute()
        keywords = set()
        for row in (result.data or []):
            title = row.get('title', '')
            for word in title.lower().split():
                if len(word) > 4:
                    keywords.add(word)
        return keywords
    except Exception as e:
        print(f"⚠️ Failed to build task keyword set (failing open): {e}")
        return set()


def is_duplicate_task(title: str, active_keywords: set) -> bool:
    """In-memory dedup check. Zero DB calls."""
    if not active_keywords:
        return False
    words = [w for w in title.lower().split() if len(w) > 4]
    for kw in words[:3]:
        if kw in active_keywords:
            print(f"⚠️  Duplicate guard: '{title}' matches existing task (keyword: '{kw}'). Dropping.")
            return True
    return False

gemini_client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))

EMBEDDING_MODEL = "gemini-embedding-2-preview"
EMBEDDING_DIMENSION = 768

RETRYABLE_ERRORS = ['503', '504', '500', 'disconnected', 'timeout', 'deadline exceeded', 'unavailable', 'overloaded', 'rate limit']
NOREPLY_PATTERNS = ['noreply', 'no-reply', 'donotreply', 'mailer-daemon', 'bounce', 'notifications@', 'automated@']


class MemoryCache(base.Cache):
    _cache = {}

    def get(self, url):
        return self._cache.get(url)

    def set(self, url, content):
        self._cache[url] = content


def get_google_creds():
    return Credentials(
        None,
        refresh_token=os.getenv("GOOGLE_REFRESH_TOKEN"),
        client_id=os.getenv("GOOGLE_CLIENT_ID"),
        client_secret=os.getenv("GOOGLE_CLIENT_SECRET"),
        token_uri="https://oauth2.googleapis.com/token"
    )


def get_embedding(text: str) -> list:
    try:
        result = gemini_client.models.embed_content(
            model=EMBEDDING_MODEL,
            contents=text,
            config={
                'output_dimensionality': EMBEDDING_DIMENSION
            }
        )
        return result.embeddings[0].values
    except Exception as e:
        print(f"Embedding error: {e}")
        return [0] * EMBEDDING_DIMENSION


async def call_gemini_with_retry(prompt: str, model: str):
    max_retries = 3
    base_delay = 5

    for attempt in range(max_retries):
        try:
            response = gemini_client.models.generate_content(
                model=model,
                contents=prompt
            )
            return response
        except Exception as e:
            error_str = str(e).lower()
            should_retry = any(err in error_str for err in RETRYABLE_ERRORS)
            if should_retry and attempt < max_retries - 1:
                delay = base_delay * (2 ** attempt)
                print(f"⚠️ API Hiccup ({error_str}), retrying in {delay}s...")
                await asyncio.sleep(delay)
                continue
            else:
                raise


def parse_json_response(response_text: str) -> any:
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


async def generate_draft(sender: str, subject: str, body: str) -> str:
    prompt = f"""You are drafting a professional reply on behalf of Danny (Yashwant Daniel), founder of Crayon. Write a concise, warm, and direct reply to this email. Do not sign off with a full signature block — end with just 'Danny'. Do not send — this is a draft for Danny's review.

Sender: {sender}
Subject: {subject}
Body:
{body[:1000]}"""

    try:
        response = await call_gemini_with_retry(prompt, model="gemini-3.1-flash-lite-preview")
        return response.text.strip()
    except Exception as e:
        print(f"⚠️ Draft generation failed: {e}")
        return ""


def extract_email_address(sender_header: str) -> tuple:
    match = re.search(r'<(.+?)>', sender_header)
    if match:
        return sender_header.replace(match.group(0), '').strip().strip('"'), match.group(1)
    return sender_header.strip(), sender_header.strip()


def decode_body(payload: dict) -> str:
    body = ""
    if 'parts' in payload:
        for part in payload['parts']:
            if part.get('mimeType') == 'text/plain':
                data = part.get('body', {}).get('data', '')
                if data:
                    body += base64.urlsafe_b64decode(data + '=' * (-len(data) % 4)).decode('utf-8', errors='ignore')
            elif 'parts' in part:
                body += decode_body(part)
    else:
        data = payload.get('body', {}).get('data', '')
        if data:
            body += base64.urlsafe_b64decode(data + '=' * (-len(data) % 4)).decode('utf-8', errors='ignore')
    return body


def decode_html_body(payload: dict) -> str:
    if 'parts' in payload:
        for part in payload['parts']:
            if part.get('mimeType') == 'text/html':
                data = part.get('body', {}).get('data', '')
                if data:
                    return base64.urlsafe_b64decode(data + '=' * (-len(data) % 4)).decode('utf-8', errors='ignore')
            elif 'parts' in part:
                result = decode_html_body(part)
                if result:
                    return result
    else:
        if payload.get('mimeType') == 'text/html':
            data = payload.get('body', {}).get('data', '')
            if data:
                return base64.urlsafe_b64decode(data + '=' * (-len(data) % 4)).decode('utf-8', errors='ignore')
    return ""


async def classify_email(sender: str, subject: str, body: str, to_header: str = '', cc_header: str = '') -> dict:
    prompt = f"""Danny is a founder-operator. Classify this email strictly. He receives emails in India (Chennai). His name is Danny or Yashwant Daniel.

Sender: {sender}
To: {to_header}
CC: {cc_header}
Subject: {subject}
Body:
{body[:800]}

Classify as one of:
- ignored: newsletters, automated alerts, no-reply senders, bulk mail, OTP/verification codes, promotional offers, forwards with FW: prefix, mailing list mail
- fyi: Danny is CC'd or BCC'd, or the email is informational and requires zero action from him
- actionable: directly addressed to Danny (To: field), requires a response, decision, or creates an obligation

Return ONLY valid JSON:
{{"classification": "ignored|fyi|actionable", "summary": "2 sentences max, plain English", "suggested_task": "one line or null", "needs_draft": true/false, "linked_person_name": "name or null", "linked_project_name": "project name or null"}}"""

    try:
        response = await call_gemini_with_retry(prompt, model="gemini-3.1-flash-lite-preview")
        return parse_json_response(response.text)
    except Exception as e:
        print(f"⚠️ Classification failed: {e}")
        return {"classification": "ignored", "summary": "Classification failed", "suggested_task": None, "needs_draft": False, "linked_person_name": None, "linked_project_name": None}


async def process_email(msg_data: dict, gmail_service, active_task_keywords: set) -> tuple:
    msg_id = msg_data['id']
    sender_name = None
    sender_email = None
    subject = None

    try:
        existing = supabase.table('emails').select('id').eq('message_id', msg_id).maybe_single().execute()
        if existing is not None and existing.data:
            return (EmailStatus.IGNORED, msg_data.get('snippet', '')[:50])
    except Exception as e:
        print(f"⚠️ Dedup check failed for {msg_id}: {e}")

    try:
        full_msg = gmail_service.users().messages().get(userId='me', id=msg_id, format='full').execute()
        payload = full_msg.get('payload', {})
        headers = {h['name'].lower(): h['value'] for h in payload.get('headers', [])}

        sender_header = headers.get('from', '')
        sender_name, sender_email = extract_email_address(sender_header)
        subject = headers.get('subject', '(No Subject)')
        to_header = headers.get('to', '')
        cc_header = headers.get('cc', '')
        received_at_raw = headers.get('date', '')
        try:
            received_at = parsedate_to_datetime(received_at_raw).isoformat()
        except Exception:
            received_at = datetime.now(timezone.utc).isoformat()

        body = decode_body(payload)[:1500]
        if not body.strip():
            html_body = decode_html_body(payload)
            body = re.sub(r'<[^>]+>', ' ', html_body).strip()[:1500]

        if any(p in sender_email.lower() for p in NOREPLY_PATTERNS):
            classification_data = {"classification": "ignored", "summary": "No-reply sender", "suggested_task": None, "needs_draft": False, "linked_person_name": None, "linked_project_name": None}
        else:
            classification_data = await classify_email(sender_header, subject, body, to_header, cc_header)
        classification = classification_data.get('classification', 'ignored')

        if classification == 'ignored':
            supabase.table('emails').insert({
                "message_id": msg_id,
                "thread_id": full_msg.get('threadId', ''),
                "source": "gmail",
                "sender": sender_name,
                "sender_email": sender_email,
                "subject": subject,
                "received_at": received_at,
                "classification": EmailStatus.IGNORED,
                "status": EmailStatus.IGNORED
            }).execute()
            print(f"⏭️ [ignored] {subject} | From: {sender_email}")
            return (EmailStatus.IGNORED, subject)

        email_row = {
            "message_id": msg_id,
            "thread_id": full_msg.get('threadId', ''),
            "source": "gmail",
            "sender": sender_name,
            "sender_email": sender_email,
            "subject": subject,
            "body_summary": body[:500],
            "received_at": received_at,
            "classification": classification,
            "status": EmailStatus.NEW if classification == "actionable" else EmailStatus.PROCESSED,
            "linked_person_id": None,
            "linked_project_id": None
        }

        if classification == 'fyi':
            insert_res = supabase.table('emails').insert(email_row).execute()
            email_id = insert_res.data[0]['id']
            print(f"✅ [fyi] {subject} | From: {sender_email}")

        elif classification == 'actionable':
            linked_person_id = None
            linked_person_name = classification_data.get('linked_person_name')
            if linked_person_name:
                person_res = supabase.table('people').select('id, name').ilike('name', f'%{linked_person_name}%').maybe_single().execute()
                if person_res.data:
                    linked_person_id = person_res.data['id']

            linked_project_id = None
            linked_project_name = classification_data.get('linked_project_name')
            if linked_project_name:
                project_res = supabase.table('projects').select('id, name').ilike('name', f'%{linked_project_name}%').maybe_single().execute()
                if project_res.data:
                    linked_project_id = project_res.data['id']

            email_row['linked_person_id'] = linked_person_id
            email_row['linked_project_id'] = linked_project_id

            insert_res = supabase.table('emails').insert(email_row).execute()
            if not insert_res.data:
                print(f"⚠️ Email insert returned no data for {subject}")
                return ('error', 'insert returned no data')
            email_id = insert_res.data[0]['id']

            suggested_task = classification_data.get('suggested_task')
            if suggested_task:
                suggested_title = suggested_task or ''
                if not is_duplicate_task(suggested_title, active_task_keywords):
                    supabase.table('email_pending_tasks').insert({
                        "email_id": email_id,
                        "suggested_title": suggested_task,
                        "suggested_project": classification_data.get('linked_project_name'),
                        "shown_in_brief": False,
                        "danny_decision": None
                    }).execute()

            if classification_data.get('needs_draft'):
                draft_body = await generate_draft(sender_name, subject, body)
                if draft_body:
                    supabase.table('email_drafts').insert({
                        "email_id": email_id,
                        "draft_body": draft_body,
                        "status": "pending"
                    }).execute()

            if suggested_task or classification_data.get('needs_draft'):
                memory_content = f"Email from {sender_name} ({sender_email}): {classification_data.get('summary', '')}"
                embedding = await asyncio.to_thread(get_embedding, memory_content)
                supabase.table('memories').insert({
                    "content": memory_content,
                    "memory_type": "email_actioned",
                    "embedding": embedding
                }).execute()

            print(f"✅ [actionable] {subject} | From: {sender_email}")

        return (classification, subject)

    except Exception as e:
        print(f"❌ Error processing email {msg_id}: {e}")
        try:
            supabase.table('emails').insert({
                "message_id": msg_id,
                "source": "gmail",
                "sender": sender_name or "unknown",
                "sender_email": sender_email or "unknown",
                "classification": EmailStatus.ERROR,
                "status": EmailStatus.ERROR,
                "subject": subject or "processing_error",
                "received_at": datetime.now(timezone.utc).isoformat()
            }).execute()
        except Exception as insert_err:
            print(f"⚠️ Failed to insert error record: {insert_err}")
        return (EmailStatus.ERROR, str(e))


async def main():
    now_ist = datetime.now(timezone(timedelta(hours=5, minutes=30)))
    print("📧 Email ingest started at " + str(now_ist))

    gmail_service = build('gmail', 'v1', credentials=get_google_creds(), cache=MemoryCache())

    active_task_keywords = build_active_task_keyword_set()
    print(f"🧠 Loaded {len(active_task_keywords)} active task keywords for dedup.")

    cutoff = datetime.now(timezone.utc) - timedelta(hours=48)
    after_timestamp = int(cutoff.timestamp())
    query = f'(label:inbox OR label:"Completed/Ashraya") after:{after_timestamp}'
    result = gmail_service.users().messages().list(userId='me', q=query, maxResults=50).execute()
    messages = result.get('messages', [])

    if not messages:
        print("📭 No new emails found.")
        print("Email ingest complete. 0 processed, 0 ignored, 0 skipped (duplicates).")
        return

    print(f"📬 Found {len(messages)} emails to process.")

    processed = 0
    ignored = 0
    skipped = 0
    results = []
    seen_ids = set()

    for msg in messages:
        if not msg:
            print("⚠️ Skipping None message data")
            continue
        msg_id = msg.get('id')
        if msg_id in seen_ids:
            print(f"⚠️ Duplicate msg_id in batch: {msg_id}, skipping")
            skipped += 1
            continue
        seen_ids.add(msg_id)
        try:
            status, detail = await process_email(msg, gmail_service, active_task_keywords)
            if status == 'skipped':
                skipped += 1
            elif status == EmailStatus.IGNORED:
                ignored += 1
            elif status == EmailStatus.ERROR:
                processed += 1
            else:
                processed += 1
            results.append((status, detail))
        except Exception as e:
            print(f"❌ Fatal error processing message: {e}")

    print(f"Email ingest complete. {processed} processed, {ignored} ignored, {skipped} skipped (duplicates).")


if __name__ == "__main__":
    asyncio.run(main())
