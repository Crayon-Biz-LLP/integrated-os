import os
import requests
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

from core.services.google_service import get_google_creds, _MemoryCache
from core.skills.outlook_token_helper import refresh_outlook_token

def search_gmail_sent(query: str, limit: int = 5) -> list:
    """Searches Gmail Sent folder for emails matching the query."""
    try:
        from googleapiclient.discovery import build
        gmail_service = build('gmail', 'v1', credentials=get_google_creds(), cache=_MemoryCache())
        
        # Build search query
        search_query = f'in:sent {query}' if query else 'in:sent'
        
        result = gmail_service.users().messages().list(userId='me', q=search_query, maxResults=limit).execute()
        messages = result.get('messages', [])
        
        parsed_results = []
        for msg in messages:
            msg_id = msg.get('id')
            if not msg_id:
                continue
                
            full_msg = gmail_service.users().messages().get(userId='me', id=msg_id, format='metadata', metadataHeaders=['To', 'Subject', 'Date', 'Message-ID']).execute()
            payload = full_msg.get('payload', {})
            headers = {h['name'].lower(): h['value'] for h in payload.get('headers', [])}
            
            subject = headers.get('subject', '(No Subject)')
            to_header = headers.get('to', '')
            received_at_raw = headers.get('date', '')
            
            try:
                received_at = parsedate_to_datetime(received_at_raw).isoformat()
            except Exception:
                received_at = datetime.now(timezone.utc).isoformat()
                
            parsed_results.append({
                'source': 'gmail',
                'message_id': headers.get('message-id', msg_id),
                'thread_id': full_msg.get('threadId', ''),
                'sender': to_header,
                'sender_email': to_header,  # We store recipient in sender field for outgoing
                'subject': subject,
                'body_summary': full_msg.get('snippet', '')[:500],
                'received_at': received_at,
            })
            
        return parsed_results
    except Exception as e:
        print(f"Gmail sent search failed: {e}")
        return []


def search_outlook_sent(query: str, limit: int = 5) -> list:
    """Searches Outlook Sent Items folder for emails matching the query."""
    try:
        access_token = os.getenv("OUTLOOK_ACCESS_TOKEN")
        if not access_token:
            result = refresh_outlook_token(write_back=True)
            access_token = result["access_token"]

        headers = {"Authorization": f"Bearer {access_token}"}
        
        url = "https://graph.microsoft.com/v1.0/me/mailFolders/sentItems/messages"
        params = {
            "$top": limit,
            "$select": "id,subject,sentDateTime,toRecipients,bodyPreview,conversationId,internetMessageId",
            "$orderby": "sentDateTime DESC"
        }
        
        if query:
            # Note: Graph API uses $search parameter differently
            params["$search"] = f'"{query}"'

        response = requests.get(url, headers=headers, params=params, timeout=30)
        
        if response.status_code == 401:
            result = refresh_outlook_token(write_back=True)
            access_token = result["access_token"]
            headers["Authorization"] = f"Bearer {access_token}"
            response = requests.get(url, headers=headers, params=params, timeout=30)
            
        response.raise_for_status()
        messages = response.json().get("value", [])
        
        parsed_results = []
        for msg in messages:
            to_recipients = msg.get("toRecipients", [])
            to_header = ", ".join(r.get("emailAddress", {}).get("address", "") for r in to_recipients if r.get("emailAddress", {}).get("address"))
            
            parsed_results.append({
                'source': 'outlook',
                'message_id': msg.get('internetMessageId', msg.get('id')),
                'thread_id': msg.get('conversationId', ''),
                'sender': to_header,
                'sender_email': to_header,  # We store recipient in sender field for outgoing
                'subject': msg.get('subject', '(No Subject)'),
                'body_summary': msg.get('bodyPreview', '')[:500],
                'received_at': msg.get('sentDateTime'),
            })
            
        return parsed_results
    except Exception as e:
        print(f"Outlook sent search failed: {e}")
        return []
