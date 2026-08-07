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
from core.services.db import (
    active_user_ids, channel_tenant_scope, maybe_single_safe, tenant_aware_client,
    tenant_scope,
)
from core.services.google_service import get_cached_service
from core.lib.time_utils import compute_expires_at
from core.services.llm import call_gemini_classify

# Tenant #1 (Danny) archive Gmail label — the SINGLE source of truth. Used
# as the value seeded into core_config by scripts/seed_tenant1_m6_config.py.
# NOT a runtime fallback — a tenant without an 'email_archive_label' row
# scans INBOX-wide (see _archive_label_filter).
TENANT1_EMAIL_ARCHIVE_LABEL = "Completed/Ashraya"

supabase = tenant_aware_client()

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
    from core.services.user_settings import resolve_user_name, resolve_context
    _user_name = resolve_user_name()
    _user_context = resolve_context()
    prompt = f"""You are drafting a professional reply on behalf of {_user_name}. {_user_context} Write a concise, warm, and direct reply to this email. Do not sign off with a full signature block — end with just '{_user_name}'. Do not send — this is a draft for {_user_name}'s review.

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


async def add_person_from_email(name: str, email: str = None, source: str = 'email_ingest') -> str | None:
    """Resolve (or create) a person, returning the graph NODE uuid.

    The people mirror table was removed (migration 75) — the node's own
    UUID is the person id and is what messages.linked_person_id stores.
    """
    if not name or len(name.strip()) < 2:
        return None

    name_clean = name.strip()

    if is_blocklisted_person(name_clean):
        print(f"Skipping blocklisted person from email: {name_clean}")
        return None

    # Match against live person NODES by label / normalized name.
    existing = supabase.table('graph_nodes').select('id, label, canonical_id').eq('type', 'person').eq('is_current', True).execute()
    existing_names = {}
    for p in (existing.data or []):
        label = p.get('label') or ''
        existing_names[label.lower()] = p['id']
        norm = normalize_person_name(label)
        if norm and norm not in existing_names:
            existing_names[norm] = p['id']

    name_lower = name_clean.lower()
    name_norm = normalize_person_name(name_clean)

    matched = existing_names.get(name_norm) if name_norm else None
    if matched is None:
        matched = existing_names.get(name_lower)

    if matched is not None:
        # Follow canonical chain if this node was merged into another
        node = next((p for p in existing.data if p['id'] == matched), None)
        canonical_id = (node or {}).get('canonical_id')
        if canonical_id:
            return str(canonical_id)
        return str(matched)

    from core.pulse.tools import create_person
    result = await create_person(name=name_clean, context=source)
    # create_person returns the created node id (UUID) on success.
    if result and result.get('node_id'):
        print(f"Added new person from email via tool: {name_clean}")
        return str(result['node_id'])
    return None


async def write_relationship_note(sender_name: str, sender_email: str, subject: str, summary: str, people_id: str = None):
    from core.services.user_settings import resolve_user_name
    _user_name = resolve_user_name()
    prompt = f"""Synthesize a brief relationship note based on this email interaction. Focus on: who sent it, what was communicated, why it matters for {_user_name}'s relationship knowledge graph. NOT a raw summary.

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
    from core.prompts.email_classify import build_email_classify_prompt
    prompt = build_email_classify_prompt(
        mailbox_type="personal",
        sender=sender,
        subject=subject,
        body=body[:1000],
        to_header=to_header,
        cc_header=cc_header,
    )
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
            classification_data = {"classification": "ignored", "summary": "No-reply sender", "suggested_task": None, "needs_draft": False, "linked_person_name": None, "linked_organization_name": None}
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

        if classification == 'fyi':
            from core.lib.ingest import ingest
            await ingest(
                text=classification_data.get('summary', '') or subject,
                source='gmail',
                classification='fyi',
                summary=classification_data.get('summary', '')[:1000],
                is_human_sender=classification_data.get('is_human_sender', False),
                has_memory_value=classification_data.get('has_memory_value', False),
                channel_specific_data={
                    "sender_name": sender_name,
                    "sender_email": sender_email,
                    "subject": subject,
                    "to_header": to_header,
                    "cc_header": cc_header,
                    "body_raw": raw_plain[:20000],
                },
                tracking_id=msg_id,
                received_at=received_at,
                body=raw_plain[:20000],
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



            suggested_task = classification_data.get('suggested_task')
            dedup_decision = None  # None = normal, 'skipped', 'merged'

            if suggested_task:
                # Check rejected tasks first
                rejected_guard = check_duplicate(suggested_task, rejected_tasks)
                if rejected_guard['result'] in ['block', 'flag']:
                    print(f"Skipping task — matches rejected task: {rejected_guard['matched_title']}")
                    dedup_decision = 'skipped'
                else:
                    guard = check_duplicate(suggested_task, active_tasks)
                    if guard['result'] == 'block':
                        if guard['is_superset'] and guard['matched_id']:
                            try:
                                supabase.table('tasks').update({'title': suggested_task}).eq('id', guard['matched_id']).execute()
                                print(f"Auto-merged task {guard['matched_id']}: '{guard['matched_title']}' → '{suggested_task}'")
                                dedup_decision = 'merged'
                            except Exception as upd_err:
                                print(f"Auto-merge failed: {upd_err}")
                                dedup_decision = 'skipped'
                        else:
                            dedup_decision = 'skipped'

            # Route through ingest() — same contract for fyi, actionable, ignored
            from core.lib.ingest import ingest
            classification_for_ingest = 'ignored' if dedup_decision == 'skipped' else 'actionable'
            await ingest(
                text=classification_data.get('summary', '') or suggested_task or subject,
                source='gmail',
                classification=classification_for_ingest,
                summary=classification_data.get('summary', '')[:1000],
                suggested_title=suggested_task,                    suggested_project=None,
                linked_person_id=linked_person_id,
                is_human_sender=is_human,
                has_memory_value=classification_data.get('has_memory_value', False),
                needs_draft=classification_data.get('needs_draft', False),
                channel_specific_data={
                    "sender_name": sender_name,
                    "sender_email": sender_email,
                    "subject": subject,
                    "to_header": to_header,
                    "cc_header": cc_header,
                    "danny_decision": dedup_decision,
                    "body_raw": raw_plain[:20000],
                },
                tracking_id=msg_id,
                received_at=received_at,
                body=raw_plain[:20000],
            )
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

    gmail_service = get_cached_service('gmail', 'v1')
    if gmail_service is None:
        print("No Google creds for this tenant — email ingest skipped.")
        return

    active_tasks = build_active_task_list()
    rejected_tasks = fetch_rejected_email_tasks()
    print(f"Loaded {len(active_tasks)} active tasks and {len(rejected_tasks)} rejected tasks for duplicate checking.")

    cutoff = datetime.now(timezone.utc) - timedelta(hours=48)
    after_timestamp = int(cutoff.timestamp())
    # Per-tenant Gmail label (M6): core_config 'email_archive_label' is
    # authoritative when a row exists (empty content → INBOX only). When the
    # Neutral fallback: a tenant without an 'email_archive_label' row gets
    # no label filter (INBOX-wide scan), never another tenant's label.
    label_part = ''
    try:
        res = supabase.table('core_config').select('content').eq('key', 'email_archive_label').execute()
        if res.data:
            lbl = str(res.data[0].get('content') or '').strip()
            label_part = f' OR label:"{lbl}"' if lbl else ''
    except Exception:
        pass
    query = f'(label:inbox{label_part}) after:{after_timestamp}'
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

async def _run_email_ingest_for_tenant(uid: str):
    """Run one tenant's email ingest cycle under its own scope."""
    with tenant_scope(uid):
        await main()


async def run_fanout():
    """(M4) Cron fan-out: iterate all active users, one tenant-scoped email
    cycle each. Each user's Gmail is read with THEIR OWN OAuth token
    (get_cached_service resolves per-user), and every write lands under
    their owner_id. A per-tenant failure is isolated and reported without
    aborting the other tenants. Tenants without Google creds are skipped
    gracefully inside main() (no-op, no crash).

    Legacy (pre-db/78, or no active users): runs once unscoped, exactly as
    the pre-M4 email ingest did — the channel tenant (or env creds) path.
    """
    uids = active_user_ids()
    if not uids:
        await _run_email_ingest_for_tenant_unscoped()
        return
    for uid in uids:
        try:
            await _run_email_ingest_for_tenant(uid)
        except Exception as e:
            from core.lib.audit_logger import audit_log_sync
            audit_log_sync("email_ingest", "ERROR", f"Email ingest failed for tenant {uid}: {e}")
            print(f"❌ Email ingest failed for tenant {uid}: {e}")


async def _run_email_ingest_for_tenant_unscoped():
    """Legacy single-tenant path — pre-db/78 or no active users. Preserves
    the exact pre-M4 behaviour (channel tenant when resolvable, else env
    creds)."""
    with channel_tenant_scope():
        await main()


if __name__ == "__main__":
    asyncio.run(run_fanout())
