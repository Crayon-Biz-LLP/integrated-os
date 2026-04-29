import os
import json
import secrets
import webbrowser
from urllib.parse import urlencode, urlparse, parse_qs
from http.server import BaseHTTPRequestHandler, HTTPServer

import requests
from dotenv import load_dotenv
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[2]
load_dotenv(BASE_DIR / ".env")
load_dotenv(BASE_DIR / ".env.local")

CLIENT_ID = os.getenv("OUTLOOK_CLIENT_ID")
CLIENT_SECRET = os.getenv("OUTLOOK_CLIENT_SECRET")
TENANT_ID = os.getenv("OUTLOOK_TENANT_ID")
REDIRECT_URI = os.getenv("OUTLOOK_REDIRECT_URI", "http://localhost:8765/callback")
SCOPES = os.getenv(
    "OUTLOOK_SCOPES",
    "offline_access User.Read Mail.Read Mail.Send"
)

print("CLIENT_ID:", os.getenv("OUTLOOK_CLIENT_ID"))
print("CLIENT_SECRET:", "SET" if os.getenv("OUTLOOK_CLIENT_SECRET") else None)
print("TENANT_ID:", os.getenv("OUTLOOK_TENANT_ID"))
if not CLIENT_ID or not CLIENT_SECRET or not TENANT_ID:
    raise RuntimeError("Missing OUTLOOK_CLIENT_ID / OUTLOOK_CLIENT_SECRET / OUTLOOK_TENANT_ID")

AUTHORITY = f"https://login.microsoftonline.com/{TENANT_ID}"
AUTHORIZE_URL = f"{AUTHORITY}/oauth2/v2.0/authorize"
TOKEN_URL = f"{AUTHORITY}/oauth2/v2.0/token"

state = secrets.token_urlsafe(24)
auth_result = {"code": None, "state": None, "error": None}


class CallbackHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path != "/callback":
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"Not found")
            return

        params = parse_qs(parsed.query)
        auth_result["code"] = params.get("code", [None])[0]
        auth_result["state"] = params.get("state", [None])[0]
        auth_result["error"] = params.get("error", [None])[0]

        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()

        if auth_result["code"]:
            self.wfile.write(b"<h2>Outlook auth successful.</h2><p>You can close this tab and return to the terminal.</p>")
        else:
            self.wfile.write(b"<h2>Outlook auth failed.</h2><p>Check terminal output.</p>")

    def log_message(self, format, *args):
        return


def main():
    query = {
        "client_id": CLIENT_ID,
        "response_type": "code",
        "redirect_uri": REDIRECT_URI,
        "response_mode": "query",
        "scope": SCOPES,
        "state": state,
    }

    auth_url = f"{AUTHORIZE_URL}?{urlencode(query)}"

    server = HTTPServer(("localhost", 8765), CallbackHandler)

    print("\nOpen this URL if the browser does not open automatically:\n")
    print(auth_url)
    print("\nWaiting for Microsoft login callback on http://localhost:8765/callback ...\n")

    webbrowser.open(auth_url)

    while auth_result["code"] is None and auth_result["error"] is None:
        server.handle_request()

    server.server_close()

    if auth_result["error"]:
        raise RuntimeError(f"Authorization failed: {auth_result['error']}")

    if auth_result["state"] != state:
        raise RuntimeError("State mismatch. Aborting.")

    code = auth_result["code"]

    token_data = {
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": REDIRECT_URI,
        "scope": SCOPES,
    }

    response = requests.post(TOKEN_URL, data=token_data, timeout=30)
    response.raise_for_status()
    tokens = response.json()

    print("\n=== TOKENS RECEIVED ===\n")
    print(json.dumps({
        "access_token_present": bool(tokens.get("access_token")),
        "refresh_token_present": bool(tokens.get("refresh_token")),
        "expires_in": tokens.get("expires_in"),
        "scope": tokens.get("scope"),
        "token_type": tokens.get("token_type"),
    }, indent=2))

    print("\n=== SAVE THESE IN .env.local ===\n")
    print(f"OUTLOOK_ACCESS_TOKEN={tokens.get('access_token', '')}")
    print(f"OUTLOOK_REFRESH_TOKEN={tokens.get('refresh_token', '')}")

    with open("outlook_tokens.json", "w") as f:
        json.dump(tokens, f, indent=2)

    print("\nFull token response also saved to outlook_tokens.json\n")


if __name__ == "__main__":
    main()