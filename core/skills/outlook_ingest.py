from core.llm.constants import CLASSIFICATION_MODEL
import os
import json
import asyncio
import re
from pathlib import Path
from datetime import datetime, timedelta, timezone

from core.lib.constants import EmailStatus
from core.lib.duplicate_guard import check_duplicate
from core.lib.time_utils import compute_expires_at
from core.services.db import get_supabase, maybe_single_safe
from core.services.llm import call_gemini_classify
import requests

supabase = get_supabase()

def build_active_task_list() -> list:
    """Fetch all active task titles ONCE for duplicate checking."""
    try:
        result = supabase.table('tasks')\
            .select('id, title')\
            .eq('is_current', True)\
            .not_.in_('status', ['done', 'cancelled'])\
            .execute()
        return result.data or []
    except Exception as e:
        print(f"⚠️ Failed to build active task list (failing open): {e}")
        return []

def fetch_rejected_email_tasks() -> list:
    try:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        result = supabase.table('messages')\
            .select('id, suggested_title')\
            .eq('channel', 'email')\
            .eq('danny_decision', 'rejected')\
            .gte('created_at', cutoff)\
            .execute()
        return [{"id": r['id'], "title": r['suggested_title']} for r in (result.data or []) if r.get('suggested_title')]
    except Exception as e:
        print(f"Failed to build rejected task list: {e}")
        return []

NOREPLY_PATTERNS = [
    'noreply', 'no-reply', 'donotreply', 'mailer-daemon',
    'bounce', 'notifications@', 'automated@',
    # Government portals and financial utilities
    'nesl.co.in', 'incometax.gov', 'gst.gov', 'mca.gov',
    'estatement@', 'alerts@', 'statement@', 'update@',
    'do-not-reply', 'donotreply'
]

BASE_DIR = Path(__file__).resolve().parents[2]
ENV_LOCAL = BASE_DIR / ".env.local"

def get_access_token():
    access_token = os.getenv("OUTLOOK_ACCESS_TOKEN")
    if access_token:
        return access_token
    from core.skills.outlook_token_helper import refresh_outlook_token
    result = refresh_outlook_token(write_back=True)
    return result["access_token"]

async def call_gemini_with_retry(prompt: str, model: str, config: dict = None):
    return await call_gemini_classify(prompt, model, config)


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
        response = await call_gemini_with_retry(prompt, model=CLASSIFICATION_MODEL)
        text = response.text.strip()
        if text and '"reasoning": "safe_hold"' in text:
            print(f"Draft generation returned safe_hold fallback for [{subject}]")
            return ""
        return text
    except Exception as e:
        print(f"Draft generation failed: {e}")
        return ""




async def classify_email(sender: str, subject: str, body: str, to_header: str = '', cc_header: str = '') -> dict:
    from core.prompts.email_classify import build_email_classify_prompt
    prompt = build_email_classify_prompt(
        mailbox_type="work",
        sender=sender,
        subject=subject,
        body=body[:1000],
        to_header=to_header,
        cc_header=cc_header,
    )
    response = await call_gemini_with_retry(
        prompt,
        model=CLASSIFICATION_MODEL,
        config={"response_mime_type": "application/json"}
    )
    return json.loads(response.text)


def fetch_outlook_sent_messages(limit=25):
    access_token = get_access_token()
    headers = {"Authorization": f"Bearer {access_token}"}

    url = "https://graph.microsoft.com/v1.0/me/mailFolders/sentItems/messages"
    params = {
        "$top": limit,
        "$select": "id,subject,sentDateTime,toRecipients,bodyPreview,body,conversationId,internetMessageId",
        "$orderby": "sentDateTime DESC"
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
    print(f"fetched {len(messages)} outlook sent messages")
    return messages

def fetch_outlook_messages(limit=25):
    access_token = get_access_token()
    headers = {"Authorization": f"Bearer {access_token}"}

    url = "https://graph.microsoft.com/v1.0/me/mailFolders/inbox/messages"
    params = {
        "$top": limit,
        "$select": "id,subject,receivedDateTime,from,bodyPreview,body,conversationId,isRead,hasAttachments,internetMessageId,toRecipients,ccRecipients",
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

    import re
    to_recipients = msg.get("toRecipients", [])
    cc_recipients = msg.get("ccRecipients", [])
    to_header = ", ".join(r.get("emailAddress", {}).get("address", "") for r in to_recipients if r.get("emailAddress", {}).get("address"))
    cc_header = ", ".join(r.get("emailAddress", {}).get("address", "") for r in cc_recipients if r.get("emailAddress", {}).get("address"))

    body_data = msg.get("body", {})
    body_content = body_data.get("content", "")
    if body_data.get("contentType", "").lower() == "html":
        # simple html tag strip
        body_content = re.sub(r'<[^>]+>', ' ', body_content).strip()
    else:
        body_content = body_content.strip()

    body_preview = msg.get("bodyPreview", "")
    if len(body_content) > 50:
        body_preview = body_content

    return {
        "source": "outlook",
        "message_id": msg.get("id"),
        "internet_message_id": msg.get("internetMessageId"),
        "thread_id": msg.get("conversationId", ""),
        "sender_email": sender_email,
        "sender": sender_name,
        "subject": msg.get("subject") or "(No Subject)",
        "body_summary": body_preview[:2000],
        "body_raw": body_content[:20000],
        "received_at": msg.get("receivedDateTime"),
        "is_read": msg.get("isRead", False),
        "has_attachments": msg.get("hasAttachments", False),
        "to_header": to_header,
        "cc_header": cc_header,
    }

async def ingest_outlook_messages(limit=25):
    messages = fetch_outlook_messages(limit=limit)
    if not messages:
        print("No new Outlook messages found.")
        return {"processed": 0, "ignored": 0, "skipped": 0}

    active_task_list = build_active_task_list()
    rejected_task_list = fetch_rejected_email_tasks()
    print(f"🧠 Loaded {len(active_task_list)} active tasks and {len(rejected_task_list)} rejected tasks for duplicate checking.")

    processed = 0
    ignored = 0
    skipped = 0
    skipped_api_error = 0
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
            existing = maybe_single_safe(supabase.table('messages').select('id').eq('channel', 'email').eq('message_id', msg_id))
            if existing is not None and getattr(existing, 'data', None):
                skipped += 1
                continue
        except Exception as e:
            print(f"Dedup check failed for {msg_id}: {e}")

        # Initialize variables for error handler
        normalized = None
        sender = "unknown"
        sender_email = "unknown"
        subject = "processing_error"
        body = ""
        to_header = ""
        cc_header = ""

        try:
            normalized = normalize_outlook_message(msg)
            sender = normalized["sender"]
            sender_email = normalized["sender_email"]
            subject = normalized["subject"]
            body = normalized["body_summary"]
            if not body or len(body.strip()) < 10:
                body = f"[No body preview available — classify based on subject only. Subject: {subject}]"
            to_header = normalized.get("to_header", "")
            cc_header = normalized.get("cc_header", "")
            if any(p in sender_email.lower() for p in NOREPLY_PATTERNS):
                classification_data = {"classification": "ignored", "summary": "No-reply sender", "suggested_task": None, "needs_draft": False, "linked_person_name": None, "linked_project_name": None}
            else:
                try:
                    classification_data = await classify_email(sender, subject, body, to_header, cc_header)
                except Exception:
                    print(f"⏭️ [skipped - classification error] {subject} | Will retry on next run")
                    skipped_api_error += 1
                    continue

            classification = classification_data.get("classification", "ignored")

            if classification == "ignored":
                supabase.table('messages').insert({
                    "channel": "email",
                    "message_id": msg_id,
                    "thread_id": normalized["thread_id"],
                    "source": "outlook",
                    "sender_name": sender,
                    "sender_id": sender_email,
                    "subject": subject,
                    "received_at": normalized["received_at"],
                    "classification": "ignored",
                    "processing_status": "completed",
                    "danny_decision": "skipped",
                    "expires_at": compute_expires_at(f"{subject} {body}", normalized["received_at"])
                }).execute()
                print(f"⏭️ [ignored] {subject} | From: {sender_email}")
                ignored += 1
                continue

            email_row = {
                "channel": "email",
                "message_id": msg_id,
                "thread_id": normalized["thread_id"],
                "source": "outlook",
                "sender_name": sender,
                "sender_id": sender_email,
                "subject": subject,
                "body": normalized.get("body_raw", "")[:20000],
                "received_at": normalized["received_at"],
                "classification": classification,
                "processing_status": "completed" if classification != "error" else "failed",
                "linked_person_id": None,
                "linked_project_id": None,
                "metadata": {
                    "body_summary": body[:2000]
                },
                "expires_at": compute_expires_at(f"{subject} {body}", normalized["received_at"])
            }

            if classification == "fyi":
                from core.lib.ingest import ingest
                await ingest(
                    text=classification_data.get('summary', '') or subject,
                    source='outlook',
                    classification='fyi',
                    summary=classification_data.get('summary', '')[:1000],
                    is_human_sender=classification_data.get('is_human_sender', False),
                    has_memory_value=classification_data.get('has_memory_value', False),
                    channel_specific_data={
                        "sender_name": sender,
                        "sender_email": sender_email,
                        "subject": subject,
                        "to_header": to_header,
                        "cc_header": cc_header,
                        "body_raw": body,
                    },
                    tracking_id=msg_id,
                    received_at=normalized.get("received_at"),
                    body=normalized.get("body_raw", "")[:20000],
                )
                print(f"✅ [fyi] {subject} | From: {sender_email}")
                processed += 1

            elif classification == "actionable":
                linked_person_id = None
                linked_person_name = classification_data.get("linked_person_name")
                if linked_person_name:
                    person_res = maybe_single_safe(supabase.table('people').select('id, name').ilike('name', linked_person_name).eq('is_current', True))
                    if not getattr(person_res, 'data', None):
                        person_res = maybe_single_safe(supabase.table('people').select('id, name').ilike('name', f'%{linked_person_name}%').eq('is_current', True))
                    if getattr(person_res, 'data', None):
                        linked_person_id = person_res.data['id']
                
                linked_project_id = None
                linked_project_name = classification_data.get("linked_project_name")
                if linked_project_name:
                    project_res = maybe_single_safe(supabase.table('projects').select('id, name').ilike('name', linked_project_name).eq('is_current', True))
                    if not getattr(project_res, 'data', None):
                        project_res = maybe_single_safe(supabase.table('projects').select('id, name').ilike('name', f'%{linked_project_name}%').eq('is_current', True))
                    if getattr(project_res, 'data', None):
                        linked_project_id = project_res.data['id']
                
                suggested_task = classification_data.get("suggested_task")
                is_human = classification_data.get("is_human_sender", False)
                dedup_decision = None

                if suggested_task:
                    rejected_guard = check_duplicate(suggested_task, rejected_task_list)
                    if rejected_guard['result'] in ['block', 'flag']:
                        print(f"⏭️ Skipping task — matches rejected task: {rejected_guard['matched_title']}")
                        dedup_decision = 'skipped'
                    else:
                        guard = check_duplicate(suggested_task, active_task_list)
                        if guard['result'] == 'block':
                            if guard['is_superset'] and guard['matched_id']:
                                try:
                                    supabase.table('tasks').update({'title': suggested_task}).eq('id', guard['matched_id']).execute()
                                    dedup_decision = 'merged'
                                except Exception:
                                    dedup_decision = 'skipped'
                            else:
                                dedup_decision = 'skipped'

                from core.lib.ingest import ingest
                classification_for_ingest = 'ignored' if dedup_decision == 'skipped' else 'actionable'
                await ingest(
                    text=classification_data.get('summary', '') or suggested_task or subject,
                    source='outlook',
                    classification=classification_for_ingest,
                    summary=classification_data.get('summary', '')[:1000],
                    suggested_title=suggested_task,
                    suggested_project=linked_project_name,
                    linked_person_id=linked_person_id,
                    linked_project_id=linked_project_id,
                    is_human_sender=is_human,
                    has_memory_value=classification_data.get('has_memory_value', False),
                    needs_draft=classification_data.get('needs_draft', False),
                    channel_specific_data={
                        "sender_name": sender,
                        "sender_email": sender_email,
                        "subject": subject,
                        "to_header": to_header,
                        "cc_header": cc_header,
                        "danny_decision": dedup_decision,
                        "body_raw": body,
                    },
                    tracking_id=msg_id,
                    received_at=normalized.get("received_at"),
                    body=normalized.get("body_raw", "")[:20000],
                )
                print(f"✅ [actionable] {subject} | From: {sender_email}")
                processed += 1

        except Exception as e:
            print(f"❌ Error processing Outlook message {msg_id}: {e}")
            try:
                supabase.table('messages').insert({
                    "channel": "email",
                    "message_id": msg_id,
                    "source": "outlook",
                    "sender_name": sender or "unknown",
                    "sender_id": sender_email or "unknown",
                    "classification": EmailStatus.ERROR,
                    "processing_status": "failed",
                    "subject": subject or "processing_error",
                    "received_at": datetime.now(timezone.utc).isoformat(),
                    "expires_at": compute_expires_at(subject or "processing_error", datetime.now(timezone.utc).isoformat())
                }).execute()
            except Exception as insert_err:
                print(f"⚠️ Failed to insert error record for {msg_id}: {insert_err}")
            continue

    print(f"Outlook ingest complete. {processed} processed, {ignored} ignored, {skipped} skipped (duplicates), {skipped_api_error} skipped (api error).")
    
    # --- FETCH SENT ITEMS ---
    print("\nFetching Outlook Sent Items...")
    try:
        sent_messages = fetch_outlook_sent_messages(limit=limit)
        if not sent_messages:
            print("No new Outlook sent messages found.")
        else:
            sent_processed = 0
            sent_skipped = 0
            for msg in sent_messages:
                msg_id = msg.get("id")
                if not msg_id or msg_id in seen_ids:
                    sent_skipped += 1
                    continue
                seen_ids.add(msg_id)
                
                existing = maybe_single_safe(supabase.table('messages').select('id').eq('channel', 'email').eq('message_id', msg_id))
                if existing is not None and getattr(existing, 'data', None):
                    sent_skipped += 1
                    continue
                    
                to_recipients = msg.get("toRecipients", [])
                to_header = ", ".join(r.get("emailAddress", {}).get("address", "") for r in to_recipients if r.get("emailAddress", {}).get("address"))
                
                subject = msg.get('subject', '(No Subject)')
                
                body_data = msg.get("body", {})
                body_content = body_data.get("content", "")
                if body_data.get("contentType", "").lower() == "html":
                    import re
                    body_content = re.sub(r'<[^>]+>', ' ', body_content).strip()
                else:
                    body_content = body_content.strip()
                    
                body_preview = msg.get("bodyPreview", "")
                if len(body_content) > 50:
                    body_preview = body_content
                
                email_row = {
                    "channel": "email",
                    "message_id": msg_id,
                    "thread_id": msg.get("conversationId", ""),
                    "source": "outlook",
                    "direction": "outgoing",
                    "sender_name": to_header,
                    "sender_id": to_header,
                    "subject": subject,
                    "body": body_content[:20000],
                    "received_at": msg.get("sentDateTime") or datetime.now(timezone.utc).isoformat(),
                    "classification": "fyi",
                    "processing_status": "completed",
                    "metadata": {
                        "body_summary": body_preview[:2000]
                    },
                    "expires_at": compute_expires_at(f"{subject} {body_content}", msg.get("sentDateTime") or datetime.now(timezone.utc).isoformat())
                }
                
                insert_res = supabase.table('messages').insert(email_row).execute()
                if getattr(insert_res, 'data', None):
                    sent_processed += 1
                    print(f"✅ [sent] {subject} | To: {to_header}")
                else:
                    sent_skipped += 1
                    
            print(f"Outlook sent ingest complete. {sent_processed} processed, {sent_skipped} skipped.")
    except Exception as e:
        print(f"Outlook sent ingest failed: {e}")

    return {"processed": processed, "ignored": ignored, "skipped": skipped, "skipped_api_error": skipped_api_error}

async def main():
    print(f"Outlook ingest started at {datetime.now(timezone(timedelta(hours=5, minutes=30)))}")
    result = await ingest_outlook_messages(limit=25)
    print(f"Result: {result}")

if __name__ == "__main__":
    asyncio.run(main())