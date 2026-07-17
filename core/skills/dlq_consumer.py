"""
DLQ Consumer — processes the dead letter queue with exponential backoff.

The DLQ stores items in audit_logs (service='dlq') that failed processing.
This consumer sweeps them, retries with exponential backoff, and
escalates to alerts on permanent failure.

Phase C of the architecture overhaul (P5).
"""

import json
from datetime import datetime, timezone, timedelta
from core.services.db import get_supabase
from core.lib.audit_logger import audit_log_sync


async def process_dlq(max_items: int = 5, max_retries: int = 3) -> dict:
    """Process items from the dead letter queue.

    Args:
        max_items: Max DLQ items to process per call.
        max_retries: Max retries before escalation.

    Returns:
        dict with keys: processed, succeeded, failed, escalated.
    """
    supabase = get_supabase()
    now = datetime.now(timezone.utc)

    # Query DLQ items from audit_logs
    cutoff = (now - timedelta(hours=72)).isoformat()
    try:
        rows = supabase.table('audit_logs') \
            .select('id, message, metadata') \
            .eq('service', 'dlq') \
            .gte('created_at', cutoff) \
            .order('created_at', desc=False) \
            .limit(max_items * 2) \
            .execute()
    except Exception as e:
        audit_log_sync("dlq_consumer", "ERROR", f"Failed to query DLQ: {e}")
        return {"processed": 0, "succeeded": 0, "failed": 0, "escalated": 0}

    if not rows or not rows.data:
        return {"processed": 0, "succeeded": 0, "failed": 0, "escalated": 0}

    # Filter to only retryable items using DLQ-specific metadata convention
    # DLQ items have metadata with: table, record_id, content, reason, retry_count
    dlq_items = []
    for row in rows.data:
        meta = row.get('metadata') or {}
        if isinstance(meta, str):
            try:
                meta = json.loads(meta)
            except Exception:
                continue
        retry_count = meta.get('retry_count', 0)
        if retry_count < max_retries:
            dlq_items.append({"audit_id": row['id'], "meta": meta, "message": row.get('message', '')})

    if not dlq_items:
        return {"processed": 0, "succeeded": 0, "failed": 0, "escalated": 0}

    dlq_items = dlq_items[:max_items]
    results = {"processed": len(dlq_items), "succeeded": 0, "failed": 0, "escalated": 0}

    for item in dlq_items:
        meta = item["meta"]
        retry_count = meta.get('retry_count', 0)
        table = meta.get('table', '')
        record_id = meta.get('record_id', '')

        # Compute backoff: 2^retry_count minutes (exponential)
        backoff_minutes = 2 ** retry_count
        last_retry_str = meta.get('last_retry_at')
        if last_retry_str:
            try:
                last_retry = datetime.fromisoformat(str(last_retry_str).replace('Z', '+00:00'))
                if datetime.now(timezone.utc) - last_retry < timedelta(minutes=backoff_minutes):
                    continue  # Not time for retry yet
            except Exception:
                pass

        # Attempt recovery based on table type
        success = False
        try:
            if table == 'raw_dumps' and record_id:
                # Reset stuck raw_dumps back to 'pending' for re-processing
                supabase.table('raw_dumps') \
                    .update({'status': 'pending', 'metadata': supabase.table('raw_dumps').select('metadata').eq('id', record_id).execute().data}) \
                    .eq('id', int(record_id)) \
                    .execute()
                success = True
            elif table == 'memories' and record_id:
                # Re-attempt embedding generation
                from core.llm import get_embedding
                emb_res = await get_embedding(item["message"][:5000])
                if emb_res and emb_res.vector:
                    supabase.table('memories') \
                        .update({'embedding': emb_res.vector, 'embedding_status': 'success'}) \
                        .eq('id', int(record_id)) \
                        .execute()
                    from core.retrieval.pipeline import schedule_index_memory
                    schedule_index_memory(int(record_id), item["message"][:5000], 'note', 'dlq_retry')
                    success = True
            elif table == 'pending_enrichment_jobs' and record_id:
                # Reset dead_letter enrichment jobs back to 'failed' for re-processing
                supabase.table('pending_enrichment_jobs') \
                    .update({'status': 'failed', 'error': None, 'retry_count': retry_count}) \
                    .eq('id', int(record_id)) \
                    .eq('status', 'dead_letter') \
                    .execute()
                audit_log_sync("dlq_consumer", "INFO",
                    f"DLQ re-queued enrichment job {record_id} (attempt {retry_count + 1})")
                success = True
            else:
                # Generic: just mark as re-processable if we can't determine type
                audit_log_sync("dlq_consumer", "INFO",
                    f"Generic DLQ item {item['audit_id']}: table={table}, record_id={record_id} - no recovery handler")
        except Exception as recovery_err:
            audit_log_sync("dlq_consumer", "WARNING",
                f"DLQ recovery failed for item {item['audit_id']}: {recovery_err}")

        # Update DLQ metadata with retry info
        new_retry_count = retry_count + 1
        new_meta = {
            **meta,
            "retry_count": new_retry_count,
            "last_retry_at": datetime.now(timezone.utc).isoformat(),
        }

        try:
            supabase.table('audit_logs') \
                .update({'metadata': new_meta}) \
                .eq('id', item['audit_id']) \
                .execute()
        except Exception:
            pass

        if success:
            results["succeeded"] += 1
            audit_log_sync("dlq_consumer", "INFO",
                f"DLQ recovery succeeded for item {item['audit_id']} (attempt {new_retry_count})")
        elif new_retry_count >= max_retries:
            results["escalated"] += 1
            # Escalate: log a critical alert
            audit_log_sync("dlq_consumer", "CRITICAL",
                f"DLQ item {item['audit_id']} escalated after {max_retries} retries. "
                f"Table: {table}, Record: {record_id}, Message: {item['message'][:200]}")
            # Also send a push notification
            try:
                from core.services.push_notification import send_push_notification
                await send_push_notification(
                    title="DLQ Escalation Alert",
                    body=f"DLQ item {item['audit_id']} failed after {max_retries} retries. Table: {table}",
                    data={"type": "dlq_escalation", "audit_id": item["audit_id"]},
                )
            except Exception:
                pass
        else:
            results["failed"] += 1

    return results
