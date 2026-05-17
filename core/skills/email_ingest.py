import random
import os
import json
import asyncio
import base64
import re
from email.utils import parsedate_to_datetime
from datetime import datetime, timedelta, timezone

from core.lib.constants import EmailStatus
from core.lib.people_utils import normalize_person_name, is_blocklisted_person
from core.lib.duplicate_guard import check_duplicate
from core.services.db import get_supabase, get_embedding
from core.services.google_service import get_google_creds, _MemoryCache
from core.services.llm import call_gemini_classify, get_gemini_client

supabase = get_supabase()

RETRYABLE_ERRORS = ['503', '504', '500', 'disconnected', 'timeout', 'deadline exceeded', 'unavailable', 'overloaded', 'rate limit']
NOREPLY_PATTERNS = [
    'noreply', 'no-reply', 'donotreply', 'mailer-daemon',
    'bounce', 'notifications@', 'automated@',
    'nesl.co.in', 'incometax.gov', 'gst.gov', 'mca.gov',
    'estatement@', 'alerts@', 'statement@', 'update@',
    'do-not-reply', 'donotreply'
]


def build_active_task_list() -> list:
    try:
        result = supabase.table('tasks')\
            .select('id, title')\
            .not_.in_('status', ['done', 'cancelled'])\
            .execute()
        return result.data or []
    except Exception as e:
        print(f"Failed to build active task list (failing open): {e}")
        return []


async def generate_draft(sender: str, subject: str, body: str) -> str:
    prompt = f"""You are drafting a professional reply on behalf of Danny (Yashwant Daniel), founder of Crayon. Write a concise, warm, and direct reply to this email. Do not sign off with a full signature block — end with just 'Danny'. Do not send — this is a draft for Danny's review.

Sender: {sender}
Subject: {subject}
Body:
{body[:1000]}"""

    try:
        response = await call_gemini_classify(prompt, model="gemini-3.1-flash-lite-preview")
        return response.text.strip()
    except Exception as e:
        print(f"Draft generation failed: {e}")
        return ""


async def add_person_from_email(name: str, email: str = None, source: str = 'email_ingest') -> int | None:
    if not name or len(name.strip()) < 2:
        return None

    name_clean = name.strip()

    if is_blocklisted_person(name_clean):
        print(f"Skipping blocklisted person from email: {name_clean}")
        return None

    existing = supabase.table('people').select('id, name').execute()
    existing_names = {}
    for p in (existing.data or []):
        existing_names[p['name'].lower()] = p['id']
        norm = normalize_person_name(p['name'])
        if norm and norm not in existing_names:
            existing_names[norm] = p['id']

    name_lower = name_clean.lower()
    name_norm = normalize_person_name(name_clean)

    matched = existing_names.get(name_norm) if name_norm else None
    if matched is None:
        matched = existing_names.get(name_lower)
    if matched is not None:
        return matched

    result = supabase.table('people').insert({
        "name": name_clean,
        "role": None,
        "strategic_weight": 5,
        "source": source
    }).execute()

    if result.data:
        print(f"Added new person from email: {name_clean}")
        return result.data[0]['id']
    return None


async def write_relationship_note(sender_name: str, sender_email: str, subject: str, summary: str, people_id: int = None):
    prompt = f"""Synthesize a brief relationship note based on this email interaction. Focus on: who sent it, what was communicated, why it matters for Danny's relationship knowledge graph. NOT a raw summary.

Sender: {sender_name} ({sender_email})
Subject: {subject}
Summary: {summary}

Output ONLY a concise 1-2 sentence note about the relationship context."""

    try:
        response = await call_gemini_classify(prompt, model="gemini-3.1-flash-lite-preview")
        note_content = response.text.strip()
        embedding = await asyncio.to_thread(get_embedding, note_content)

        metadata = {}
        if people_id:
            metadata['people_id'] = people_id

        supabase.table('memories').insert({
            "content": note_content,
            "memory_type": "relationship_note",
            "embedding": embedding,
            "embedding_status": 'success' if embedding and any(embedding) else 'failed',
            "source": "email_ingest",
            "metadata": metadata if metadata else None
        }).execute()
        print(f"Relationship note written for {sender_name}")
    except Exception as e:
        print(f"Relationship note write failed: {e}")


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
                    try:
                        cleaned = data.replace('\n', '').replace('\r', '').replace(' ', '')
                        body += base64.urlsafe_b64decode(
                            cleaned + '=' * (-len(cleaned) % 4)
                        ).decode('utf-8', errors='ignore')
                    except Exception:
                        try:
                            import base64 as _b64
                            body += _b64.b64decode(
                                data + '=' * (-len(data) % 4)
                            ).decode('utf-8', errors='ignore')
                        except Exception:
                            pass
            elif 'parts' in part:
                body += decode_body(part)
    else:
        data = payload.get('body', {}).get('data', '')
        if data:
            try:
                cleaned = data.replace('\n', '').replace('\r', '').replace(' ', '')
                body += base64.urlsafe_b64decode(
                    cleaned + '=' * (-len(cleaned) % 4)
                ).decode('utf-8', errors='ignore')
            except Exception:
                try:
                    import base64 as _b64
                    body += _b64.b64decode(
                        data + '=' * (-len(data) % 4)
                    ).decode('utf-8', errors='ignore')
                except Exception:
                    pass
    return body


def decode_html_body(payload: dict) -> str:
    if 'parts' in payload:
        for part in payload['parts']:
            if part.get('mimeType') == 'text/html':
                data = part.get('body', {}).get('data', '')
                if data:
                    try:
                        cleaned = data.replace('\n', '').replace('\r', '').replace(' ', '')
                        return base64.urlsafe_b64decode(
                            cleaned + '=' * (-len(cleaned) % 4)
                        ).decode('utf-8', errors='ignore')
                    except Exception:
                        try:
                            import base64 as _b64
                            return _b64.b64decode(
                                data + '=' * (-len(data) % 4)
                            ).decode('utf-8', errors='ignore')
                        except Exception:
                            pass
            elif 'parts' in part:
                result = decode_html_body(part)
                if result:
                    return result
    else:
        if payload.get('mimeType') == 'text/html':
            data = payload.get('body', {}).get('data', '')
            if data:
                try:
                    cleaned = data.replace('\n', '').replace('\r', '').replace(' ', '')
                    return base64.urlsafe_b64decode(
                        cleaned + '=' * (-len(cleaned) % 4)
                    ).decode('utf-8', errors='ignore')
                except Exception:
                    try:
                        import base64 as _b64
                        return _b64.b64decode(
                            data + '=' * (-len(data) % 4)
                        ).decode('utf-8', errors='ignore')
                    except Exception:
                        pass
    return ""


async def classify_email(sender: str, subject: str, body: str, to_header: str = '', cc_header: str = '') -> dict:
    prompt = f"""You are classifying an email for Danny (Yashwant Daniel), founder of Crayon, Chennai, India.

MAILBOX CONTEXT: This is Danny's PERSONAL Gmail inbox. It is scoped strictly to two labels:
- inbox: personal correspondence, family, church-related work
- Completed/Ashraya: Ashraya is a church ministry Danny leads

This mailbox does NOT receive Crayon business emails, client work, or vendor communications. Those go to his Outlook work inbox.

What legitimately arrives here:
- Personal contacts: family, friends, personal relationships
- Church contacts: pastors, ministry team, Ashraya volunteers, church admin, event coordination
- Personal finances: CA, personal banking, insurance (human-sent, not automated alerts)
- Government correspondence: direct human responses from officials (not automated portal emails)
- Personal vendors: doctor, school, personal services

Sender: {sender}
To: {to_header}
CC: {cc_header}
Subject: {subject}
Body:
{body[:1000]}

CLASSIFICATION RULES

CLASSIFY AS "ignored" IF ANY of these are true:
- Sender contains: noreply, no-reply, donotreply, mailer-daemon, bounce, notifications@, automated@, alert@, update@
- It is an OTP, verification code, payment alert, bank notification, delivery update, or booking confirmation
- It is from a SaaS platform, e-commerce site, or any automated system
- It is a newsletter, promotional offer, or bulk mail
- Subject starts with FW: or Fwd: with no new content added

CLASSIFY AS "fyi" IF:
- Danny is in CC or BCC (not primary To: recipient)
- A real person is sharing information — a church update, ministry report, or personal FYI — where no response is expected or needed

CLASSIFY AS "actionable" IF:
- Addressed directly To: Danny
- From a real individual (family, friend, church member, ministry volunteer, pastor, personal contact)
- Requires Danny to respond, decide, coordinate, approve, or take an action
- Church coordination, Ashraya ministry tasks, personal obligations, and family matters all qualify

OUTPUT RULES

suggested_task:
- Verb-first, specific action (e.g., "Confirm attendance for Ashraya prayer meeting with Elder Thomas", "Call Amma about Sunday lunch plan")
- NULL if fyi or ignored
- NULL if action cannot be stated specifically

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
- true if the email contains a decision, commitment, ministry update, relationship context, or information worth remembering weeks later
- false for transactional or routine correspondence
- Can only be true if is_human_sender is also true

Return ONLY valid JSON, NO markdown, NO explanation:
{{
  "classification": "ignored|fyi|actionable",
  "summary": "2 sentences max. Who sent it, what they want or shared.",
  "suggested_task": "verb-first task or null",
  "needs_draft": true or false,
  "linked_person_name": "full name if identifiable, else null",
  "linked_project_name": "project or ministry name if mentioned, else null",
  "is_human_sender": true or false,
  "has_memory_value": true or false
}}"""

    response = await call_gemini_classify(
        prompt,
        model="gemini-3.1-flash-lite-preview",
        config={"response_mime_type": "application/json"}
    )
    return json.loads(response.text)


async def process_email(msg_data: dict, gmail_service, active_tasks: list) -> tuple:
    msg_id = msg_data['id']
    sender_name = None
    sender_email = None
    subject = None

    try:
        existing = supabase.table('emails').select('id').eq('message_id', msg_id).maybe_single().execute()
        if existing is not None and existing.data:
            return (EmailStatus.IGNORED, msg_data.get('snippet', '')[:50])
    except Exception as e:
        print(f"Dedup check failed for {msg_id}: {e}")

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
            try:
                classification_data = await classify_email(sender_header, subject, body, to_header, cc_header)
            except Exception as classify_err:
                print(f"[skipped - classification error] {subject} | Will retry on next run")
                return ("skipped_api_error", subject)
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
            print(f"[ignored] {subject} | From: {sender_email}")
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
            if not insert_res.data:
                print(f"Email insert returned no data for {subject}")
                return ('error', 'insert returned no data')

            is_human = classification_data.get('is_human_sender', False)
            has_memory = classification_data.get('has_memory_value', False)

            people_id = None
            if is_human:
                people_id = await add_person_from_email(sender_name, sender_email)

            if is_human and has_memory:
                await write_relationship_note(
                    sender_name,
                    sender_email,
                    subject,
                    classification_data.get('summary', ''),
                    people_id=people_id
                )

            print(f"[fyi] {subject} | From: {sender_email}")

        elif classification == 'actionable':
            linked_person_id = None
            linked_person_name = classification_data.get('linked_person_name')

            if linked_person_name:
                if is_blocklisted_person(linked_person_name):
                    print(f"Skipping blocklisted linked person: {linked_person_name}")
                else:
                    person_res = supabase.table('people').select('id, name').ilike('name', f'%{linked_person_name}%').maybe_single().execute()
                    if getattr(person_res, 'data', None):
                        linked_person_id = person_res.data['id']
                    else:
                        new_person = supabase.table('people').insert({
                            "name": linked_person_name,
                            "source": "email_ingest",
                            "strategic_weight": 5
                        }).execute()
                        if new_person.data:
                            linked_person_id = new_person.data[0]['id']
                            print(f"Added linked person from email: {linked_person_name}")

            is_human = classification_data.get('is_human_sender', False)
            if is_human:
                sender_id = await add_person_from_email(sender_name, sender_email)
                if not linked_person_id:
                    linked_person_id = sender_id

            linked_project_id = None
            linked_project_name = classification_data.get('linked_project_name')
            if linked_project_name:
                project_res = supabase.table('projects').select('id, name').ilike('name', f'%{linked_project_name}%').maybe_single().execute()
                if getattr(project_res, 'data', None):
                    linked_project_id = project_res.data['id']

            email_row['linked_person_id'] = linked_person_id
            email_row['linked_project_id'] = linked_project_id

            insert_res = supabase.table('emails').insert(email_row).execute()
            if not insert_res.data:
                print(f"Email insert returned no data for {subject}")
                return ('error', 'insert returned no data')
            email_id = insert_res.data[0]['id']

            suggested_task = classification_data.get('suggested_task')

            if suggested_task:
                suggested_title = suggested_task or ''
                guard = check_duplicate(suggested_title, active_tasks)
                if guard['result'] == 'block':
                    if guard['is_superset'] and guard['matched_id']:
                        try:
                            supabase.table('tasks').update({'title': suggested_title}).eq('id', guard['matched_id']).execute()
                            print(f"Auto-updated task {guard['matched_id']}: '{guard['matched_title']}' -> '{suggested_title}'")
                        except Exception as upd_err:
                            print(f"Auto-update failed: {upd_err}")
                    else:
                        print(f"Duplicate guard (block): '{suggested_title}' matches existing task [{guard['matched_id']}]. Skipping.")
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
                    print(f"Duplicate guard (flag): '{suggested_title}' may be similar to task '{guard['matched_title']}'. Created with flag.")
                else:
                    supabase.table('email_pending_tasks').insert({
                        "email_id": email_id,
                        "suggested_title": suggested_task,
                        "suggested_project": linked_project_name,
                        "shown_in_brief": False,
                        "danny_decision": None,
                        "is_human_sender": is_human
                    }).execute()

            if is_human and classification_data.get('has_memory_value'):
                await write_relationship_note(
                    sender_name,
                    sender_email,
                    subject,
                    classification_data.get('summary', ''),
                    people_id=linked_person_id or (await add_person_from_email(sender_name, sender_email) if is_human else None)
                )

            if classification_data.get('needs_draft'):
                draft_body = await generate_draft(sender_name, subject, body)
                if draft_body:
                    supabase.table('email_drafts').insert({
                        "email_id": email_id,
                        "draft_body": draft_body,
                        "status": "pending"
                    }).execute()

            print(f"[actionable] {subject} | From: {sender_email}")

        return (classification, subject)

    except Exception as e:
        print(f"Error processing email {msg_id}: {e}")
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
            print(f"Failed to insert error record: {insert_err}")
        return (EmailStatus.ERROR, str(e))


async def main():
    now_ist = datetime.now(timezone(timedelta(hours=5, minutes=30)))
    print("Email ingest started at " + str(now_ist))

    from googleapiclient.discovery import build
    gmail_service = build('gmail', 'v1', credentials=get_google_creds(), cache=_MemoryCache())

    active_tasks = build_active_task_list()
    print(f"Loaded {len(active_tasks)} active tasks for duplicate checking.")

    cutoff = datetime.now(timezone.utc) - timedelta(hours=48)
    after_timestamp = int(cutoff.timestamp())
    query = f'(label:inbox OR label:"Completed/Ashraya") after:{after_timestamp}'
    result = gmail_service.users().messages().list(userId='me', q=query, maxResults=50).execute()
    messages = result.get('messages', [])

    if not messages:
        print("No new emails found.")
        print("Email ingest complete. 0 processed, 0 ignored, 0 skipped (duplicates).")
        return

    print(f"Found {len(messages)} emails to process.")

    processed = 0
    ignored = 0
    skipped = 0
    skipped_api_error = 0
    results = []
    seen_ids = set()

    for msg in messages:
        if not msg:
            print("Skipping None message data")
            continue
        msg_id = msg.get('id')
        if msg_id in seen_ids:
            print(f"Duplicate msg_id in batch: {msg_id}, skipping")
            skipped += 1
            continue
        seen_ids.add(msg_id)
        try:
            status, detail = await process_email(msg, gmail_service, active_tasks)
            if status == EmailStatus.IGNORED:
                ignored += 1
            elif status == EmailStatus.ERROR:
                processed += 1
            elif status == "skipped_api_error":
                skipped_api_error += 1
            else:
                processed += 1
            results.append((status, detail))
        except Exception as e:
            print(f"Fatal error processing message: {e}")

    print(f"Email ingest complete. {processed} processed, {ignored} ignored, {skipped} skipped (duplicates), {skipped_api_error} skipped (api error).")


if __name__ == "__main__":
    asyncio.run(main())
