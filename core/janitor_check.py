"""
Rhodey Janitor — heartbeat health check.

Runs every 30 minutes via GitHub Actions. Checks pipeline health:
- Stalled raw_dumps (stuck in 'pending'/'staged' > 2 hours)
- Unresolved failed_queue items
- Recent errors in audit_logs

Alerts via Telegram if issues found. Silent if healthy.
"""
import os
import sys
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
from supabase import create_client
import httpx

load_dotenv(os.path.join(os.path.dirname(__file__), '..', '.env'))

supabase = create_client(
    os.getenv("SUPABASE_URL"),
    os.getenv("SUPABASE_SERVICE_ROLE_KEY")
)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# IST business hours: 9 AM to 10 PM IST = 3:30 UTC to 16:30 UTC
IST_OFFSET = timedelta(hours=5, minutes=30)
BIZ_START_UTC = 3  # 3:30 UTC = 9 AM IST
BIZ_END_UTC = 17   # 17:00 UTC = 10:30 PM IST (buffer)


def is_business_hours():
    now_utc = datetime.now(timezone.utc)
    hour = now_utc.hour + (now_utc.minute / 60)
    return BIZ_START_UTC <= hour <= BIZ_END_UTC


async def send_telegram_alert(message: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print(f"[JANITOR] Cannot alert — missing Telegram config.")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message}
    try:
        async with httpx.AsyncClient() as client:
            await client.post(url, json=payload, timeout=10)
    except Exception as e:
        print(f"[JANITOR] Telegram alert failed: {e}")


def check_stalled_dumps():
    """Find raw_dumps stuck in pending/staged > 2 hours."""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    res = supabase.table('raw_dumps') \
        .select('id', count='exact') \
        .in_('status', ['pending', 'staged']) \
        .lt('created_at', cutoff) \
        .execute()
    return res.count or 0


def check_failed_queue():
    """Find unresolved failed_queue items."""
    res = supabase.table('failed_queue') \
        .select('id', count='exact') \
        .lt('retry_count', 5) \
        .execute()
    return res.count or 0


def check_recent_errors():
    """Find errors in audit_logs from the last hour."""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    res = supabase.table('audit_logs') \
        .select('id', count='exact') \
        .eq('level', 'ERROR') \
        .gte('created_at', cutoff) \
        .execute()
    return res.count or 0


def check_dlq_unresolved():
    """Count unresolved dead_letter_queue items (failed_queue with max retries)."""
    res = supabase.table('failed_queue') \
        .select('id', count='exact') \
        .gte('retry_count', 5) \
        .execute()
    return res.count or 0


async def main():
    if not is_business_hours():
        print("[JANITOR] Outside IST business hours. Skipping.")
        return

    issues = []

    stalled = check_stalled_dumps()
    if stalled > 0:
        issues.append(f"⚠️ {stalled} raw_dumps stalled in pipeline")

    failed_q = check_failed_queue()
    if failed_q > 0:
        issues.append(f"⚠️ {failed_q} items in failed_queue")

    errors = check_recent_errors()
    if errors > 0:
        issues.append(f"⚠️ {errors} errors in last hour (audit_logs)")

    dlq = check_dlq_unresolved()
    if dlq > 0:
        issues.append(f"⚠️ {dlq} unresolved DLQ items (max retries exceeded)")

    if not issues:
        print("[JANITOR] All clear.")
        return

    alert = f"⚠️ Rhodey Janitor:\n" + "\n".join(issues)
    print(f"[JANITOR] Issues found:\n{alert}")
    await send_telegram_alert(alert)


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
