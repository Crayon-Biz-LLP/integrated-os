import os
import asyncio
from datetime import datetime, timezone, timedelta
from supabase import create_client, Client
from core.lib.audit_logger import audit_log_sync, error
from core.pulse.utils import format_error
from core.pulse.llm import get_embedding
from core.services.db import versioned_update

supabase: Client = create_client(
    os.getenv("SUPABASE_URL"),
    os.getenv("SUPABASE_SERVICE_ROLE_KEY")
)


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
    Checks: pending/processing dumps, null embeddings, failed items.
    """
    lines = []
    try:
        #         Check for stuck dumps (pending/staged > 2 hours)
        two_hours_ago = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()

        # Check pending dumps
        stuck_res = supabase.table('raw_dumps') \
            .select('id', count='exact') \
            .in_('status', ['pending', 'staged']) \
            .lt('created_at', two_hours_ago) \
            .execute()
        stuck_count = stuck_res.count or 0
        if stuck_count > 0:
            lines.append(f"⚠️ {stuck_count} raw_dumps stuck in 'pending'/'staged' > 2h")

        # Check processing dumps (stuck > 10 minutes)
        ten_mins_ago = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
        processing_res = supabase.table('raw_dumps') \
            .select('id', count='exact') \
            .eq('status', 'processing') \
            .lt('created_at', ten_mins_ago) \
            .execute()
        processing_count = processing_res.count or 0
        if processing_count > 0:
            lines.append(f"⚠️ {processing_count} raw_dumps stuck in 'processing' > 10min")
            # Send Telegram alert
            try:
                telegram_chat_id = os.getenv("TELEGRAM_CHAT_ID")
                telegram_bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
                if telegram_chat_id and telegram_bot_token:
                    import httpx
                    url = f"https://api.telegram.org/bot{telegram_bot_token}/sendMessage"
                    payload = {
                        "chat_id": int(telegram_chat_id),
                        "text": f"⚠️ HEALTH ALERT: {processing_count} raw_dumps stuck in 'processing' > 10min",
                        "parse_mode": "Markdown"
                    }
                    httpx.post(url, json=payload, timeout=10)
            except Exception as alert_e:
                audit_log_sync("pulse", "WARNING", f"Failed to send Telegram alert: {alert_e}")

        # Check for null embeddings in recent memories
        seven_days_ago = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
        null_emb_res = supabase.table('memories') \
            .select('id', count='exact') \
            .is_('embedding', 'null') \
            .gte('created_at', seven_days_ago) \
            .execute()
        null_emb_count = null_emb_res.count or 0
        if null_emb_count > 0:
            lines.append(f"⚠️ {null_emb_count} memories with NULL embeddings (last 7 days)")

        # Check last Pulse success
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

        if not lines:
            return "✅ Pipeline health: All clear!"
        return "PIPELINE HEALTH REPORT:\n" + "\n".join(lines)
    except Exception as e:
        return f"⚠️ Health check failed: {e}"

async def retry_failed_operations(max_retries: int = 5):
    """Retry operations in the failed_queue with exponential backoff."""
    try:
        # Fetch items that haven't exceeded max retries
        failed_items = supabase.table('failed_queue') \
            .select('*') \
            .lt('retry_count', max_retries) \
            .order('created_at', desc=False) \
            .limit(20) \
            .execute()

        if not failed_items.data:
            return "✅ No failed items to retry."

        print(f"🔄 Retrying {len(failed_items.data)} failed operations...")
        retried = 0
        failed_again = 0

        for item in failed_items.data:
            queue_id = item['id']
            source_table = item['source_table']
            source_id = item['source_id']
            operation = item['operation']

            try:
                if operation == 'embedding' and source_table == 'memories':
                    # Retry embedding generation
                    mem_res = supabase.table('memories') \
                        .select('id, content') \
                        .eq('id', int(source_id)) \
                        .maybe_single() \
                        .execute()

                    if mem_res and mem_res.data:
                        embedding = await asyncio.to_thread(get_embedding, mem_res.data['content'])
                        if embedding and any(embedding):
                            # Versioned update for memories
                            versioned_update('memories', int(source_id), {
                                "embedding": embedding,
                                "embedding_status": "success"
                            })

                            # Remove from failed queue on success
                            supabase.table('failed_queue') \
                                .delete() \
                                .eq('id', queue_id) \
                                .execute()
                            retried += 1
                        else:
                            raise Exception("Embedding generation returned zero vector")

                elif operation == 'memory_insert':
                    # Would need the original content - skip for now
                    audit_log_sync("pulse", "WARNING", f"   ⚠️ Cannot retry memory_insert without original content: {queue_id}")
                    continue

                # Update retry count (metadata update, no versioning needed)
                supabase.table('failed_queue') \
                    .update({
                        "retry_count": item['retry_count'] + 1,
                        "last_retry_at": datetime.now(timezone.utc).isoformat()
                    }) \
                    .eq('id', queue_id) \
                    .execute()

            except Exception as e:
                # Update retry count and last_retry_at
                try:
                    supabase.table('failed_queue') \
                        .update({
                            "retry_count": item['retry_count'] + 1,
                            "last_retry_at": datetime.now(timezone.utc).isoformat(),
                            "error_message": str(e)[:500]
                        }) \
                        .eq('id', queue_id) \
                        .execute()
                except:
                    pass
                failed_again += 1

        return f"🔄 Retry complete: ✅ {retried} succeeded, ❌ {failed_again} still failing"

    except Exception as e:
        return f"⚠️ Retry process failed: {e}"
