"""
Backfill script: Find raw_dumps with no memories entry and re-process them.

Idempotent — safe to run multiple times. Skips records that already have
matching memories entries. Logs all operations to audit_logs.
Failures go to failed_queue for retry.

Usage:
    python scripts/backfill_orphaned_notes.py           # dry run
    python scripts/backfill_orphaned_notes.py --apply   # actually process
"""
import os
import sys
import json
import time
import argparse

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from core.lib.audit_logger import audit_log_sync
from dotenv import load_dotenv
from supabase import create_client

load_dotenv(os.path.join(os.path.dirname(__file__), '..', '.env'))

supabase = create_client(
    os.getenv("SUPABASE_URL"),
    os.getenv("SUPABASE_SERVICE_ROLE_KEY")
)

EMBEDDING_DIMENSION = 768

try:
    from google import genai
    gemini_client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
    EMBEDDING_MODEL = "gemini-embedding-2-preview"

    def get_embedding(text: str) -> list:
        try:
            result = gemini_client.models.embed_content(
                model=EMBEDDING_MODEL,
                contents=text,
                config={'output_dimensionality': EMBEDDING_DIMENSION}
            )
            return result.embeddings[0].values
        except Exception as e:
            print(f"[BACKFILL] Embedding error: {e}")
            return [0] * EMBEDDING_DIMENSION
except ImportError:
    print("[BACKFILL] WARNING: google.genai not available. Using zero-vector fallback.")
    def get_embedding(text: str) -> list:
        return [0] * EMBEDDING_DIMENSION


def find_orphaned_dumps():
    """Find raw_dumps entries that lack matching memories rows."""
    dumps_res = supabase.table('raw_dumps') \
        .select('id, content, created_at, metadata, source') \
        .in_('status', ['completed', 'pending', 'staged', 'embedding_failed']) \
        .execute()
    all_dumps = dumps_res.data or []
    print(f"[BACKFILL] Found {len(all_dumps)} total eligible dumps.")

    # Get all memories content for comparison
    memories_res = supabase.table('memories') \
        .select('content, source, created_at') \
        .execute()
    all_memories = memories_res.data or []
    memory_contents = set()
    for m in all_memories:
        content = (m.get('content') or '').strip().lower()
        if content:
            memory_contents.add(content)

    orphaned = []
    for d in all_dumps:
        content = (d.get('content') or '').strip()
        if not content:
            continue
        # Skip acknowledgments and system messages
        if d.get('metadata') and isinstance(d.get('metadata'), dict):
            meta_str = d.get('metadata', '{}')
            if isinstance(meta_str, str):
                try:
                    meta = json.loads(meta_str)
                except:
                    meta = {}
            else:
                meta = meta_str
            if meta.get('type') == 'ack':
                continue
        if content.lower() in memory_contents:
            continue
        orphaned.append(d)

    print(f"[BACKFILL] Found {len(orphaned)} orphaned dumps (no matching memories).")
    return orphaned


def process_orphan(dump, apply: bool):
    """Process a single orphaned dump — embed and insert into memories."""
    content = dump.get('content', '')
    dump_id = dump['id']
    source = dump.get('source', 'backfill')

    if not content or not content.strip():
        return {"id": dump_id, "status": "skipped", "reason": "empty content"}

    if not apply:
        return {"id": dump_id, "status": "would_process", "content_preview": content[:80]}

    embedding = get_embedding(content)
    embed_success = bool(embedding and any(embedding))
    embed_status = 'success' if embed_success else 'failed'

    if not embed_success:
        try:
            supabase.table('raw_dumps').update({"status": "embedding_failed"}).eq('id', dump_id).execute()
        except Exception as e:
            print(f"[BACKFILL] Failed to update dump {dump_id}: {e}")
        try:
            supabase.table('failed_queue').insert({
                "source_table": "raw_dumps",
                "source_id": str(dump_id),
                "operation": "embedding",
                "error_message": f"Backfill: Embedding returned zero vector"
            }).execute()
        except Exception as e:
            print(f"[BACKFILL] Failed to write to failed_queue: {e}")
        audit_log_sync("backfill", "ERROR", f"Backfill embedding failed for dump {dump_id}")
        return {"id": dump_id, "status": "embedding_failed"}

    try:
        supabase.table('memories').insert({
            "content": content,
            "memory_type": "backfilled",
            "embedding": embedding,
            "embedding_status": embed_status,
            "source": source or "backfill"
        }).execute()
    except Exception as e:
        err_msg = str(e)
        supabase.table('failed_queue').insert({
            "source_table": "memories",
            "source_id": str(dump_id),
            "operation": "memory_insert",
            "error_message": err_msg[:500]
        }).execute()
        supabase.table('raw_dumps').update({"status": "embedding_failed"}).eq('id', dump_id).execute()
        audit_log_sync("backfill", "ERROR", f"Backfill memory insert failed for dump {dump_id}: {err_msg[:100]}")
        return {"id": dump_id, "status": "insert_failed", "error": err_msg[:100]}

    supabase.table('raw_dumps').update({"status": "processed", "is_processed": True}).eq('id', dump_id).execute()
    audit_log_sync("backfill", "INFO", f"Backfilled dump {dump_id} to memories")
    return {"id": dump_id, "status": "processed"}


def main():
    parser = argparse.ArgumentParser(description="Backfill orphaned raw_dumps into memories")
    parser.add_argument('--apply', action='store_true', help="Actually process (default: dry run)")
    parser.add_argument('--limit', type=int, default=0, help="Max dumps to process (0 = all)")
    args = parser.parse_args()

    print(f"[BACKFILL] Running in {'APPLY' if args.apply else 'DRY RUN'} mode")
    if not args.apply:
        print("[BACKFILL] Pass --apply to actually process.\n")

    orphaned = find_orphaned_dumps()
    if not orphaned:
        print("[BACKFILL] Nothing to do.")
        return

    if args.limit > 0:
        orphaned = orphaned[:args.limit]
        print(f"[BACKFILL] Limited to {args.limit} dumps.")

    results = {"processed": 0, "failed": 0, "skipped": 0}
    for i, dump in enumerate(orphaned, 1):
        result = process_orphan(dump, apply=args.apply)
        if result["status"] == "processed":
            results["processed"] += 1
        elif result["status"] in ("embedding_failed", "insert_failed"):
            results["failed"] += 1
        elif result["status"] == "skipped":
            results["skipped"] += 1
        if args.apply:
            time.sleep(0.5)  # Rate limit
        print(f"  [{i}/{len(orphaned)}] dump {dump['id']}: {result['status']}")

    print(f"\n[BACKFILL] Done. Processed: {results['processed']}, Failed: {results['failed']}, Skipped: {results['skipped']}")
    audit_log_sync("backfill", "INFO",
        f"Backfill complete. Processed: {results['processed']}, Failed: {results['failed']}, Skipped: {results['skipped']}")


if __name__ == "__main__":
    main()
