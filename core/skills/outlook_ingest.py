import os
import sys
import json
import asyncio
import re
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from core.constants import EmailStatus

load_dotenv()
load_dotenv('.env.local')
from supabase import create_client, Client
from google import genai
import requests

supabase: Client = create_client(
    os.getenv("SUPABASE_URL"),
    os.getenv("SUPABASE_SERVICE_ROLE_KEY")
)
gemini_client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))

EMBEDDING_MODEL = "gemini-embedding-2-preview"
EMBEDDING_DIMENSION = 768

RETRYABLE_ERRORS = ['503', '504', '500', 'disconnected', 'timeout', 'deadline exceeded', 'unavailable', 'overloaded', 'rate limit']
NOREPLY_PATTERNS = ['noreply', 'no-reply', 'donotreply', 'mailer-daemon', 'bounce', 'notifications@', 'automated@']

BASE_DIR = Path(__file__).resolve().parents[2]
ENV_LOCAL = BASE_DIR / ".env.local"

def get_access_token():
    access_token = os.getenv("OUTLOOK_ACCESS_TOKEN")
    if access_token:
        return access_token
    from core.skills.outlook_token_helper import refresh_outlook_token
    result = refresh_outlook_token(write_back=True)
    return result["access_token"]

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
                print(f"API Hiccup ({error_str}), retrying in {delay}s...")
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
        print(f"Draft generation failed: {e}")
        return ""

async def classify_email(sender: str, subject: str, body: str) -> dict:
    prompt = f"""Danny is a founder-operator. He uses this Outlook mailbox for work (Crayon, client work, Solvstrat, Product Labs, business vendors, team). Classify this email strictly. He receives emails in India (Chennai). His name is Danny or Yashwant Daniel.

Sender: {sender}
Subject: {subject}
Body:
{body[:800]}

Classify as one of:
- ignored: newsletters, automated alerts, no-reply senders, bulk mail, OTP/verification codes, promotional offers, forwards with FW: prefix, mailing list mail
- fyi: Danny is CC'd or BCC'd, or the email is informational and requires zero action from him
- actionable: directly addressed to Danny, requires a response, decision, or creates an obligation (work email bias: vendor/client/team responses, approvals, scheduling, task follow-ups are actionable)

Return ONLY valid JSON:
{{"classification": "ignored|fyi|actionable", "summary": "2 sentences max, plain English", "suggested_task": "one line or null", "needs_draft": true/false, "linked_person_name": "name or null", "linked_project_name": "project name or null"}}"""

    try:
        response = await call_gemini_with_retry(prompt, model="gemini-3.1-flash-lite-preview")
        return parse_json_response(response.text)
    except Exception as e:
        print(f"Classification failed: {e}")
        return {"classification": "ignored", "summary": "Classification failed", "suggested_task": None, "needs_draft": False, "linked_person_name": None, "linked_project_name": None}

def fetch_outlook_messages(limit=25):
    access_token = get_access_token()
    headers = {"Authorization": f"Bearer {access_token}"}

    url = "https://graph.microsoft.com/v1.0/me/mailFolders/inbox/messages"
    params = {
        "$top": limit,
        "$select": "id,subject,receivedDateTime,from,bodyPreview,conversationId,isRead,hasAttachments,internetMessageId",
        "$orderby": "receivedDateTime DESC"
    }

    response = requests.get(url, headers=headers, params=params, timeout=30)

    if response.status_code == 401:
        from core.skills.outlook_token_helper import refresh_outlook_token
        result = refresh_outlook_token(write_back=True)
        access_token = result["access_token"]
        headers["Authorization"] = f"Bearer {access_token}"
        response = requests.get(url, headers=headers, params=params, timeout=30)

    response.raise_for_status()
    messages = response.json().get("value", [])
    print(f"fetched {len(messages)} outlook messages")
    return messages

def normalize_outlook_message(msg):
    from_field = msg.get("from", {})
    email_address = from_field.get("emailAddress", {})
    sender_email = email_address.get("address", "unknown")
    sender_name = email_address.get("name") or sender_email

    return {
        "source": "outlook",
        "message_id": msg.get("id"),
        "internet_message_id": msg.get("internetMessageId"),
        "thread_id": msg.get("conversationId", ""),
        "sender_email": sender_email,
        "sender": sender_name,
        "subject": msg.get("subject") or "(No Subject)",
        "body_summary": msg.get("bodyPreview", ""),
        "received_at": msg.get("receivedDateTime"),
        "is_read": msg.get("isRead", False),
        "has_attachments": msg.get("hasAttachments", False),
    }

async def ingest_outlook_messages(limit=25):
    messages = fetch_outlook_messages(limit=limit)
    if not messages:
        print("No new Outlook messages found.")
        return {"processed": 0, "ignored": 0, "skipped": 0}

    processed = 0
    ignored = 0
    skipped = 0
    seen_ids = set()

    for msg in messages:
        msg_id = msg.get("id")
        if not msg_id:
            continue
        if msg_id in seen_ids:
            print(f"Duplicate msg_id in batch: {msg_id}, skipping")
            skipped += 1
            continue
        seen_ids.add(msg_id)

        try:
            existing = supabase.table('emails').select('id').eq('message_id', msg_id).maybe_single().execute()
            if existing is not None and existing.data:
                skipped += 1
                continue
        except Exception as e:
            print(f"Dedup check failed for {msg_id}: {e}")

        normalized = normalize_outlook_message(msg)
        sender = normalized["sender"]
        sender_email = normalized["sender_email"]
        subject = normalized["subject"]
        body = normalized["body_summary"]

        if any(p in sender_email.lower() for p in NOREPLY_PATTERNS):
            classification_data = {"classification": "ignored", "summary": "No-reply sender", "suggested_task": None, "needs_draft": False, "linked_person_name": None, "linked_project_name": None}
        else:
            classification_data = await classify_email(sender, subject, body)

        classification = classification_data.get("classification", "ignored")

        if classification == "ignored":
            supabase.table('emails').insert({
                "message_id": msg_id,
                "thread_id": normalized["thread_id"],
                "source": "outlook",
                "sender": sender,
                "sender_email": sender_email,
                "subject": subject,
                "received_at": normalized["received_at"],
                "classification": EmailStatus.IGNORED,
                "status": EmailStatus.IGNORED
            }).execute()
            print(f"[ignored] {subject} | From: {sender_email}")
            ignored += 1
            continue

        email_row = {
            "message_id": msg_id,
            "thread_id": normalized["thread_id"],
            "source": "outlook",
            "sender": sender,
            "sender_email": sender_email,
            "subject": subject,
            "body_summary": body[:500],
            "received_at": normalized["received_at"],
            "classification": classification,
            "status": EmailStatus.NEW if classification == "actionable" else EmailStatus.PROCESSED,
            "linked_person_id": None,
            "linked_project_id": None
        }

        if classification == "fyi":
            insert_res = supabase.table('emails').insert(email_row).execute()
            print(f"[fyi] {subject} | From: {sender_email}")
            processed += 1

        elif classification == "actionable":
            linked_person_id = None
            linked_person_name = classification_data.get("linked_person_name")
            if linked_person_name:
                person_res = supabase.table('people').select('id, name').ilike('name', f'%{linked_person_name}%').maybe_single().execute()
                if person_res.data:
                    linked_person_id = person_res.data['id']

            linked_project_id = None
            linked_project_name = classification_data.get("linked_project_name")
            if linked_project_name:
                project_res = supabase.table('projects').select('id, name').ilike('name', f'%{linked_project_name}%').maybe_single().execute()
                if project_res.data:
                    linked_project_id = project_res.data['id']

            email_row['linked_person_id'] = linked_person_id
            email_row['linked_project_id'] = linked_project_id

            insert_res = supabase.table('emails').insert(email_row).execute()
            if not insert_res.data:
                print(f"Email insert returned no data for {subject}")
                continue
            email_id = insert_res.data[0]['id']

            suggested_task = classification_data.get("suggested_task")
            if suggested_task:
                supabase.table('email_pending_tasks').insert({
                    "email_id": email_id,
                    "suggested_title": suggested_task,
                    "suggested_project": classification_data.get("linked_project_name"),
                    "shown_in_brief": False,
                    "danny_decision": None
                }).execute()

            if classification_data.get("needs_draft"):
                draft_body = await generate_draft(sender, subject, body)
                if draft_body:
                    supabase.table('email_drafts').insert({
                        "email_id": email_id,
                        "draft_body": draft_body,
                        "status": "pending"
                    }).execute()

            if suggested_task or classification_data.get("needs_draft"):
                memory_content = f"Email from {sender} ({sender_email}): {classification_data.get('summary', '')}"
                embedding = await asyncio.to_thread(get_embedding, memory_content)
                supabase.table('memories').insert({
                    "content": memory_content,
                    "memory_type": "email_actioned",
                    "embedding": embedding
                }).execute()

            print(f"[actionable] {subject} | From: {sender_email}")
            processed += 1

    print(f"Outlook ingest complete. {processed} processed, {ignored} ignored, {skipped} skipped (duplicates).")
    return {"processed": processed, "ignored": ignored, "skipped": skipped}

async def main():
    print(f"Outlook ingest started at {datetime.now(timezone(timedelta(hours=5, minutes=30)))}")
    result = await ingest_outlook_messages(limit=25)
    print(f"Result: {result}")

if __name__ == "__main__":
    asyncio.run(main())
