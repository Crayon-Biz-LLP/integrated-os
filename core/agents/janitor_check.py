"""
Rhodey Janitor — heartbeat health check.

Runs every 30 minutes via GitHub Actions. Checks pipeline health:
- Stalled raw_dumps (stuck in 'pending'/'staged' > 2 hours)
- Unresolved failed_queue items
- Recent errors in audit_logs

Alerts via Telegram if issues found. Silent if healthy.
"""
import os
from datetime import datetime, timezone, timedelta

from core.services.db import get_supabase
from core.services.telegram import send_telegram

supabase = get_supabase()

TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

IST_OFFSET = timedelta(hours=5, minutes=30)
BIZ_START_UTC = 3
BIZ_END_UTC = 17


def is_business_hours():
    now_utc = datetime.now(timezone.utc)
    hour = now_utc.hour + (now_utc.minute / 60)
    return BIZ_START_UTC <= hour <= BIZ_END_UTC


def check_stalled_dumps():
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    res = supabase.table('raw_dumps') \
        .select('id', count='exact') \
        .in_('status', ['pending', 'staged']) \
        .lt('created_at', cutoff) \
        .execute()
    return res.count or 0


def check_failed_queue():
    res = supabase.table('failed_queue') \
        .select('id', count='exact') \
        .lt('retry_count', 5) \
        .execute()
    return res.count or 0


def check_recent_errors():
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    res = supabase.table('audit_logs') \
        .select('id', count='exact') \
        .eq('level', 'ERROR') \
        .gte('created_at', cutoff) \
        .execute()
    return res.count or 0


def check_dlq_unresolved():
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
        issues.append(f"{stalled} raw_dumps stalled in pipeline")

    failed_q = check_failed_queue()
    if failed_q > 0:
        issues.append(f"{failed_q} items in failed_queue")

    errors = check_recent_errors()
    if errors > 0:
        issues.append(f"{errors} errors in last hour (audit_logs)")

    dlq = check_dlq_unresolved()
    if dlq > 0:
        issues.append(f"{dlq} unresolved DLQ items (max retries exceeded)")

    if not issues:
        print("[JANITOR] All clear.")
        return

    alert = f"Rhodey Janitor:\n" + "\n".join(issues)
    print(f"[JANITOR] Issues found:\n{alert}")
    if TELEGRAM_CHAT_ID:
        await send_telegram(int(TELEGRAM_CHAT_ID), alert)


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
