from core.services.db import get_supabase
from core.lib.audit_logger import audit_log_sync


def cleanup_memory_retrieval_index(memory_id: int):
    """Clean up retrieval index entries for a deleted memory."""
    supabase = get_supabase()
    try:
        passages = supabase.table('retrieval_passages') \
            .select('id') \
            .eq('memory_id', memory_id) \
            .execute()
        if passages.data:
            passage_ids = [p['id'] for p in passages.data]
            supabase.table('retrieval_passage_phrase_links') \
                .delete() \
                .in_('passage_id', passage_ids) \
                .execute()
            try:
                supabase.table('retrieval_passage_triple_links') \
                    .delete() \
                    .in_('passage_id', passage_ids) \
                    .execute()
            except Exception:
                pass
            supabase.table('retrieval_memory_bundle_links') \
                .delete() \
                .eq('memory_id', memory_id) \
                .execute()
            supabase.table('retrieval_passages') \
                .delete() \
                .in_('id', passage_ids) \
                .execute()
        supabase.table('retrieval_index_runs') \
            .delete() \
            .eq('source_type', 'memory') \
            .eq('source_id', str(memory_id)) \
            .execute()
    except Exception as e:
        audit_log_sync("retrieval", "WARNING",
                       f"Cleanup retrieval index for memory {memory_id} failed: {e}")


def sweep_orphan_retrieval_entries():
    """Sweep retrieval tables for orphaned entries referencing deleted memories."""
    supabase = get_supabase()
    try:
        passages = supabase.table('retrieval_passages') \
            .select('id, memory_id') \
            .not_.is_('memory_id', 'null') \
            .execute()
        if passages.data:
            memory_ids = list(set(
                p['memory_id'] for p in passages.data if p.get('memory_id')
            ))
            if memory_ids:
                existing = supabase.table('memories') \
                    .select('id') \
                    .in_('id', memory_ids) \
                    .execute()
                existing_ids = {e['id'] for e in (existing.data or [])}
                orphan_ids = [mid for mid in memory_ids if mid not in existing_ids]
                for mid in orphan_ids:
                    cleanup_memory_retrieval_index(mid)
                if orphan_ids:
                    audit_log_sync("retrieval", "INFO",
                                   f"Sweep cleaned {len(orphan_ids)} orphaned memory retrieval entries")
        runs = supabase.table('retrieval_index_runs') \
            .select('id, source_id') \
            .eq('source_type', 'memory') \
            .execute()
        if runs.data:
            run_memory_ids = [
                int(r['source_id']) for r in runs.data
                if r.get('source_id') and r['source_id'].lstrip('-').isdigit()
            ]
            if run_memory_ids:
                existing = supabase.table('memories') \
                    .select('id') \
                    .in_('id', run_memory_ids) \
                    .execute()
                existing_ids = {e['id'] for e in (existing.data or [])}
                orphan_run_ids = [
                    r['id'] for r in runs.data
                    if r.get('source_id') and r['source_id'].lstrip('-').isdigit()
                    and int(r['source_id']) not in existing_ids
                ]
                if orphan_run_ids:
                    supabase.table('retrieval_index_runs') \
                        .delete() \
                        .in_('id', orphan_run_ids) \
                        .execute()
                    audit_log_sync("retrieval", "INFO",
                                   f"Sweep cleaned {len(orphan_run_ids)} orphaned index runs")
    except Exception as e:
        audit_log_sync("retrieval", "WARNING",
                       f"Orphan sweep failed: {e}")
