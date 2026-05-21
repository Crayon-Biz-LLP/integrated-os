"""
Google OAuth Setup — generates auth URL with ALL scopes used by Rhodey,
starts a local server to catch the callback, and prints the refresh token.

Usage:
    python core/skills/oauth_setup.py

Loads GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET from .env file or env vars.
"""
import os
import re

_env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", ".env")
if os.path.exists(_env_path):
    with open(_env_path) as f:
        for line in f:
            m = re.match(r'^\s*(\w+)=(.*)$', line.strip())
            if m and m.group(1) not in os.environ:
                val = m.group(2).strip().strip('"').strip("'")
                os.environ[m.group(1)] = val
import sys
import json
import http.server
import urllib.parse
import webbrowser
import threading
import httpx

SCOPES = [
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/tasks",
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/spreadsheets.readonly",
    "https://www.googleapis.com/auth/drive.readonly",
]

REDIRECT_PORT = 8080
REDIRECT_URI = f"http://localhost:{REDIRECT_PORT}/callback"

auth_code = None
event = threading.Event()


class CallbackHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        global auth_code
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)
        if "code" in params:
            auth_code = params["code"][0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(
                "<html><body><h1>Authorization received!</h1>"
                "<p>You can close this tab and return to the terminal.</p></body></html>".encode()
            )
            event.set()
        else:
            error = params.get("error", ["unknown"])[0]
            self.send_response(400)
            self.end_headers()
            self.wfile.write(f"<h1>Error: {error}</h1>".encode())
            event.set()

    def log_message(self, format, *args):
        pass


def main():
    client_id = os.getenv("GOOGLE_CLIENT_ID")
    client_secret = os.getenv("GOOGLE_CLIENT_SECRET")

    if not client_id or not client_secret:
        print("Missing GOOGLE_CLIENT_ID or GOOGLE_CLIENT_SECRET")
        print("Set them as env vars or pass inline:")
        print("  GOOGLE_CLIENT_ID=xxx GOOGLE_CLIENT_SECRET=yyy python core/skills/oauth_setup.py")
        sys.exit(1)

    scope_str = " ".join(SCOPES)

    auth_url = (
        "https://accounts.google.com/o/oauth2/v2/auth?"
        f"client_id={client_id}&"
        f"redirect_uri={REDIRECT_URI}&"
        "response_type=code&"
        f"scope={urllib.parse.quote(scope_str)}&"
        "access_type=offline&"
        "prompt=consent"
    )

    print("=" * 60)
    print("Google OAuth Setup — Rhodey")
    print("=" * 60)
    print()
    print(f"Loaded credentials for client: {client_id[:40]}...")
    if client_secret:
        print(f"Client secret: {client_secret[:12]}... (length: {len(client_secret)})")
    print()
    print("Scopes being requested:")
    for s in SCOPES:
        name = s.split("/")[-1]
        print(f"  • {name}")
    print()
    print(f"Starting local server on port {REDIRECT_PORT}...")

    server = http.server.HTTPServer(("localhost", REDIRECT_PORT), CallbackHandler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()

    print("Opening browser for authorization...")
    webbrowser.open(auth_url)
    print()
    print(f"If browser doesn't open, visit this URL:")
    print(f"  {auth_url}")
    print()

    event.wait(timeout=300)
    server.shutdown()

    if not auth_code:
        print("❌ No authorization code received. Timed out after 5 minutes.")
        sys.exit(1)

    print("✅ Authorization code received. Exchanging for tokens...")

    token_url = "https://oauth2.googleapis.com/token"
    payload = {
        "code": auth_code[:20] + "...",
        "client_id": client_id,
        "client_secret": client_secret[:8] + "..." if client_secret else None,
        "redirect_uri": REDIRECT_URI,
        "grant_type": "authorization_code",
    }
    print(f"Debug - token exchange payload: {json.dumps(payload)}")
    print(f"Debug - redirect_uri: {REDIRECT_URI}")

    payload = {
        "code": auth_code,
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": REDIRECT_URI,
        "grant_type": "authorization_code",
    }

    with httpx.Client() as client:
        resp = client.post(token_url, data=payload)
    if resp.status_code != 200:
        print(f"❌ Token exchange failed: {resp.status_code} {resp.text}")
        print(f"   Request URL: {token_url}")
        sys.exit(1)

    tokens = resp.json()
    refresh_token = tokens.get("refresh_token")

    if not refresh_token:
        print("❌ No refresh_token in response.")
        print("   This usually means the account has already granted these scopes.")
        print("   Try revoking first: https://myaccount.google.com/permissions")
        print()
        print("Full response:")
        print(json.dumps(tokens, indent=2))
        sys.exit(1)

    print()
    print("=" * 60)
    print("✅ SUCCESS — Set these in your environment:")
    print("=" * 60)
    print()
    print(f"  GOOGLE_REFRESH_TOKEN={refresh_token}")
    print()
    print("Scopes granted:")
    for s in SCOPES:
        print(f"  • {s.split('/')[-1]}")
    print()
    print("If you deploy to Vercel/GitHub, update GOOGLE_REFRESH_TOKEN in secrets.")
    print()


if __name__ == "__main__":
    main()
