import os
import json
import asyncio
import re
from pathlib import Path
from datetime import datetime, timedelta, timezone

from core.lib.constants import EmailStatus
from core.lib.duplicate_guard import check_duplicate
from core.services.db import get_supabase, get_embedding
from core.services.llm import call_gemini_classify, get_gemini_client
import requests

supabase = get_supabase()

def build_active_task_list() -> list:
    """Fetch all active task titles ONCE for duplicate checking."""
    try:
        result = supabase.table('tasks')\
            .select('id, title')\
            .not_.in_('status', ['done', 'cancelled'])\
            .execute()
        return result.data or []
    except Exception as e:
        print(f"⚠️ Failed to build active task list (failing open): {e}")
        return []

RETRYABLE_ERRORS = ['503', '504', '500', 'disconnected', 'timeout', 'deadline exceeded', 'unavailable', 'overloaded', 'rate limit']
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
        response = await call_gemini_with_retry(prompt, model="gemini-3.1-flash-lite-preview")
        return response.text.strip()
    except Exception as e:
        print(f"Draft generation failed: {e}")
        return ""




async def classify_email(sender: str, subject: str, body: str, to_header: str = '', cc_header: str = '') -> dict:
    prompt = f"""You are classifying an email for Danny (Yashwant Daniel), founder of Crayon, Chennai, India.

MAILBOX CONTEXT: This is Danny's WORK Outlook inbox. It receives exclusively work-related emails. Personal and church emails do NOT arrive here.

What legitimately arrives here:
- Clients: briefs, feedback, approvals, project questions
- Vendors: quotes, invoices (human-sent), delivery confirmations requiring action
- Team: employees, contractors, freelancers, collaborators
- Business partners: legal, CA, compliance, banking (human-sent)
- Business entities: Crayon, Solvstrat, Product Labs, Qhord.

Sender: {sender}
To: {to_header}
CC: {cc_header}
Subject: {subject}
Body:
{body[:1000]}

─── CLASSIFICATION RULES ───

CLASSIFY AS "ignored" IF ANY of these are true:
- Sender contains: noreply, no-reply, donotreply, mailer-daemon, bounce, notifications@, automated@, alert@
- It is an automated SaaS notification (GitHub, Notion, Slack, Stripe, Razorpay, Jira, Trello, any platform digest)
- It is a newsletter, promotional offer, or cold outreach sales email
- Subject starts with FW: or Fwd: with no new content added
- It is a payment receipt, invoice auto-confirmation, or automated billing notification

CLASSIFY AS "fyi" IF:
- Danny is in CC or BCC
- A team member or partner is sharing a status update, report, or information that needs no response
- It is a human-sent update where no action is required from Danny specifically

CLASSIFY AS "actionable" IF:
- Addressed directly To: Danny
- From a real individual (client, vendor, team member, CA, lawyer, partner, contractor)
- Requires Danny to respond, approve, review, decide, schedule, or fulfill a business obligation
- Bias toward actionable for client and vendor emails — when in doubt, surface it

─── OUTPUT RULES ───

suggested_task:
- Verb-first, specific (e.g., "Send revised proposal to Ananya at TechCorp", "Approve Siva's leave request", "Reply to CA Suresh with Q1 financials")
- NULL if fyi or ignored
- NULL if action is too vague to state specifically

needs_draft:
- true if Danny needs to write a reply
- true if is_human_sender = true AND the sender is waiting for acknowledgement,
  confirmation, or an update — even if the task itself is an offline action
- false ONLY if the task is a call, meeting, or internal action where 
  the sender has no expectation of a response

is_human_sender:
- true if sender is a real individual person
- false for any automated system, platform, or bulk sender

has_memory_value:
- true if email contains a decision, project update, confirmed terms, client commitment, or business context worth remembering weeks later
- false for transactional or routine notifications
- Can only be true if is_human_sender is also true

Return ONLY valid JSON, NO markdown, NO explanation:
{{
  "classification": "ignored|fyi|actionable",
  "summary": "2 sentences max. Who sent it, what they want or shared.",
  "suggested_task": "verb-first task or null",
  "needs_draft": true or false,
  "linked_person_name": "full name if identifiable, else null",
  "linked_project_name": "project or company name if mentioned, else null",
  "is_human_sender": true or false,
  "has_memory_value": true or false
}}"""

    response = await call_gemini_with_retry(
        prompt,
        model="gemini-3.1-flash-lite-preview",
        config={"response_mime_type": "application/json"}
    )
    return json.loads(response.text)


def fetch_outlook_messages(limit=25):
    access_token = get_access_token()
    headers = {"Authorization": f"Bearer {access_token}"}

    url = "https://graph.microsoft.com/v1.0/me/mailFolders/inbox/messages"
    params = {
        "$top": limit,
        "$select": "id,subject,receivedDateTime,from,bodyPreview,conversationId,isRead,hasAttachments,internetMessageId,toRecipients,ccRecipients",
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

    to_recipients = msg.get("toRecipients", [])
    cc_recipients = msg.get("ccRecipients", [])
    to_header = ", ".join(r.get("emailAddress", {}).get("address", "") for r in to_recipients if r.get("emailAddress", {}).get("address"))
    cc_header = ", ".join(r.get("emailAddress", {}).get("address", "") for r in cc_recipients if r.get("emailAddress", {}).get("address"))

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
        "to_header": to_header,
        "cc_header": cc_header,
    }

async def ingest_outlook_messages(limit=25):
    messages = fetch_outlook_messages(limit=limit)
    if not messages:
        print("No new Outlook messages found.")
        return {"processed": 0, "ignored": 0, "skipped": 0}

    active_task_list = build_active_task_list()
    print(f"🧠 Loaded {len(active_task_list)} active tasks for duplicate checking.")

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
            existing = supabase.table('emails').select('id').eq('message_id', msg_id).maybe_single().execute()
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
                except Exception as classify_err:
                    print(f"⏭️ [skipped - classification error] {subject} | Will retry on next run")
                    skipped_api_error += 1
                    continue

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
                print(f"⏭️ [ignored] {subject} | From: {sender_email}")
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
                if not getattr(insert_res, 'data', None):
                    print(f"⚠️ Email insert returned no data for {subject}")
                    continue
                
                # FYI path: write relationship_note if human sender with memory value
                is_human = classification_data.get("is_human_sender", False)
                has_memory = classification_data.get("has_memory_value", False)
                if is_human and has_memory:
                    _summary = classification_data.get("summary", "")
                    _mem_content = f"{sender} ({sender_email}): {_summary}"
                    _emb = get_embedding(_mem_content)
                    supabase.table('memories').insert({
                        "content": _mem_content,
                        "memory_type": "relationship_note",
                        "embedding": _emb
                    }).execute()
                    print(f"🧠 [relationship_note] FYI memory saved for {sender_email}")
                
                print(f"✅ [fyi] {subject} | From: {sender_email}")
                processed += 1

            elif classification == "actionable":
                linked_person_id = None
                linked_person_name = classification_data.get("linked_person_name")
                if linked_person_name:
                    person_res = supabase.table('people').select('id, name').ilike('name', f'%{linked_person_name}%').maybe_single().execute()
                    if getattr(person_res, 'data', None):
                        linked_person_id = person_res.data['id']
                
                linked_project_id = None
                linked_project_name = classification_data.get("linked_project_name")
                if linked_project_name:
                    project_res = supabase.table('projects').select('id, name').ilike('name', f'%{linked_project_name}%').maybe_single().execute()
                    if getattr(project_res, 'data', None):
                        linked_project_id = project_res.data['id']
                
                email_row['linked_person_id'] = linked_person_id
                email_row['linked_project_id'] = linked_project_id
                
                insert_res = supabase.table('emails').insert(email_row).execute()
                if not getattr(insert_res, 'data', None):
                    print(f"⚠️ Email insert returned no data for {subject}")
                    continue
                email_id = insert_res.data[0]['id']
                
                suggested_task = classification_data.get("suggested_task")
                is_human = classification_data.get("is_human_sender", False)
                
                if suggested_task:
                    suggested_title = suggested_task or ''
                    guard = check_duplicate(suggested_title, active_task_list)
                    if guard['result'] == 'block':
                        if guard['is_superset'] and guard['matched_id']:
                            try:
                                supabase.table('tasks').update({'title': suggested_title}).eq('id', guard['matched_id']).execute()
                                print(f"🔁 Auto-updated task {guard['matched_id']}: '{guard['matched_title']}' → '{suggested_title}'")
                            except Exception as upd_err:
                                print(f"⚠️ Auto-update failed: {upd_err}")
                        else:
                            print(f"⚠️ Duplicate guard (block): '{suggested_title}' matches existing task [{guard['matched_id']}]. Skipping.")
                    elif guard['result'] == 'flag':
                        supabase.table('email_pending_tasks').insert({
                            "email_id": email_id,
                            "suggested_title": suggested_task,
                            "suggested_project": linked_project_name,
                            "shown_in_brief": False,
                            "danny_decision": None,
                            "is_human_sender": is_human,
                            "possible_duplicate": True,
                            "duplicate_of_title": guard['matched_title']
                        }).execute()
                        print(f"⚠️ Duplicate guard (flag): '{suggested_title}' may be similar to task '{guard['matched_title']}'. Created with flag.")
                    else:
                        supabase.table('email_pending_tasks').insert({
                            "email_id": email_id,
                            "suggested_title": suggested_task,
                            "suggested_project": linked_project_name,
                            "shown_in_brief": False,
                            "danny_decision": None,
                            "is_human_sender": is_human
                        }).execute()
                
                if classification_data.get("needs_draft"):
                    draft_body = await generate_draft(sender, subject, body)
                    if draft_body:
                        supabase.table('email_drafts').insert({
                            "email_id": email_id,
                            "draft_body": draft_body,
                            "status": "pending"
                        }).execute()
                
                print(f"✅ [actionable] {subject} | From: {sender_email}")
                processed += 1

        except Exception as e:
            print(f"❌ Error processing Outlook message {msg_id}: {e}")
            try:
                supabase.table('emails').insert({
                    "message_id": msg_id,
                    "source": "outlook",
                    "sender": (sender or "unknown"),
                    "sender_email": (sender_email or "unknown"),
                    "classification": EmailStatus.ERROR,
                    "status": EmailStatus.ERROR,
                    "subject": (subject or "processing_error"),
                    "received_at": (normalized.get("received_at") if normalized else None) or datetime.now(timezone.utc).isoformat()
                }).execute()
            except Exception as insert_err:
                print(f"⚠️ Failed to insert error record for {msg_id}: {insert_err}")
            continue

    print(f"Outlook ingest complete. {processed} processed, {ignored} ignored, {skipped} skipped (duplicates), {skipped_api_error} skipped (api error).")
    return {"processed": processed, "ignored": ignored, "skipped": skipped, "skipped_api_error": skipped_api_error}

async def main():
    print(f"Outlook ingest started at {datetime.now(timezone(timedelta(hours=5, minutes=30)))}")
    result = await ingest_outlook_messages(limit=25)
    print(f"Result: {result}")

if __name__ == "__main__":
    asyncio.run(main())