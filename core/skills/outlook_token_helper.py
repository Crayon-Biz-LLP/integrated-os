import os
from pathlib import Path
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
SCOPES = os.getenv("OUTLOOK_SCOPES", "offline_access User.Read Mail.Read Mail.Send")

TOKEN_URL = f"https://login.microsoftonline.com/{TENANT_ID}/oauth2/v2.0/token"

def refresh_outlook_token(write_back: bool = True):
    if not all([TENANT_ID, CLIENT_ID, CLIENT_SECRET, REFRESH_TOKEN]):
        raise RuntimeError("Missing Outlook token env vars")

    payload = {
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "grant_type": "refresh_token",
        "refresh_token": REFRESH_TOKEN,
        "scope": SCOPES,
    }

    response = requests.post(TOKEN_URL, data=payload, timeout=30)
    response.raise_for_status()
    data = response.json()

    access_token = data.get("access_token")
    new_refresh_token = data.get("refresh_token") or REFRESH_TOKEN

    if not access_token:
        raise RuntimeError("No access token returned from refresh")

    if write_back:
        set_key(str(ENV_LOCAL), "OUTLOOK_ACCESS_TOKEN", access_token)
        set_key(str(ENV_LOCAL), "OUTLOOK_REFRESH_TOKEN", new_refresh_token)

    return {
        "access_token": access_token,
        "refresh_token": new_refresh_token,
        "expires_in": data.get("expires_in"),
        "scope": data.get("scope"),
        "token_type": data.get("token_type"),
    }

if __name__ == "__main__":
    result = refresh_outlook_token(write_back=True)
    print({
        "access_token_present": bool(result["access_token"]),
        "refresh_token_present": bool(result["refresh_token"]),
        "expires_in": result["expires_in"],
        "scope": result["scope"],
        "token_type": result["token_type"],
    })