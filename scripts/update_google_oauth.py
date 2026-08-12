#!/usr/bin/env python3
"""
update_google_oauth.py — connect a TENANT's Google account (M5).

Replaces the old single-global-token flow: the refresh token is now stored
PER USER in `user_oauth_tokens` (db/84), and users.google_connected is
flipped on. google_service.get_google_creds() reads it per tenant.

Usage:
    python scripts/update_google_oauth.py --user "Priya" [--dsn postgresql://...] [--apply]

    --user   the tenant display name (must exist in public.users)
    --dsn    override connection (local copy DB); otherwise discovered from .env
    --apply  actually write the token (default is dry-run: exchange + print only)

Flow (unchanged from the old script): opens the Google consent URL, you
approve, paste the localhost redirect URL back, the code is exchanged for
tokens, and the refresh token is stored for that user.

Safety: dry-run by default — pass --apply to write to the DB.
"""

import argparse
import os
import re
import sys
import webbrowser
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

CLIENT_ID = None
CLIENT_SECRET = None
REDIRECT_URI = "http://localhost:8080"

SCOPES = " ".join([
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/tasks",
    "https://www.googleapis.com/auth/gmail.modify",
    # Full Drive access (NOT drive.file): call_ingest and renew_drive_channel
    # watch a manually-created folder (Crayon/Rhodey OS/Call Recordings).
    # drive.file only exposes files the app itself created/opened, so a
    # hand-made folder returns 404 — which silently killed call ingest on
    # 2026-08-06 when the token was re-issued. The consent screen will now
    # ask for full Drive access.
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/spreadsheets.readonly",
])


def _psql_bin() -> str:
    import shutil
    found = shutil.which("psql")
    if found:
        return found
    for candidate in (
        "/opt/homebrew/opt/postgresql@17/bin/psql",
        "/opt/homebrew/opt/libpq/bin/psql",
    ):
        if os.path.exists(candidate):
            return candidate
    raise SystemExit("❌ psql not found on PATH. Install with: brew install libpq (or postgresql@17)")


def _psql(sql: str, dsn: str, password: str | None) -> str:
    import subprocess
    env = {**os.environ}
    if password:
        env["PGPASSWORD"] = password
    r = subprocess.run(
        [_psql_bin(), dsn, "-tAc", sql], env=env, capture_output=True, text=True, timeout=120,
    )
    if r.returncode != 0:
        raise SystemExit(f"❌ psql failed:\n{r.stderr[-2000:]}")
    return r.stdout.strip()


def main():
    global CLIENT_ID, CLIENT_SECRET
    parser = argparse.ArgumentParser(description="Connect a tenant's Google account (M5 per-user OAuth)")
    parser.add_argument("--user", required=True, help="Tenant display name (public.users.name)")
    parser.add_argument("--dsn", default=None, help="Override connection (local copy DB)")
    parser.add_argument("--apply", action="store_true", help="Write token to DB (default dry-run)")
    args = parser.parse_args()

    env_path = ROOT / ".env"
    if env_path.exists():
        load_dotenv(env_path)

    CLIENT_ID = os.environ["GOOGLE_CLIENT_ID"]
    CLIENT_SECRET = os.environ["GOOGLE_CLIENT_SECRET"]

    dsn, password = (args.dsn, None)
    if not dsn:
        from backup_supabase import discover_conn
        dsn, password = discover_conn()

    # Resolve the user id for the tenant name (must exist — created by bootstrap_tenant.py).
    uid = _psql(
        f"select id from public.users where name = '{args.user.replace(chr(39), chr(39)*2)}' limit 1",
        dsn, password,
    )
    if not uid:
        raise SystemExit(f"❌ No user named '{args.user}' — create them first via scripts/bootstrap_tenant.py")
    print(f"👤 tenant: {args.user} → {uid}")

    import requests

    # Step 1: Build auth URL manually
    from urllib.parse import urlencode
    params = {
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": SCOPES,
        "access_type": "offline",
        "prompt": "consent",
    }
    auth_url = f"https://accounts.google.com/o/oauth2/auth?{urlencode(params)}"

    print("1. Opening browser for Google OAuth...")
    print(f"   If it doesn't open, go to:\n{auth_url}\n")
    webbrowser.open(auth_url)

    print("2. After approving, the browser will try to redirect to localhost.")
    print("   It will show 'connection refused' — that's fine.")
    print("   Copy the FULL URL from the address bar and paste it below.")
    print(f"   It should start with: {REDIRECT_URI}/?code=")
    print()
    callback_url = input("Paste the full redirect URL: ").strip()

    # Step 2: Extract the code
    match = re.search(r"[?&]code=([^&]+)", callback_url)
    if not match:
        print("❌ Could not find 'code' in the URL. Make sure you pasted the full redirect URL.")
        return
    code = match.group(1)

    # Step 3: Exchange code for tokens
    print("\n3. Exchanging code for tokens...")
    resp = requests.post("https://oauth2.googleapis.com/token", data={
        "code": code,
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "redirect_uri": REDIRECT_URI,
        "grant_type": "authorization_code",
    })
    data = resp.json()
    if "refresh_token" not in data:
        print(f"❌ Token exchange failed: {data.get('error', resp.text)}")
        return

    refresh_token = data["refresh_token"]
    scopes = data.get("scope", "unknown")
    print(f"✅ Token exchanged (scopes: {scopes})")

    if not args.apply:
        print("\n(dry-run — pass --apply to store this token in user_oauth_tokens)")
        return

    # Step 4: Store per-user (db/84): upsert the google token + mark connected.
    _psql(
        "insert into public.user_oauth_tokens (user_id, provider, refresh_token, scopes) "
        f"values ('{uid}', 'google', '{refresh_token.replace(chr(39), chr(39)*2)}', '{scopes.replace(chr(39), chr(39)*2)}') "
        "on conflict (user_id, provider) do update set "
        "refresh_token = excluded.refresh_token, scopes = excluded.scopes, updated_at = now()",
        dsn, password,
    )
    _psql(
        f"update public.users set google_connected = true where id = '{uid}'",
        dsn, password,
    )
    # Drop any in-process cached credentials so the updated token takes
    # effect without a container restart (M5 review fix).
    try:
        from core.services.google_service import clear_google_creds_cache
        clear_google_creds_cache()
        print("  (cached google creds cleared)")
    except Exception:
        pass
    print(f"✅ Stored Google refresh token for {args.user} (user_oauth_tokens + users.google_connected)")


if __name__ == "__main__":
    main()
