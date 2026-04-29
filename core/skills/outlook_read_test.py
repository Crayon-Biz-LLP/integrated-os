import os
from pathlib import Path
import requests
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parents[2]
load_dotenv(BASE_DIR / ".env")
load_dotenv(BASE_DIR / ".env.local")

ACCESS_TOKEN = os.getenv("OUTLOOK_ACCESS_TOKEN")

if not ACCESS_TOKEN:
    raise RuntimeError("Missing OUTLOOK_ACCESS_TOKEN")

url = "https://graph.microsoft.com/v1.0/me/mailFolders/inbox/messages"
params = {
    "$top": 5,
    "$select": "id,subject,receivedDateTime,from,bodyPreview,conversationId,isRead",
    "$orderby": "receivedDateTime DESC"
}
headers = {
    "Authorization": f"Bearer {ACCESS_TOKEN}",
    "Accept": "application/json"
}

print("Calling Microsoft Graph...")
response = requests.get(url, headers=headers, params=params, timeout=30)

print("Status code:", response.status_code)
print("Response text preview:", response.text[:500])

response.raise_for_status()
data = response.json()
messages = data.get("value", [])

print("Message count:", len(messages))

if not messages:
    print("No inbox messages found.")
else:
    print(f"Found {len(messages)} inbox messages:\n")
    for i, msg in enumerate(messages, start=1):
        sender = (((msg.get("from") or {}).get("emailAddress") or {}).get("address")) or "unknown"
        subject = msg.get("subject") or "(No Subject)"
        received = msg.get("receivedDateTime") or "unknown time"
        is_read = msg.get("isRead")
        print(f"{i}. [{received}] {'READ' if is_read else 'UNREAD'}")
        print(f"   From: {sender}")
        print(f"   Subject: {subject}")
        print()