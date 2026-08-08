"""One-time migration: Danny's env OUTLOOK_REFRESH_TOKEN → user_oauth_tokens.

With Outlook token resolution now tenant-scoped (strict: a tenant context
never falls back to the env token), Danny's Outlook would stop flowing
unless his token is stored per-tenant. This moves it into
user_oauth_tokens (provider='outlook') so his calendar/ingest keeps working
and tenant #2+ can never read it.

Usage:  set -a && . ./.env && set +a
        PYTHONPATH=. python3 scripts/migrate_danny_outlook_token.py
"""
import os
import sys

sys.path.insert(0, ".")
from core.services.db import get_supabase

DANNY_UID = os.getenv("TENANT1_UID") or "c302706e-fe61-422a-b384-68e3bc8f6f8e"


def main() -> int:
    refresh = os.getenv("OUTLOOK_REFRESH_TOKEN")
    if not refresh:
        print("❌ OUTLOOK_REFRESH_TOKEN not set in env — nothing to migrate.")
        return 1

    db = get_supabase()
    existing = (
        db.table("user_oauth_tokens")
        .select("id, user_id, refresh_token")
        .eq("user_id", DANNY_UID)
        .eq("provider", "outlook")
        .execute()
    )
    rows = existing.data or []
    if rows:
        db.table("user_oauth_tokens") \
            .update({"refresh_token": refresh}) \
            .eq("user_id", DANNY_UID) \
            .eq("provider", "outlook") \
            .execute()
        print(f"✅ Updated existing outlook token row for {DANNY_UID[:8]}...")
    else:
        db.table("user_oauth_tokens").insert({
            "user_id": DANNY_UID,
            "provider": "outlook",
            "refresh_token": refresh,
        }).execute()
        print(f"✅ Inserted outlook token row for {DANNY_UID[:8]}...")
    return 0


if __name__ == "__main__":
    sys.exit(main())
