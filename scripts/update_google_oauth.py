#!/usr/bin/env python3
"""
Update Google OAuth to include Docs API scope.
"""

import os
import re
import webbrowser
from pathlib import Path
from dotenv import load_dotenv

env_path = Path(__file__).parent.parent / ".env"
if env_path.exists():
    load_dotenv(env_path)

CLIENT_ID = os.environ["GOOGLE_CLIENT_ID"]
CLIENT_SECRET = os.environ["GOOGLE_CLIENT_SECRET"]
REDIRECT_URI = "http://localhost:8080"

SCOPES = " ".join([
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/tasks",
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/drive.file",
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/spreadsheets.readonly",
])


def main():
    import requests

    # Step 1: Build auth URL manually
    params = {
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": SCOPES,
        "access_type": "offline",
        "prompt": "consent",
    }
    from urllib.parse import urlencode
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

    print("\n=== NEW GOOGLE_REFRESH_TOKEN ===")
    print(data["refresh_token"])
    print("===================================")
    print("\nUpdate GOOGLE_REFRESH_TOKEN in GitHub secrets.")
    print("Scopes:", data.get("scope", "unknown"))


if __name__ == "__main__":
    main()
