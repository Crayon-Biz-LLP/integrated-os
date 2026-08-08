"""M11 sign-in plumbing: email/OTP + Google identity → per-user API key.

Design invariants
-----------------
1. The API key is ISSUED at first successful sign-in and returned once.
   Only the sha256 hash is stored (users.api_key_hash) — never the
   plaintext. Signing in again on a new device rotates the key (the old
   device re-signs in). Per-device keys are future work.
2. OTP rows live in `login_otps` (auth state, NOT tenant data) and are
   written through the RAW supabase client (`get_supabase()`) because
   they are created before a tenant context exists.
3. Anti-enumeration: `/otp/send` answers the same way whether or not
   the email is provisioned, and only actually sends when it is.
4. All secrets compare with `hmac.compare_digest` / constant-time hashes.
"""

import hashlib
import hmac
import os
import secrets
import time
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import urlencode

import httpx

from core.services.db import get_supabase, hash_api_key

# ── Constants ──────────────────────────────────────────────────────────────

OTP_TTL_SECONDS = 10 * 60          # code validity
OTP_MAX_ATTEMPTS = 5               # wrong tries before forcing a resend
OTP_RESEND_GAP_SECONDS = 60        # rate limit between sends per email
_OTP_PEPPER = os.getenv("OTP_PEPPER", "rhodey-otp-v1")

# ── Email/OTP flow ─────────────────────────────────────────────────────────


def _hash_code(email: str, code: str) -> str:
    """Peppered sha256 of the code — a DB leak exposes no usable OTPs."""
    return hashlib.sha256(f"{email}:{code}:{_OTP_PEPPER}".encode()).hexdigest()


def _find_user_by_email(email: str) -> Optional[dict]:
    """Active user row for an email, or None. Case-insensitive match."""
    try:
        res = (
            get_supabase()
            .table("users")
            .select("id, name, email, status")
            .ilike("email", email.strip().lower())
            .limit(1)
            .maybe_single()
            .execute()
        )
        return res.data if res.data else None
    except Exception as e:
        from core.lib.audit_logger import audit_log_sync

        audit_log_sync("auth", "WARNING", f"_find_user_by_email failed: {e}")
        return None


def _latest_otp(email: str) -> Optional[dict]:
    try:
        res = (
            get_supabase()
            .table("login_otps")
            .select("id, code_hash, attempts, expires_at, consumed_at, created_at")
            .eq("email", email.strip().lower())
            .order("created_at", desc=True)
            .limit(1)
            .execute()
        )
        rows = res.data or []
        return rows[0] if rows else None
    except Exception:
        return None


def send_otp(email: str) -> dict:
    """Request a sign-in code. Response is identical whether invited or not.

    Only actually emails when the address belongs to an active user —
    this is what makes the invite model work: Danny provisions the
    address, the user signs in with it.
    """
    email = (email or "").strip().lower()
    if not email or "@" not in email or "." not in email.split("@")[-1]:
        return {"ok": False, "message": "Enter a valid email address."}

    now = time.time()
    latest = _latest_otp(email)
    if latest and latest.get("consumed_at") is None:
        try:
            created = datetime.fromisoformat(latest["created_at"].replace("Z", "+00:00"))
            if (now - created.timestamp()) < OTP_RESEND_GAP_SECONDS:
                return {
                    "ok": False,
                    "message": "A code was just sent — wait a minute before requesting another.",
                }
        except Exception:
            pass  # tolerate bad rows; the resend proceeds

    user = _find_user_by_email(email)
    code = f"{secrets.randbelow(10**6):06d}"
    try:
        get_supabase().table("login_otps").insert(
            {
                "email": email,
                "code_hash": _hash_code(email, code),
                "attempts": 0,
                "expires_at": (
                    datetime.now(timezone.utc) + timedelta(seconds=OTP_TTL_SECONDS)
                ).isoformat(),
            }
        ).execute()
    except Exception as e:
        from core.lib.audit_logger import audit_log_sync

        audit_log_sync("auth", "ERROR", f"OTP insert failed for {email}: {e}")
        return {"ok": False, "message": "Could not start sign-in right now — please retry."}

    if user and user.get("status", "active") == "active":
        from core.services.otp_email import send_otp_email

        sent = send_otp_email(email, code)
        if not sent:
            return {
                "ok": False,
                "message": "Email sign-in isn't configured yet — use Google, or ask your admin.",
            }

    # Same message for invited and uninvited: never leak who's provisioned.
    return {
        "ok": True,
        "message": "If an account exists for that email, a 6-digit code is on its way.",
    }


def verify_otp(email: str, code: str) -> dict:
    """Validate a code, mark it consumed, and issue the tenant's API key.

    Anti-enumeration: every "wrong" outcome (unknown email, no code,
    wrong code, reused code) returns the SAME message — callers cannot
    tell which emails are provisioned.
    """
    email = (email or "").strip().lower()
    code = (code or "").strip()
    if not email or not code:
        return {"ok": False, "message": "Email and code are both required."}

    user = _find_user_by_email(email)
    if not user or user.get("status", "active") != "active":
        return {"ok": False, "message": "Invalid code or email."}

    otp = _latest_otp(email)
    if not otp or otp.get("consumed_at"):
        return {"ok": False, "message": "Invalid code or email."}
    try:
        expires = datetime.fromisoformat(otp["expires_at"].replace("Z", "+00:00"))
        if datetime.now(timezone.utc) > expires:
            return {"ok": False, "message": "That code expired — request a new one."}
    except Exception:
        pass  # malformed expiry: fall through to attempt check

    attempts = int(otp.get("attempts") or 0)
    if attempts >= OTP_MAX_ATTEMPTS:
        return {"ok": False, "message": "Too many wrong attempts — request a new code."}

    # Increment attempts BEFORE validating (burn one try either way).
    try:
        get_supabase().table("login_otps").update({"attempts": attempts + 1}).eq(
            "id", otp["id"]
        ).execute()
    except Exception:
        pass

    if not hmac.compare_digest(otp.get("code_hash", ""), _hash_code(email, code)):
        return {"ok": False, "message": "Invalid code or email."}

    try:
        get_supabase().table("login_otps").update(
            {
                "consumed_at": datetime.now(timezone.utc).isoformat(),
                "attempts": attempts + 1,
            }
        ).eq("id", otp["id"]).execute()
    except Exception:
        pass

    api_key = issue_api_key(user["id"])
    if not api_key:
        return {"ok": False, "message": "Could not complete sign-in — please try again."}
    return {"ok": True, "api_key": api_key, "name": user.get("name", "") or ""}


# ── Key issuance ───────────────────────────────────────────────────────────


def issue_api_key(user_id: str) -> Optional[str]:
    """Generate a fresh key, store its hash, return the plaintext ONCE.

    Returns None when the hash could not be persisted — callers must
    surface a retry error instead of handing the app a dead key.
    """
    key = secrets.token_hex(32)  # 64 hex chars
    try:
        get_supabase().table("users").update(
            {"api_key_hash": hash_api_key(key)}
        ).eq("id", user_id).execute()
    except Exception as e:
        from core.lib.audit_logger import audit_log_sync

        audit_log_sync("auth", "ERROR", f"issue_api_key failed for {user_id}: {e}")
        return None
    return key


# ── Google identity flow ───────────────────────────────────────────────────

GOOGLE_IDENTITY_SCOPES = "openid email profile"
_GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
_GOOGLE_TOKENINFO_URL = "https://oauth2.googleapis.com/tokeninfo"


def build_google_identity_url(client_id: str, redirect_uri: str, state: str) -> str:
    """Consent URL asking ONLY for identity (no calendar/gmail scopes)."""
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": GOOGLE_IDENTITY_SCOPES,
        "prompt": "select_account",
        "state": state,
    }
    return "https://accounts.google.com/o/oauth2/auth?" + urlencode(params)


async def exchange_google_identity(
    code: str, client_id: str, client_secret: str, redirect_uri: str
) -> Optional[dict]:
    """Exchange the code for a verified Google identity.

    Returns {"email", "name"} or None on any failure. The id_token is
    validated via Google's tokeninfo endpoint (issuer/audience checks
    happen on Google's side). `redirect_uri` MUST equal the one used in
    the consent URL (the registered HTTPS callback).
    """
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            tok = await client.post(
                _GOOGLE_TOKEN_URL,
                data={
                    "code": code,
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "redirect_uri": redirect_uri,
                    "grant_type": "authorization_code",
                },
            )
            if tok.status_code != 200:
                return None
            id_token = tok.json().get("id_token")
            if not id_token:
                return None

            info = await client.get(
                _GOOGLE_TOKENINFO_URL, params={"id_token": id_token}
            )
            if info.status_code != 200:
                return None
            data = info.json()
            if data.get("email_verified") != "true":
                return None
            return {
                "email": (data.get("email") or "").strip().lower(),
                "name": data.get("name") or "",
            }
    except Exception:
        return None


def signin_by_google_identity(identity: dict) -> dict:
    """Match a verified Google identity to a provisioned tenant + issue key."""
    email = (identity or {}).get("email", "")
    user = _find_user_by_email(email) if email else None
    if not user or user.get("status", "active") != "active":
        return {"ok": False, "message": "This Google account isn't invited to Rhodey yet."}
    api_key = issue_api_key(user["id"])
    if not api_key:
        return {"ok": False, "message": "Could not complete sign-in — please try again."}
    return {
        "ok": True,
        "api_key": api_key,
        "name": user.get("name", "") or "",
        "email": email,
    }
