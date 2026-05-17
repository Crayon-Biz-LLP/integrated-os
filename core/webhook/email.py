import os
import json
import re
import base64
import asyncio
import httpx
from email.mime.text import MIMEText
from email.utils import getaddresses
from datetime import datetime, timezone, timedelta
from supabase import create_client, Client
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from core.lib.audit_logger import audit_log_sync
from core.webhook.telegram import send_telegram
from core.webhook.utils import supabase, is_already_in_tasks_table


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
        guard = is_already_in_tasks_table(title)

        if guard['result'] == 'block':
            if guard['is_superset'] and guard['matched_id']:
                try:
                    client.table('tasks').update({'title': title}).eq('id', guard['matched_id']).execute()
                    client.table('email_pending_tasks').update({'danny_decision': 'merged'}).eq('id', row['id']).execute()
                    print(f"Auto-updated task {guard['matched_id']}: '{guard['matched_title']}' → '{title}'")
                    return {
                        "success": True, "action": "updated",
                        "message": f"Updated task [{guard['matched_id']}] with richer title: {title}"
                    }
                except Exception:
                    pass
            client.table('email_pending_tasks').update({'danny_decision': 'skipped'}).eq('id', row['id']).execute()
            return {
                "success": False, "action": "duplicate",
                "message": f"A similar task already exists on your board: [{title}]"
            }

        try:
            insert_data = {
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
            }
            if guard['result'] == 'flag':
                insert_data['metadata']['possible_duplicate'] = True
                insert_data['metadata']['duplicate_of_title'] = guard['matched_title']
                insert_data['metadata']['duplicate_of_id'] = guard['matched_id']

            client.table('raw_dumps').insert([insert_data]).execute()
        except Exception:
            return {
                "success": False, "action": "staging_failed",
                "message": f"Task staging failed for [{row['id']}]. You can retry."
            }

        client.table('email_pending_tasks').update({'danny_decision': 'approved'}).eq('id', row['id']).execute()

        if guard['result'] == 'flag':
            print(f"Staged to raw_dumps with possible_duplicate flag: {title}")
            return {
                "success": True, "action": "approved",
                "message": f"Task staged: {title}\n⚠️ Looks similar to '{guard['matched_title']}' — kept both."
            }

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

        msg = MIMEText(draft['draft_body'], _charset='utf-8')
        msg['To'] = email['sender_email']
        msg['From'] = os.getenv('GMAIL_SENDER_EMAIL', '')
        msg['Subject'] = f"Re: {email['subject']}"

        # Fetch original message headers for reply-all behavior and proper threading
        self_email = os.getenv('GMAIL_SENDER_EMAIL', '')
        original_msg_id = None
        additional_cc = []

        try:
            original = gmail_service.users().messages().get(
                userId='me', id=email['message_id'],
                format='metadata', metadataHeaders=['Message-ID', 'To', 'Cc']
            ).execute()
            orig_headers_list = original.get('payload', {}).get('headers', [])

            # Extract RFC 5322 Message-ID for In-Reply-To/References
            for h in orig_headers_list:
                if h['name'].lower() == 'message-id':
                    original_msg_id = h['value'].strip()
                    break

            # Collect all recipients from To and Cc for reply-all (exclude sender and self)
            orig_headers = {h['name'].lower(): h['value'] for h in orig_headers_list}
            sender_email_lower = email['sender_email'].lower()
            self_email_lower = self_email.lower()

            for field in ('to', 'cc'):
                header_val = orig_headers.get(field, '')
                if header_val:
                    for display_name, addr in getaddresses([header_val]):
                        addr_lower = addr.lower()
                        if addr_lower and addr_lower != sender_email_lower and addr_lower != self_email_lower:
                            if display_name and display_name != addr:
                                additional_cc.append(f"{display_name} <{addr}>")
                            else:
                                additional_cc.append(addr)

        except Exception as e:
            audit_log_sync("webhook", "WARNING",
                f"Could not fetch original message {email['message_id']} for reply headers: {e}")

        # Set threading headers (use proper Message-ID if available)
        if original_msg_id:
            msg['In-Reply-To'] = original_msg_id
            msg['References'] = original_msg_id
        else:
            msg['In-Reply-To'] = email['thread_id']
            msg['References'] = email['thread_id']

        # Include additional CC recipients (reply-all behavior)
        if additional_cc:
            msg['Cc'] = ', '.join(additional_cc)

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
    """Send an approved draft via Outlook Graph API replyAll. Returns (success: bool, error: str|None)."""
    try:
        email = draft['emails']
        body = draft['draft_body']

        access_token = os.getenv("OUTLOOK_ACCESS_TOKEN")
        if not access_token:
            from core.skills.outlook_token_helper import refresh_outlook_token
            result = refresh_outlook_token(write_back=True)
            access_token = result["access_token"]

        payload = {
            "message": {
                "body": {"contentType": "Text", "content": body}
            }
        }

        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json"
        }

        # Update status to 'sent' BEFORE API call to prevent double-send
        supabase.table('email_drafts').update({'status': 'sent'}).eq('id', draft['id']).execute()

        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                f"https://graph.microsoft.com/v1.0/me/messages/{email['message_id']}/replyAll",
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
                    f"https://graph.microsoft.com/v1.0/me/messages/{email['message_id']}/replyAll",
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

