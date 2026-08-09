import os
from pathlib import Path
from datetime import datetime, timezone
import requests
from dotenv import load_dotenv, set_key

BASE_DIR = Path(__file__).resolve().parents[2]
ENV_LOCAL = BASE_DIR / ".env.local"

load_dotenv(BASE_DIR / ".env")
load_dotenv(ENV_LOCAL)

TENANT_ID = os.getenv("OUTLOOK_TENANT_ID")
CLIENT_ID = os.getenv("OUTLOOK_CLIENT_ID")
CLIENT_SECRET = os.getenv("OUTLOOK_CLIENT_SECRET")
REFRESH_TOKEN = os.getenv("OUTLOOK_REFRESH_TOKEN")
SCOPES = os.getenv("OUTLOOK_SCOPES", "offline_access User.Read Mail.Read Mail.Send Chat.Read Files.Read.All")

TOKEN_URL = f"https://login.microsoftonline.com/{TENANT_ID}/oauth2/v2.0/token"


def _resolve_uid(user_id: str | None) -> str | None:
    """Explicit user id → active tenant context (never resolve inside a call
    that the contextvar governs — same rule as google_service)."""
    if user_id:
        return user_id
    try:
        from core.services.db import get_tenant
        return get_tenant()
    except Exception:
        return None


def get_outlook_refresh_token(user_id: str | None = None) -> str | None:
    """The tenant's Outlook refresh token (M — tenant hardening).

    Resolution order (mirrors core.services.google_service.get_refresh_token):
      1. `user_id` (or the active tenant context) → user_oauth_tokens row
         (provider='outlook')
      2. OUTLOOK_REFRESH_TOKEN env — legacy single-user mode ONLY, and ONLY
         when no tenant context is active (scripts / CLI / pre-db/84).

    Returns None when neither exists — callers then SKIP Outlook work.

    A tenant-scoped call NEVER falls back to the env token: that env var is
    tenant #1's legacy token, and using it from tenant #2's scope would read
    tenant #1's mailbox (the exact cross-tenant leak class already fixed for
    Google). Tenants without an Outlook row simply get no Outlook.
    """
    uid = _resolve_uid(user_id)
    if uid:
        try:
            from core.services.db import get_supabase, maybe_single_safe
            res = maybe_single_safe(
                get_supabase()
                .table("user_oauth_tokens")
                .select("refresh_token")
                .eq("user_id", uid)
                .eq("provider", "outlook")
                .limit(1)
            )
            # maybe_single_safe returns data as a dict (single row) OR a list
            # (one-element list in some client versions) — handle both.
            data = (res.data if res and res.data else None)
            if isinstance(data, list):
                data = data[0] if data else None
            token = (data or {}).get("refresh_token") if isinstance(data, dict) else None
            if token:
                return str(token)
        except Exception:
            # Table missing pre-db/84 — treat as no-token (never fall back
            # to another tenant's env token from a tenant-scoped call).
            return None
        # A tenant context IS active but has no Outlook row — return None so
        # callers skip Outlook work. Do NOT fall through to the env token.
        return None
    # No tenant context at all → legacy single-user env mode (scripts/CLI).
    return os.getenv("OUTLOOK_REFRESH_TOKEN")


def _write_back_token(new_refresh: str, access_token: str, uid: str | None) -> None:
    """Persist a refreshed token pair: user_oauth_tokens row when the token
    came from a tenant row, else the legacy .env.local file."""
    if uid:
        try:
            from core.services.db import get_supabase
            get_supabase().table("user_oauth_tokens").update({
                "refresh_token": new_refresh,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }).eq("user_id", uid).eq("provider", "outlook").execute()
            return
        except Exception:
            pass  # row gone / write failed → fall through to env.local
    set_key(str(ENV_LOCAL), "OUTLOOK_REFRESH_TOKEN", new_refresh)
    set_key(str(ENV_LOCAL), "OUTLOOK_ACCESS_TOKEN", access_token)


def refresh_outlook_token(write_back: bool = True, user_id: str | None = None):
    """Refresh (or return) an Outlook access token for the ACTIVE TENANT.

    Token resolution is tenant-scoped (get_outlook_refresh_token): a tenant
    without an Outlook row gets None back — callers must skip cleanly (they
    must NOT crash or fall back to another tenant's credential).

    Returns None when no refresh token is resolvable for the current scope.
    """
    uid = _resolve_uid(user_id)
    refresh = get_outlook_refresh_token(uid)  # pass uid: avoid re-resolving
    if not refresh:
        return None

    if not all([TENANT_ID, CLIENT_ID, CLIENT_SECRET]):
        raise RuntimeError("Missing Outlook client env vars (OUTLOOK_TENANT_ID/CLIENT_ID/CLIENT_SECRET)")

    payload = {
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "grant_type": "refresh_token",
        "refresh_token": refresh,
        "scope": SCOPES,
    }

    response = requests.post(TOKEN_URL, data=payload, timeout=30)
    response.raise_for_status()
    data = response.json()

    access_token = data.get("access_token")
    new_refresh_token = data.get("refresh_token") or refresh

    if not access_token:
        raise RuntimeError("No access token returned from refresh")

    if write_back:
        _write_back_token(new_refresh_token, access_token, uid)

    return {
        "access_token": access_token,
        "refresh_token": new_refresh_token,
        "expires_in": data.get("expires_in"),
        "scope": data.get("scope"),
        "token_type": data.get("token_type"),
    }


def get_outlook_access_token(user_id: str | None = None) -> str | None:
    """An Outlook access token for the ACTIVE TENANT (or None).

    Replacement for the old `os.getenv("OUTLOOK_ACCESS_TOKEN")` reads in
    webhook/email.py and email_search.py: that env var is tenant #1's legacy
    token, so reading it from tenant #2's scope used tenant #1's credential
    (cross-tenant mailbox access, Aug 9 audit). Resolution mirrors
    get_outlook_refresh_token:
      1. tenant context (or explicit user_id) → refresh via the per-tenant
         row; None when the tenant has no Outlook row.
      2. NO tenant context (legacy scripts/CLI) → env OUTLOOK_ACCESS_TOKEN
         first (pre-cached), else refresh from env refresh token.

    A tenant-scoped call NEVER falls back to the env token.
    """
    uid = _resolve_uid(user_id)
    if uid:
        result = refresh_outlook_token(write_back=True, user_id=uid)
        if result:
            return result.get("access_token")
        return None
    # Legacy unscoped mode: the env access token may already be cached;
    # otherwise refresh from the env refresh token.
    cached = os.getenv("OUTLOOK_ACCESS_TOKEN")
    if cached:
        return cached
    result = refresh_outlook_token(write_back=True)
    if result:
        return result.get("access_token")
    return None


if __name__ == "__main__":
    result = refresh_outlook_token(write_back=True)
    if not result:
        print("No Outlook refresh token resolvable in this scope — nothing to refresh.")
    else:
        print({
            "access_token_present": bool(result["access_token"]),
            "refresh_token_present": bool(result["refresh_token"]),
            "expires_in": result["expires_in"],
            "scope": result["scope"],
            "token_type": result["token_type"],
        })
