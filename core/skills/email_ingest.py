from core.llm.constants import CLASSIFICATION_MODEL
from core.llm import get_embedding
import json
import asyncio
import base64
import re
from email.utils import parsedate_to_datetime
from datetime import datetime, timedelta, timezone

from core.lib.constants import EmailStatus
from core.lib.people_utils import normalize_person_name, is_blocklisted_person
from core.lib.duplicate_guard import check_duplicate
from core.retrieval.pipeline import schedule_index_memory
from core.pulse.entity_extractor import extract_and_link_entities
from core.services.db import get_supabase, maybe_single_safe
from core.services.google_service import get_google_creds, _MemoryCache
from core.lib.time_utils import compute_expires_at
from core.services.llm import call_gemini_classify

supabase = get_supabase()

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
            .eq('is_current', True)\
            .not_.in_('status', ['done', 'cancelled'])\
            .execute()
        return result.data or []
    except Exception as e:
        print(f"Failed to build active task list (failing open): {e}")
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


async def generate_draft(sender: str, subject: str, body: str) -> str:
    prompt = f"""You are drafting a professional reply on behalf of Danny (Yashwant Daniel), founder of Crayon. Write a concise, warm, and direct reply to this email. Do not sign off with a full signature block — end with just 'Danny'. Do not send — this is a draft for Danny's review.

Sender: {sender}
Subject: {subject}
Body:
{body[:1000]}"""

    try:
        response = await call_gemini_classify(prompt, model=CLASSIFICATION_MODEL)
        text = response.text.strip()
        if text and '"reasoning": "safe_hold"' in text:
            print(f"Draft generation returned safe_hold fallback for [{subject}]")
            return ""
        return text
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
        # Resolve canonical if it exists in graph
        g_res = maybe_single_safe(supabase.table("graph_nodes").select("canonical_id, db_record_id").eq("db_record_id", str(matched)))
        if g_res and g_res.data and g_res.data.get("canonical_id"):
            c_res = maybe_single_safe(supabase.table("graph_nodes").select("db_record_id").eq("id", g_res.data["canonical_id"]))
            if c_res and c_res.data and c_res.data.get("db_record_id"):
                return int(c_res.data["db_record_id"])
        return matched

    from core.pulse.tools import create_person
    result_msg = create_person(name=name_clean, context=source)
    if "ID " in result_msg:
        try:
            new_id = int(result_msg.split("ID ")[1])
            print(f"Added new person from email via tool: {name_clean}")
            return new_id
        except Exception:
            pass
    return None


async def write_relationship_note(sender_name: str, sender_email: str, subject: str, summary: str, people_id: int = None):
    prompt = f"""Synthesize a brief relationship note based on this email interaction. Focus on: who sent it, what was communicated, why it matters for Danny's relationship knowledge graph. NOT a raw summary.

Sender: {sender_name} ({sender_email})
Subject: {subject}
Summary: {summary}

Output ONLY a concise 1-2 sentence note about the relationship context."""

    try:
        response = await call_gemini_classify(prompt, model=CLASSIFICATION_MODEL)
        note_content = response.text.strip()
        embedding = (await get_embedding(note_content)).vector

        metadata = {}
        if people_id:
            metadata['people_id'] = people_id

        result = supabase.table('memories').insert({
            "content": note_content,
            "memory_type": "relationship_note",
            "embedding": embedding,
            "embedding_status": 'success' if embedding and any(embedding) else 'failed',
            "source": "email_ingest",
            "expires_at": compute_expires_at(note_content, datetime.now(timezone.utc).isoformat()),
            "metadata": metadata if metadata else None
        }).execute()
        memory_id = result.data[0]['id']
        schedule_index_memory(memory_id, note_content, "relationship_note", "email_ingest")
        extract_and_link_entities(note_content, str(memory_id), 'memory')
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
        model=CLASSIFICATION_MODEL,
        config={"response_mime_type": "application/json"}
    )
    return json.loads(response.text)


def process_sent_email(msg_data: dict, gmail_service) -> tuple:
    msg_id = msg_data['id']
    try:
        # Check if already exists to prevent duplicate processing
        existing = maybe_single_safe(supabase.table('messages').select('id').eq('channel', 'email').eq('message_id', msg_id))
        if existing is not None and existing.data:
            return ('ignored', msg_data.get('snippet', '')[:50])

        full_msg = gmail_service.users().messages().get(userId='me', id=msg_id, format='full').execute()
        payload = full_msg.get('payload', {})
        headers = {h['name'].lower(): h['value'] for h in payload.get('headers', [])}

        subject = headers.get('subject', '(No Subject)')
        to_header = headers.get('to', '')
        received_at_raw = headers.get('date', '')
        try:
            received_at = parsedate_to_datetime(received_at_raw).isoformat()
        except Exception:
            received_at = datetime.now(timezone.utc).isoformat()
            
        raw_plain = decode_body(payload)
        body = raw_plain[:10000]
        if not body.strip():
            html_body = decode_html_body(payload)
            raw_plain = re.sub(r'<[^>]+>', ' ', html_body).strip()
            body = raw_plain[:10000]
            
        # Try to extract a clean email for the recipient
        match = re.search(r'<(.+?)>', to_header)
        recipient_email = match.group(1).strip() if match else to_header.strip()

        email_row = {
            "channel": "email",
            "source": "gmail",
            "direction": "outgoing",
            "message_id": msg_id,
            "thread_id": full_msg.get('threadId', ''),
            "sender_name": to_header,
            "sender_id": recipient_email,
            "subject": subject,
            "body": raw_plain[:20000],
            "received_at": received_at,
            "classification": "fyi",
            "processing_status": "completed",
            "expires_at": compute_expires_at(f"{subject} {raw_plain[:20000]}", received_at),
            "metadata": {
                "body_summary": body[:2000]
            }
        }

        insert_res = supabase.table('messages').insert(email_row).execute()
        if not insert_res.data:
            return ('error', 'insert returned no data')

        print(f"[sent] {subject} | To: {recipient_email}")
        return ('processed', subject)
    except Exception as e:
        print(f"Error processing sent email {msg_id}: {e}")
        return ('error', str(e))


async def process_email(msg_data: dict, gmail_service, active_tasks: list, rejected_tasks: list) -> tuple:
    msg_id = msg_data['id']
    sender_name = None
    sender_email = None
    subject = None

    try:
        existing = maybe_single_safe(supabase.table('messages').select('id').eq('channel', 'email').eq('message_id', msg_id))
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

        raw_plain = decode_body(payload)
        body = raw_plain[:10000]
        if not body.strip():
            html_body = decode_html_body(payload)
            raw_plain = re.sub(r'<[^>]+>', ' ', html_body).strip()
            body = raw_plain[:10000]

        if any(p in sender_email.lower() for p in NOREPLY_PATTERNS):
            classification_data = {"classification": "ignored", "summary": "No-reply sender", "suggested_task": None, "needs_draft": False, "linked_person_name": None, "linked_project_name": None}
        else:
            try:
                # We only pass the first 1500 chars to Gemini for classification to save tokens
                classification_data = await classify_email(sender_header, subject, body[:1500], to_header, cc_header)
            except Exception:
                print(f"[skipped - classification error] {subject} | Will retry on next run")
                return ("skipped_api_error", subject)
        classification = classification_data.get('classification', 'ignored')

        if classification == 'ignored':
            supabase.table('messages').insert({
                "channel": "email",
                "message_id": msg_id,
                "thread_id": full_msg.get('threadId', ''),
                "source": "gmail",
                "sender_name": sender_name,
                "sender_id": sender_email,
                "subject": subject,
                "received_at": received_at,
                "classification": "ignored",
                "processing_status": "completed",
                "expires_at": compute_expires_at(subject or "", received_at),
                "danny_decision": "skipped"
            }).execute()
            print(f"[ignored] {subject} | From: {sender_email}")
            return (EmailStatus.IGNORED, subject)

        email_row = {
            "channel": "email",
            "message_id": msg_id,
            "thread_id": full_msg.get('threadId', ''),
            "source": "gmail",
            "sender_name": sender_name,
            "sender_id": sender_email,
            "subject": subject,
            "body": raw_plain[:20000],
            "received_at": received_at,
            "classification": classification,
            "processing_status": "completed" if classification != "error" else "failed",
            "expires_at": compute_expires_at(f"{subject} {raw_plain[:20000]}", received_at),
            "linked_person_id": None,
            "linked_project_id": None,
            "metadata": {
                "body_summary": body[:2000]
            }
        }

        if classification == 'fyi':
            insert_res = supabase.table('messages').insert(email_row).execute()
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
                    linked_person_id = await add_person_from_email(linked_person_name, None, source="email_ingest_linked")

            is_human = classification_data.get('is_human_sender', False)
            if is_human:
                sender_id = await add_person_from_email(sender_name, sender_email)
                if not linked_person_id:
                    linked_person_id = sender_id

            linked_project_id = None
            linked_project_name = classification_data.get('linked_project_name')
            if linked_project_name:
                # Exact match first (case-insensitive), fall back to partial
                project_res = maybe_single_safe(supabase.table('projects').select('id, name').ilike('name', linked_project_name))
                if not getattr(project_res, 'data', None):
                    project_res = maybe_single_safe(supabase.table('projects').select('id, name').ilike('name', f'%{linked_project_name}%'))
                if getattr(project_res, 'data', None):
                    linked_project_id = project_res.data['id']

            email_row['linked_person_id'] = linked_person_id
            email_row['linked_project_id'] = linked_project_id

            suggested_task = classification_data.get('suggested_task')
            email_row['is_human_sender'] = is_human

            if suggested_task:
                suggested_title = suggested_task or ''
                email_row['suggested_title'] = suggested_task
                email_row['suggested_project'] = linked_project_name
                
                # Check rejected tasks first
                rejected_guard = check_duplicate(suggested_title, rejected_tasks)
                if rejected_guard['result'] in ['block', 'flag']:
                    print(f"Skipping task as it matches previously rejected task: {rejected_guard['matched_title']}")
                    email_row['danny_decision'] = 'skipped'
                else:
                    guard = check_duplicate(suggested_title, active_tasks)
                    if guard['result'] == 'block':
                        if guard['is_superset'] and guard['matched_id']:
                            try:
                                supabase.table('tasks').update({'title': suggested_title}).eq('id', guard['matched_id']).execute()
                                print(f"Auto-updated task {guard['matched_id']}: '{guard['matched_title']}' -> '{suggested_title}'")
                                email_row['danny_decision'] = 'merged'
                            except Exception as upd_err:
                                print(f"Auto-update failed: {upd_err}")
                                email_row['danny_decision'] = 'skipped'
                        else:
                            print(f"Duplicate guard (block): '{suggested_title}' matches existing task [{guard['matched_id']}]. Skipping.")
                            email_row['danny_decision'] = 'skipped'
                    elif guard['result'] == 'flag':
                        email_row['possible_duplicate'] = True
                        email_row['duplicate_of_title'] = guard['matched_title']
                        print(f"Duplicate guard (flag): '{suggested_title}' may be similar to task '{guard['matched_title']}'. Created with flag.")

            insert_res = supabase.table('messages').insert(email_row).execute()
            if not insert_res.data:
                print(f"Email insert returned no data for {subject}")
                return ('error', 'insert returned no data')
            email_id = insert_res.data[0]['id']

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
                        "message_id": email_id,
                        "draft_body": draft_body,
                        "status": "pending"
                    }).execute()

            print(f"[actionable] {subject} | From: {sender_email}")

        return (classification, subject)

    except Exception as e:
        print(f"Error processing email {msg_id}: {e}")
        try:
            now_iso = datetime.now(timezone.utc).isoformat()
            supabase.table('messages').insert({
                "channel": "email",
                "message_id": msg_id,
                "source": "gmail",
                "sender_name": sender_name or "unknown",
                "sender_id": sender_email or "unknown",
                "classification": "error",
                "processing_status": "failed",
                "subject": subject or "processing_error",
                "received_at": now_iso,
                "expires_at": compute_expires_at(subject or "processing_error", now_iso)
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
    rejected_tasks = fetch_rejected_email_tasks()
    print(f"Loaded {len(active_tasks)} active tasks and {len(rejected_tasks)} rejected tasks for duplicate checking.")

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
            status, detail = await process_email(msg, gmail_service, active_tasks, rejected_tasks)
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
    
    # --- FETCH SENT ITEMS ---
    print("\nFetching Sent Items...")
    sent_query = f'in:sent after:{after_timestamp}'
    try:
        sent_result = gmail_service.users().messages().list(userId='me', q=sent_query, maxResults=50).execute()
        sent_messages = sent_result.get('messages', [])
        
        if not sent_messages:
            print("No new sent emails found.")
        else:
            print(f"Found {len(sent_messages)} sent emails to process.")
            sent_processed = 0
            sent_skipped = 0
            
            for msg in sent_messages:
                if not msg:
                    continue
                msg_id = msg.get('id')
                if msg_id in seen_ids:
                    sent_skipped += 1
                    continue
                seen_ids.add(msg_id)
                
                status, _ = process_sent_email(msg, gmail_service)
                if status == 'processed':
                    sent_processed += 1
                else:
                    sent_skipped += 1
                    
            print(f"Sent email ingest complete. {sent_processed} processed, {sent_skipped} skipped.")
    except Exception as e:
        print(f"Sent emails ingest failed: {e}")

if __name__ == "__main__":
    asyncio.run(main())
