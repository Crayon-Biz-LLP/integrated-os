import os
import uuid

from core.services.google_service import get_cached_service

GOOGLE_DRIVE_CALLS_FOLDER_ID = os.getenv("GOOGLE_DRIVE_CALLS_FOLDER_ID")
WEBHOOK_BASE_URL = os.getenv("WEBHOOK_BASE_URL", "")

CHANNEL_ID = os.getenv("DRIVE_WATCH_CHANNEL_ID", "integrated-os-call-ingest")

def renew_channel():
    folder_id = GOOGLE_DRIVE_CALLS_FOLDER_ID
    webhook_url = f"{WEBHOOK_BASE_URL}/api/drive-webhook"

    if not folder_id:
        print("ERROR: GOOGLE_DRIVE_CALLS_FOLDER_ID not set")
        return

    if not WEBHOOK_BASE_URL:
        print("ERROR: WEBHOOK_BASE_URL not set")
        return

    address = webhook_url
    channel_id = f"{CHANNEL_ID}-{uuid.uuid4().hex[:8]}"

    service = get_cached_service("drive", "v3")
    if service is None:
        print("ERROR: no Google creds for this tenant (M5) — drive channel renewal skipped")
        return

    body = {
        "id": channel_id,
        "type": "web_hook",
        "address": address,
        "token": os.getenv("PULSE_SECRET", ""),
        "payload": True
    }

    try:
        response = service.channels().stop(body={"id": channel_id, "resourceId": ""}).execute()
    except Exception:
        pass

    response = service.files().watch(fileId=folder_id, body=body).execute()
    expiration = response.get("expiration", "unknown")
    resource_id = response.get("resourceId", "unknown")
    print(f"Channel created: {channel_id}")
    print(f"  Resource ID: {resource_id}")
    print(f"  Expiration: {expiration}")
    print(f"  Webhook URL: {address}")
    print(f"  Expires at: {expiration}")

    return {
        "channel_id": channel_id,
        "resource_id": resource_id,
        "expiration": expiration
    }


if __name__ == "__main__":
    renew_channel()
