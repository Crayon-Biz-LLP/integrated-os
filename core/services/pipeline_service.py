import os
from datetime import datetime, timezone, timedelta
from core.services.db import get_supabase
from core.lib.audit_logger import audit_log_sync


async def check_pipeline_health() -> str:
    lines = []
    supabase = get_supabase()
    try:
        two_hours_ago = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()

        stuck_res = supabase.table('raw_dumps') \
            .select('id', count='exact') \
            .in_('status', ['pending', 'staged']) \
            .lt('created_at', two_hours_ago) \
            .execute()
        stuck_count = stuck_res.count or 0
        if stuck_count > 0:
            lines.append(f"{stuck_count} raw_dumps stuck in 'pending'/'staged' > 2h")

        ten_mins_ago = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
        processing_res = supabase.table('raw_dumps') \
            .select('id', count='exact') \
            .eq('status', 'processing') \
            .lt('created_at', ten_mins_ago) \
            .execute()
        processing_count = processing_res.count or 0
        if processing_count > 0:
            lines.append(f"{processing_count} raw_dumps stuck in 'processing' > 10min")
            try:
                telegram_chat_id = os.getenv("TELEGRAM_CHAT_ID")
                telegram_bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
                if telegram_chat_id and telegram_bot_token:
                    import httpx
                    url = f"https://api.telegram.org/bot{telegram_bot_token}/sendMessage"
                    payload = {
                        "chat_id": int(telegram_chat_id),
                        "text": f"HEALTH ALERT: {processing_count} raw_dumps stuck in 'processing' > 10min",
                        "parse_mode": "Markdown"
                    }
                    httpx.post(url, json=payload, timeout=10)
            except Exception as alert_e:
                audit_log_sync("pipeline_service", "WARNING", f"Failed to send Telegram alert: {alert_e}")

        seven_days_ago = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
        null_emb_res = supabase.table('memories') \
            .select('id', count='exact') \
            .is_('embedding', 'null') \
            .gte('created_at', seven_days_ago) \
            .execute()
        null_emb_count = null_emb_res.count or 0
        if null_emb_count > 0:
            lines.append(f"{null_emb_count} memories with NULL embeddings (last 7 days)")

        last_run_res = supabase.table('core_config') \
            .select('content') \
            .eq('key', 'pulse_last_success') \
            .maybe_single() \
            .execute()
        if last_run_res and last_run_res.data:
            last_run = datetime.fromisoformat(last_run_res.data['content'])
            hours_ago = (datetime.now(timezone.utc) - last_run).total_seconds() / 3600
            if hours_ago > 24:
                lines.append(f"Pulse hasn't run in {hours_ago:.1f} hours!")
            else:
                lines.append(f"Pulse last ran {hours_ago:.1f} hours ago")
        else:
            lines.append("No Pulse heartbeat found")

        if not lines:
            return "Pipeline health: All clear!"
        return "PIPELINE HEALTH REPORT:\n" + "\n".join(lines)
    except Exception as e:
        return f"Health check failed: {e}"


async def add_to_failed_queue(source_table: str, source_id: str, operation: str, error_message: str):
    supabase = get_supabase()
    try:
        supabase.table('failed_queue').insert({
            "source_table": source_table,
            "source_id": str(source_id),
            "operation": operation,
            "error_message": error_message[:500] if error_message else None,
        }).execute()
    except Exception as e:
        audit_log_sync("pipeline_service", "WARNING", f"Failed to add to failed_queue: {e}")


async def retry_failed_operations(max_retries: int = 5):
    supabase = get_supabase()
    try:
        failed_items = supabase.table('failed_queue') \
            .select('*') \
            .lt('retry_count', max_retries) \
            .order('created_at', desc=False) \
            .limit(10) \
            .execute()
        if not failed_items.data:
            return

        for item in failed_items.data:
            try:
                audit_log_sync("pipeline_service", "INFO", f"Retrying {item['source_table']}:{item['source_id']}")
                supabase.table('failed_queue') \
                    .update({'retry_count': item.get('retry_count', 0) + 1, 'last_retry_at': datetime.now(timezone.utc).isoformat()}) \
                    .eq('id', item['id']) \
                    .execute()
            except Exception as retry_e:
                audit_log_sync("pipeline_service", "ERROR", f"Retry failed for {item['id']}: {retry_e}")
    except Exception as e:
        audit_log_sync("pipeline_service", "WARNING", f"retry_failed_operations failed: {e}")
