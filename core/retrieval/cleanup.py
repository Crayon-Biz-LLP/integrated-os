from core.lib.audit_logger import audit_log_sync


def cleanup_memory_retrieval_index(memory_id: int):
    """Deprecated: replaced by DB trigger trg_memories_cleanup (db/32).

    The AFTER DELETE trigger on memories table now automatically cascades
    cleanup to retrieval_memory_bundle_links, retrieval_passages,
    retrieval_triples, and retrieval_index_runs. This function is kept as
    a no-op for backward compatibility with existing callers.
    """
    audit_log_sync("retrieval", "INFO",
                   f"cleanup_memory_retrieval_index({memory_id}): skipped — DB trigger handles cleanup")


def sweep_orphan_retrieval_entries():
    """Deprecated: replaced by DB trigger trg_memories_cleanup (db/32).

    The AFTER DELETE trigger on memories table now automatically cascades
    cleanup to retrieval tables when a memory is deleted. No orphaned
    entries can accumulate. This function is kept as a no-op for backward
    compatibility with existing callers.
    """
    audit_log_sync("retrieval", "INFO",
                   "sweep_orphan_retrieval_entries: skipped — DB trigger handles cleanup")
