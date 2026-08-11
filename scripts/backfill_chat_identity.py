"""Backfill Stage-0 chat identity (chat_id + participant) into existing WhatsApp rows.

Existing rows were persisted with the mixed `sender_id` ("Group: Participant"
or "Contact"). This splits every undecided/completed whatsapp row once and
writes metadata.chat_id + metadata.participant so episode windows, group
batching, and the new classifier work on exact chat keys immediately.

Idempotent: skips rows that already carry metadata.chat_id.

Usage:
    python scripts/backfill_chat_identity.py
"""
import sys
import time
from collections import Counter

sys.path.insert(0, ".")  # allow running as `python scripts/...` from repo root

from core.lib.chat_split import split_chat_identity  # noqa: E402
from core.services.db import active_user_ids, tenant_scope  # noqa: E402

BATCH = 500


def _backfill_tenant(uid: str | None) -> tuple[int, int, Counter]:
    """Backfill ONE tenant's whatsapp rows. Returns (updated, skipped, chats).

    Uses tenant_aware_client under tenant_scope so the writes respect the
    tenant boundary in the shared DB (never touches another tenant's rows).
    """
    from core.services.db import tenant_aware_client

    supabase = tenant_aware_client()
    updated = 0
    skipped = 0
    chats = Counter()

    # Process in batches over the whatsapp rows missing metadata.chat_id
    last_id = None
    while True:
        q = (
            supabase.table("messages")
            .select("id, sender_id, metadata")
            .eq("channel", "whatsapp")
            .is_("metadata->>chat_id", "null")
            .order("id")
            .limit(BATCH)
        )
        if last_id is not None:
            q = q.gt("id", last_id)
        res = q.execute()
        rows = res.data or []
        if not rows:
            break

        for r in rows:
            # Always advance the cursor — even on skipped rows — so an
            # all-skipped batch (empty sender_id) can never loop forever.
            last_id = r["id"]
            identity = split_chat_identity(r.get("sender_id"))
            meta = dict(r.get("metadata") or {})
            if not identity["chat_id"]:
                skipped += 1
                continue
            if meta.get("chat_id"):
                skipped += 1
                continue
            meta["chat_id"] = identity["chat_id"]
            if identity["participant"]:
                meta["participant"] = identity["participant"]
            supabase.table("messages").update({"metadata": meta}).eq("id", r["id"]).execute()
            updated += 1
            chats[identity["is_group"]] += 1

        print(f"  [{uid or 'legacy'}] ...{updated} updated (last id {last_id})", flush=True)
        time.sleep(0.2)

    return updated, skipped, chats


def main():
    total_updated = 0
    total_skipped = 0
    total_chats = Counter()

    # Fan out per active tenant (multi-tenant boundary); fall back to the
    # legacy unscoped path only when there is no users table yet.
    uids = active_user_ids()
    if not uids:
        updated, skipped, chats = _backfill_tenant(None)
        total_updated += updated
        total_skipped += skipped
        total_chats += chats
    else:
        for uid in uids:
            with tenant_scope(uid):
                updated, skipped, chats = _backfill_tenant(uid)
            total_updated += updated
            total_skipped += skipped
            total_chats += chats

    print(f"\nDone: {total_updated} updated, {total_skipped} skipped")
    print(f"Group rows: {total_chats.get(True, 0)} | 1:1 rows: {total_chats.get(False, 0)}")


if __name__ == "__main__":
    main()
