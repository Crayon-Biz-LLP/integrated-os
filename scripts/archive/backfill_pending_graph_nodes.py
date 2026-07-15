"""
Backfill script: migrate existing pending_graph_nodes rows into the new
pending_nodes and merge_proposals tables.

The old pending_graph_nodes table held two distinct concerns:
  - Node creation approvals (person/org/project nodes awaiting HITL)
  - Merge proposals (source → target merge requests)

These are now split into pending_nodes and merge_proposals respectively.

Usage:
    python scripts/backfill_pending_graph_nodes.py          # Execute
    python scripts/backfill_pending_graph_nodes.py --dry-run  # Preview only
"""

import asyncio
import os
import sys
from dotenv import load_dotenv

load_dotenv()
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.services.db import get_supabase  # noqa: E402


DRY_RUN = "--dry-run" in sys.argv


async def backfill_pending_graph_nodes():
    supabase = get_supabase()
    print(f"{'DRY RUN' if DRY_RUN else 'EXECUTING'} — Backfill pending_graph_nodes → new tables\n")

    # ── Fetch all rows from the old table ──
    res = supabase.table("pending_nodes").select("*").execute()
    old_rows = res.data or []
    print(f"Total rows in pending_graph_nodes: {len(old_rows)}\n")

    pending_count = 0
    merge_count = 0
    error_count = 0

    for row in old_rows:
        old_id = row["id"]
        label = row.get("label", "")
        node_type = row.get("type", "concept")
        status = row.get("status", "pending")
        source_text = row.get("source_text", "")
        eval_context = row.get("eval_context", {})
        created_at = row.get("created_at")

        if not label:
            continue

        # ── Merge proposals go to merge_proposals table ──
        if status == "merge_proposed":
            target_node_id = row.get("merge_candidate_id")
            if not target_node_id:
                print(f"  ⚠️  Row {old_id}: merge_proposed but no merge_candidate_id — skipping")
                continue

            # Resolve target label from graph_nodes
            target_label = None
            try:
                t_res = supabase.table("graph_nodes").select("label").eq("id", target_node_id).limit(1).execute()
                if t_res.data:
                    target_label = t_res.data[0].get("label")
            except Exception:
                pass

            if not target_label:
                print(f"  ⚠️  Row {old_id}: cannot resolve target_node_id {target_node_id} — skipping")
                continue

            if DRY_RUN:
                print(f"  [DRY] Merge proposal: {label} → {target_label} (target_id={target_node_id})")
                merge_count += 1
                continue

            try:
                ins = supabase.table("merge_proposals").insert({
                    "source_label": label,
                    "source_type": node_type,
                    "target_node_id": target_node_id,
                    "target_label": target_label,
                    "status": "proposed",
                    "rationale": "migrated_from_pending_graph_nodes",
                    "proposed_at": created_at,
                    "origin_table": "pending_nodes",
                    "origin_id": old_id,
                }).execute()
                if ins.data:
                    merge_count += 1
                    print(f"  ✅ Merged row {old_id}: {label} → {target_label} (merge_proposals #{ins.data[0]['id']})")
            except Exception as e:
                error_count += 1
                print(f"  ❌ Row {old_id} merge_proposals insert failed: {e}")

        # ── All other statuses go to pending_nodes table ──
        else:
            # Map status values
            new_status = status
            if new_status in ("merge_proposed",):
                continue  # handled above

            # Set resolved_at for terminal statuses
            resolved_at = None
            if status in ("approved", "rejected", "merged"):
                resolved_at = created_at  # Keep original timestamp

            if DRY_RUN:
                print(f"  [DRY] Pending node: {label} ({node_type}) status={status} → pending_nodes")
                pending_count += 1
                continue

            try:
                # Check if already migrated (by origin_id)
                existing = supabase.table("pending_nodes") \
                    .select("id") \
                    .eq("origin_table", "pending_nodes") \
                    .eq("origin_id", old_id) \
                    .limit(1).execute()
                if existing.data:
                    # Already migrated — skip
                    pending_count += 1
                    continue

                ins = supabase.table("pending_nodes").insert({
                    "label": label,
                    "node_type": node_type,
                    "source_text": source_text,
                    "eval_context": eval_context if isinstance(eval_context, dict) else {},
                    "status": new_status,
                    "created_at": created_at,
                    "resolved_at": resolved_at,
                    "origin_table": "pending_nodes",
                    "origin_id": old_id,
                }).execute()
                if ins.data:
                    pending_count += 1
                    if pending_count <= 3:
                        print(f"  ✅ Row {old_id}: {label} ({node_type}) → pending_nodes #{ins.data[0]['id']}")
            except Exception as e:
                error_count += 1
                print(f"  ❌ Row {old_id} pending_nodes insert failed: {e}")

    # ── Summary ──
    print()
    print("═" * 50)
    print(f"BACKFILL COMPLETE {'(DRY RUN)' if DRY_RUN else ''}")
    print(f"  Total old rows:          {len(old_rows)}")
    print(f"  → pending_nodes:          {pending_count}")
    print(f"  → merge_proposals:        {merge_count}")
    print(f"  Errors:                   {error_count}")
    print("═" * 50)

    # ── Verification counts ──
    if not DRY_RUN:
        new_pending = supabase.table("pending_nodes").select("id", count="exact").execute()
        new_merge = supabase.table("merge_proposals").select("id", count="exact").execute()
        print("\nVerification:")
        print(f"  pending_nodes count:      {new_pending.count if hasattr(new_pending, 'count') else len(new_pending.data or [])}")
        print(f"  merge_proposals count:    {new_merge.count if hasattr(new_merge, 'count') else len(new_merge.data or [])}")


if __name__ == "__main__":
    asyncio.run(backfill_pending_graph_nodes())
