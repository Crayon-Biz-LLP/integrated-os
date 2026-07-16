"""Pipeline health monitoring — single source for system health checks.

Used by:
- engine.py (on every pulse run, via check_pipeline_health())
- scripts/run_health.py (on-demand via GHA, via run_full_health_check())
"""

from core.services.db import get_supabase
import os
from datetime import datetime, timezone, timedelta
from core.lib.audit_logger import audit_log_sync, error
from core.pulse.utils import format_error

supabase = get_supabase()


async def update_heartbeat():
    """Update the last successful Pulse run timestamp."""
    try:
        supabase.table('core_config').upsert({
            "key": "pulse_last_success",
            "content": datetime.now(timezone.utc).isoformat()
        }, on_conflict="key").execute()
        print("💓 Heartbeat updated.")
    except Exception as e:
        error("pulse", f"Heartbeat update failed: {e}", format_error(e))


async def check_pipeline_health() -> str:
    """
    Returns a health report of the memory pipeline.
    Checks: pending/processing dumps, null embeddings, failed items, pulse heartbeat.
    Backward-compatible: engine.py calls this and logs the string result.

    Returns:
        A human-readable health report string.
    """
    result = await run_full_health_check()
    return result["report"]


async def run_full_health_check() -> dict:
    """Comprehensive health check — merges janitor checks + pipeline checks.

    Returns dict with:
      - issues: list of issue strings (empty if healthy)
      - report: formatted health report string
      - counts: dict of individual check counts
    """
    lines = []
    counts = {}

    try:
        # ── Stuck raw_dumps (pending/staged > 2h) ──
        two_hours_ago = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        stuck_res = supabase.table('raw_dumps') \
            .select('id', count='exact') \
            .in_('status', ['pending', 'staged']) \
            .lt('created_at', two_hours_ago) \
            .execute()
        counts["stalled_dumps"] = stuck_res.count or 0
        if counts["stalled_dumps"] > 0:
            lines.append(f"⚠️ {counts['stalled_dumps']} raw_dumps stuck in 'pending'/'staged' > 2h")

        # ── Stuck processing dumps (> 10 min) ──
        ten_mins_ago = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
        processing_res = supabase.table('raw_dumps') \
            .select('id', count='exact') \
            .eq('status', 'processing') \
            .lt('created_at', ten_mins_ago) \
            .execute()
        counts["stuck_processing"] = processing_res.count or 0
        if counts["stuck_processing"] > 0:
            lines.append(f"⚠️ {counts['stuck_processing']} raw_dumps stuck in 'processing' > 10min")
            _send_processing_alert(counts['stuck_processing'])

        # ── Null embeddings (last 7 days) ──
        seven_days_ago = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
        null_emb_res = supabase.table('memories') \
            .select('id', count='exact') \
            .is_('embedding', 'null') \
            .gte('created_at', seven_days_ago) \
            .execute()
        counts["null_embeddings"] = null_emb_res.count or 0
        if counts["null_embeddings"] > 0:
            lines.append(f"⚠️ {counts['null_embeddings']} memories with NULL embeddings (last 7 days)")

        # ── Pulse heartbeat ──
        hours_ago = None
        last_run_res = supabase.table('core_config') \
            .select('content') \
            .eq('key', 'pulse_last_success') \
            .maybe_single() \
            .execute()
        if last_run_res and last_run_res.data:
            last_run = datetime.fromisoformat(last_run_res.data['content'])
            hours_ago = (datetime.now(timezone.utc) - last_run).total_seconds() / 3600
            if hours_ago > 24:
                lines.append(f"⚠️ Pulse hasn't run in {hours_ago:.1f} hours!")
            else:
                lines.append(f"✅ Pulse last ran {hours_ago:.1f} hours ago")
        else:
            lines.append("⚠️ No Pulse heartbeat found")
        counts["pulse_hours_ago"] = round(hours_ago, 1) if hours_ago is not None else None

        # ── DLQ unresolved items ──
        dlq_res = supabase.table('dead_letter_queue') \
            .select('id', count='exact') \
            .eq('resolved', False) \
            .execute()
        counts["dlq_items"] = dlq_res.count or 0
        if counts["dlq_items"] > 0:
            lines.append(f"⚠️ {counts['dlq_items']} unresolved items in dead_letter_queue")

        # ── Recent errors (last hour) ──
        one_hour_ago = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        errors_res = supabase.table('system_audit_logs') \
            .select('id', count='exact') \
            .eq('event_type', 'error') \
            .gte('created_at', one_hour_ago) \
            .execute()
        counts["recent_errors"] = errors_res.count or 0
        if counts["recent_errors"] > 0:
            lines.append(f"⚠️ {counts['recent_errors']} errors in last hour (system_audit_logs)")

        # ── LLM degradations (last hour) ──
        llm_res = supabase.table('audit_logs') \
            .select('id', count='exact') \
            .eq('service', 'llm') \
            .eq('level', 'WARNING') \
            .gte('created_at', one_hour_ago) \
            .execute()
        counts["llm_degradations"] = llm_res.count or 0
        if counts["llm_degradations"] > 0:
            lines.append(f"⚠️ {counts['llm_degradations']} LLM fallback/degradations (429s/timeouts) in last hour")

        if not lines:
            return {
                "issues": [],
                "report": "✅ Pipeline health: All clear!",
                "counts": counts,
            }
        return {
            "issues": lines,
            "report": "PIPELINE HEALTH REPORT:\n" + "\n".join(lines),
            "counts": counts,
        }
    except Exception as e:
        return {
            "issues": [str(e)],
            "report": f"⚠️ Health check failed: {e}",
            "counts": counts,
        }


def _send_processing_alert(count: int):
    """Send Telegram alert for stuck processing dumps."""
    try:
        telegram_chat_id = os.getenv("TELEGRAM_CHAT_ID")
        telegram_bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
        if telegram_chat_id and telegram_bot_token:
            import httpx
            url = f"https://api.telegram.org/bot{telegram_bot_token}/sendMessage"
            payload = {
                "chat_id": int(telegram_chat_id),
                "text": f"⚠️ HEALTH ALERT: {count} raw_dumps stuck in 'processing' > 10min",
                "parse_mode": "Markdown"
            }
            httpx.post(url, json=payload, timeout=10)
    except Exception as alert_e:
        audit_log_sync("pulse", "WARNING", f"Failed to send Telegram alert: {alert_e}")
