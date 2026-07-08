"""Push notification service using Firebase Cloud Messaging HTTP v1 API.

Requires a Firebase Admin SDK service account JSON key stored in the
FIREBASE_SERVICE_ACCOUNT environment variable. If not set, push
notifications are silently skipped — the system degrades gracefully.

The ``device_tokens`` table stores FCM tokens registered by the Flutter app.
"""
import json
import os
from google.oauth2 import service_account
from google.auth.transport.requests import Request
import httpx

from core.lib.audit_logger import audit_log_sync
from core.services.db import get_supabase

_SCOPES = ["https://www.googleapis.com/auth/firebase.messaging"]


def _get_fcm_credentials():
    """Return service account credentials for FCM, or None if not configured."""
    raw = os.environ.get("FIREBASE_SERVICE_ACCOUNT")
    if not raw:
        return None
    try:
        info = json.loads(raw)
        creds = service_account.Credentials.from_service_account_info(info, scopes=_SCOPES)
        return creds
    except Exception as e:
        audit_log_sync("push", "WARNING", f"Failed to parse FIREBASE_SERVICE_ACCOUNT: {e}")
        return None


def _get_project_id(creds) -> str:
    """Return the Firebase project ID. Falls back to env var or extracted from credentials."""
    explicit = os.environ.get("FIREBASE_PROJECT_ID")
    if explicit:
        return explicit
    if creds and creds.project_id:
        return creds.project_id
    return "rhodey-os"


async def send_push_notification(
    title: str,
    body: str,
    data: dict | None = None,
) -> int:
    """Send a push notification to ALL registered device tokens.

    Args:
        title: Notification title (shown in lock screen / banner).
        body: Notification body text.
        data: Optional key-value pairs for the ``data`` payload (carried
              to the app even if the notification is tapped from background).

    Returns:
        Number of devices successfully notified.
    """
    creds = _get_fcm_credentials()
    if not creds:
        audit_log_sync("push", "INFO", "FIREBASE_SERVICE_ACCOUNT not set — skipping push")
        return 0

    # Get an access token (refreshes automatically if expired)
    try:
        request = Request()
        creds.refresh(request)
        access_token = creds.token
    except Exception as e:
        audit_log_sync("push", "ERROR", f"Failed to get FCM access token: {e}")
        return 0

    project_id = _get_project_id(creds)
    fcm_url = f"https://fcm.googleapis.com/v1/projects/{project_id}/messages:send"

    # Fetch all registered device tokens
    supabase = get_supabase()
    tokens_res = supabase.table("device_tokens").select("token,platform").execute()
    tokens = tokens_res.data if tokens_res and tokens_res.data else []
    if not tokens:
        audit_log_sync("push", "INFO", "No registered device tokens — skipping push")
        return 0

    success_count = 0
    invalid_tokens = []

    async with httpx.AsyncClient(timeout=15) as client:
        for entry in tokens:
            token = entry.get("token", "")
            platform = entry.get("platform", "android")
            if not token:
                continue

            payload = {"message": {"token": token, "notification": {"title": title, "body": body}}}

            # Platform-specific config
            if platform == "android":
                payload["message"]["android"] = {"priority": "high"}
            elif platform == "ios":
                payload["message"]["apns"] = {
                    "payload": {"aps": {"sound": "default", "badge": 1}}
                }

            # Add data payload if provided (carried to app on tap)
            if data:
                payload["message"]["data"] = {str(k): str(v) for k, v in data.items()}

            try:
                resp = await client.post(
                    fcm_url,
                    headers={
                        "Authorization": f"Bearer {access_token}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                )
                if resp.status_code == 200:
                    success_count += 1
                elif resp.status_code == 404:
                    # Token is invalid/unregistered — clean up
                    invalid_tokens.append(token)
                    audit_log_sync("push", "INFO", f"Removing invalid device token (404): {token[:20]}...")
                else:
                    # 401 (expired), 429 (rate limit), etc — log and continue
                    body_text = resp.text[:200]
                    audit_log_sync(
                        "push", "WARNING",
                        f"FCM send failed ({resp.status_code}): {body_text[:100]}",
                    )
            except httpx.TimeoutException:
                audit_log_sync("push", "WARNING", "FCM send timed out for a token — continuing")
            except Exception as e:
                audit_log_sync("push", "WARNING", f"FCM send error: {e}")

    # Clean up invalid tokens
    if invalid_tokens:
        try:
            supabase.table("device_tokens").delete().in_("token", invalid_tokens).execute()
            audit_log_sync("push", "INFO", f"Cleaned up {len(invalid_tokens)} invalid device tokens")
        except Exception as e:
            audit_log_sync("push", "WARNING", f"Failed to clean invalid tokens: {e}")

    return success_count
