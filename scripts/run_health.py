#!/usr/bin/env python3
"""CLI entry point for system health check.

Replaces:
  - scripts/run_maintenance.py (deleted — sentinel piggybacks handle maintenance)
  - core/agents/janitor_check.py (deleted — merged into pulse/pipeline.py)

Usage:
    python scripts/run_health.py                    # standard: health check only
    python scripts/run_health.py --verbose          # print full report

Sends Telegram alert only if issues are found.
Silent if healthy (no alert fatigue).
"""

import asyncio
import sys
import os

# Add repo root to sys.path so core modules are importable
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def is_business_hours() -> bool:
    """Business hours: 8:30 AM to 10:30 PM IST (3:00 to 17:00 UTC)."""
    from datetime import datetime, timezone
    now_utc = datetime.now(timezone.utc)
    hour = now_utc.hour + (now_utc.minute / 60)
    return 3.0 <= hour <= 17.0


async def main():
    verbose = "--verbose" in sys.argv
    skip_biz_check = "--force" in sys.argv

    if not skip_biz_check and not is_business_hours():
        print("[HEALTH] Outside business hours. Skipping (use --force to override).")
        return

    from core.pulse.pipeline import run_full_health_check
    from core.webhook.telegram import send_telegram

    result = await run_full_health_check()
    issues = result["issues"]
    report = result["report"]

    if verbose:
        print(report)
        print(f"\nCounts: {result['counts']}")

    if not issues:
        print("[HEALTH] All clear.")
        if verbose:
            print(report)
        return

    # Issues found — log and alert
    print(f"[HEALTH] Issues found ({len(issues)}):")
    for line in issues:
        print(f"  {line}")

    telegram_chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if telegram_chat_id:
        alert = "⚠️ Rhodey Health Check:\n" + "\n".join(issues)
        await send_telegram(int(telegram_chat_id), alert)

    # Exit with error code so GHA knows something's wrong
    sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
