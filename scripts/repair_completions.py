"""
repair_completions.py
Surgical, idempotent migration for known-bad dump IDs.
USAGE: python scripts/repair_completions.py

Rules:
- Only processes explicit allowlisted IDs. No wildcard queries.
- Skips rows already migrated (message_type='completion').
- Preserves original content.
- Appends repaired_from_status to metadata for auditability.
- All mutations are logged and reversible.
"""

import os
from dotenv import load_dotenv
load_dotenv(".env")
from supabase import create_client

supabase = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_SERVICE_ROLE_KEY"))

# ── Explicit allowlist only — never run with a wildcard ──────────────────────
KNOWN_BAD_IDS = [562, 566, 569, 571]   # Target specific failed IDs

def repair():
    print(f"[repair_completions] Starting repair for IDs: {KNOWN_BAD_IDS}")
    repaired = 0
    skipped  = 0
    failed   = 0

    for dump_id in KNOWN_BAD_IDS:
        try:
            row_res = supabase.table("raw_dumps") \
                .select("id, content, status, message_type, metadata, is_processed") \
                .eq("id", dump_id) \
                .maybe_single() \
                .execute()

            row = row_res.data
            if not row:
                print(f"  [{dump_id}] NOT FOUND — skipping")
                skipped += 1
                continue

            # Idempotency: already migrated
            if row.get("message_type") == "completion":
                print(f"  [{dump_id}] Already migrated (message_type=completion) — skipping")
                skipped += 1
                continue

            # Safe mutation
            original_status = row.get("status", "unknown")
            meta = row.get("metadata") or {}
            meta["repaired_from_status"] = original_status   # ← audit trail
            meta["intent"] = "COMPLETION"

            supabase.table("raw_dumps").update({
                "message_type": "completion",
                "status":       "awaiting_completion_match",
                "is_processed": False,
                "metadata":     meta,
                # content is NOT touched
            }).eq("id", dump_id).execute()

            print(f"  [{dump_id}] Repaired: {original_status} → awaiting_completion_match")
            repaired += 1

        except Exception as e:
            print(f"  [{dump_id}] FAILED: {e}")
            failed += 1

    print(f"\n[repair_completions] Done. repaired={repaired} skipped={skipped} failed={failed}")

if __name__ == "__main__":
    repair()
