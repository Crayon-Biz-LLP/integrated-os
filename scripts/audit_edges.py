#!/usr/bin/env python3
"""
Graph Edge Audit Script
========================
Inspect, delete, correct, swap, and create graph edges interactively.
Drill down by entity to see relevant edges. Always dry-runs first.

Usage:
    python scripts/audit_edges.py          # inspect + dry-run
    python scripts/audit_edges.py --apply  # execute changes after dry-run
"""

import asyncio
import json
import os
import sys
from collections import Counter
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

from supabase import create_client, Client  # noqa: E402

supabase: Client = create_client(
    os.getenv("SUPABASE_URL"),
    os.getenv("SUPABASE_SERVICE_ROLE_KEY")
)


def get_metadata(e: dict) -> dict:
    meta = e.get("metadata", {})
    if isinstance(meta, str):
        try:
            return json.loads(meta)
        except (json.JSONDecodeError, TypeError):
            return {}
    return meta or {}


def fmt_time(ts: str | None) -> str:
    if not ts:
        return "?"
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d")
    except (ValueError, AttributeError):
        return ts[:10] if ts else "?"


def classify_source_tag(tag: str) -> str:
    if tag in {"graph_approval", "tasks_table", "manual", "manual_audit"}:
        return "✓"
    if "backfill" in tag or "inference" in tag or tag == "pending_approval":
        return "⚡"
    return "?"


async def load_graph():
    edges_res = supabase.table("graph_edges").select("*").order("created_at", desc=False).execute()
    edges = edges_res.data if edges_res and edges_res.data else []

    nodes_res = supabase.table("graph_nodes").select("id, label, type").execute()
    nodes = nodes_res.data if nodes_res and nodes_res.data else []
    node_map = {n["id"]: n for n in nodes}

    enriched = []
    deleted_source_id = None

    for e in edges:
        eid = e["id"]
        src = node_map.get(e["source_node_id"], {})
        tgt = node_map.get(e["target_node_id"], {})
        src_label = src.get("label")
        tgt_label = tgt.get("label")
        src_exists = src_label is not None
        tgt_exists = tgt_label is not None

        if not src_exists:
            deleted_source_id = e["source_node_id"]
        if not tgt_exists:
            if not deleted_source_id:
                deleted_source_id = e["target_node_id"]

        meta = get_metadata(e)

        enriched.append({
            "id": eid,
            "source_node_id": e["source_node_id"],
            "target_node_id": e["target_node_id"],
            "source_label": src_label or f"<DELETED> {e['source_node_id'][:8]}",
            "target_label": tgt_label or f"<DELETED> {e['target_node_id'][:8]}",
            "relationship": e["relationship"],
            "source_tag": meta.get("source", "unknown"),
            "created_at": fmt_time(e.get("created_at")),
            "icon": classify_source_tag(meta.get("source", "")),
            "orphan": not src_exists or not tgt_exists,
        })

    return enriched, deleted_source_id


def show_dashboard(edges, deleted_source_id):
    print("\n" + "=" * 66)
    print("  📊 GRAPH EDGE AUDIT")
    print("=" * 66)

    orphans = [e for e in edges if e["orphan"]]
    ai = [e for e in edges if e["icon"] == "⚡"]
    explicit = [e for e in edges if e["icon"] == "✓"]

    print(f"\n  Total edges: {len(edges)}")
    print(f"    ✓ Explicit:    {len(explicit)}")
    print(f"    ⚡ AI-inferred: {len(ai)}")
    print(f"    ? Unknown:     {len(edges) - len(explicit) - len(ai)}")

    if orphans:
        orphan_sources = Counter(e["source_label"] for e in orphans if e["source_label"].startswith("<DELETED>"))
        orphan_targets = Counter(e["target_label"] for e in orphans if e["target_label"].startswith("<DELETED>"))
        print(f"\n  ❌ {len(orphans)} orphan edge(s) — referenced nodes were deleted:")
        for label, count in (orphan_sources + orphan_targets).most_common(5):
            if count > 0:
                print(f"     {label}: {count} edge(s)")
        print()

    # Entity summary
    entity_edges = Counter()
    for e in edges:
        if not e["source_label"].startswith("<DELETED>"):
            entity_edges[e["source_label"]] += 1
        if not e["target_label"].startswith("<DELETED>"):
            entity_edges[e["target_label"]] += 1

    print("  Entities by edge count (top 20):")
    for label, count in entity_edges.most_common(20):
        print(f"    {count:4d}  {label}")
    if len(entity_edges) > 20:
        print(f"    ... and {len(entity_edges) - 20} more")

    print()
    return entity_edges


def show_entity_edges(edges, entity_name):
    matching = [
        e for e in edges
        if entity_name.lower() in e["source_label"].lower()
        or entity_name.lower() in e["target_label"].lower()
    ]
    if not matching:
        print(f"  No edges found for '{entity_name}'.")
        return None

    print(f"\n  📋 Edges involving '{entity_name}' ({len(matching)} edges):")
    print()
    for idx, e in enumerate(matching, 1):
        print(f"  [{idx:3d}] {e['icon']} {e['source_label']} → {e['relationship']} → {e['target_label']}  [{e['source_tag']}] {e['created_at']}")
    print()
    return matching


async def apply_changes(actions: list[dict]) -> list[str]:
    results = []
    for action in actions:
        try:
            if action["type"] == "delete":
                supabase.table("graph_edges").delete().eq("id", action["edge_id"]).execute()
                results.append(f"  ✓ DELETED [{action['edge_id']}] {action['display']}")
            elif action["type"] == "correct":
                supabase.table("graph_edges").update({"relationship": action["new_rel"]}).eq("id", action["edge_id"]).execute()
                results.append(f"  ✓ CORRECTED [{action['edge_id']}] {action['old_rel']} → {action['new_rel']}  ({action['display']})")
            elif action["type"] == "swap":
                supabase.table("graph_edges").update({
                    "source_node_id": action["new_source"],
                    "target_node_id": action["new_target"],
                }).eq("id", action["edge_id"]).execute()
                results.append(f"  ✓ SWAPPED [{action['edge_id']}] {action['display']}")
            elif action["type"] == "create":
                supabase.table("graph_edges").insert({
                    "source_node_id": action["source_id"],
                    "target_node_id": action["target_id"],
                    "relationship": action["relationship"],
                    "weight": 1.0,
                    "metadata": json.dumps({"source": "manual_audit"})
                }).execute()
                results.append(f"  ✓ CREATED  {action['source_label']} → {action['relationship']} → {action['target_label']}")
            elif action["type"] == "reparent":
                updates = {}
                if action.get("fix_source"):
                    updates["source_node_id"] = action["new_id"]
                if action.get("fix_target"):
                    updates["target_node_id"] = action["new_id"]
                if updates:
                    supabase.table("graph_edges").update(updates).eq("id", action["edge_id"]).execute()
                    results.append(f"  ✓ REPARENT [{action['edge_id']}] {action['display']}  →  {action['new_label']}")
        except Exception as exc:
            results.append(f"  ✗ FAILED: {exc}")
    return results


def show_actions_summary(actions):
    print(f"\n  Queued actions ({len(actions)}):")
    for a in actions:
        if a["type"] == "delete":
            print(f"    DELETE   {a['display']}")
        elif a["type"] == "correct":
            print(f"    CORRECT  {a['old_rel']} → {a['new_rel']}  ({a['display']})")
        elif a["type"] == "swap":
            print(f"    SWAP     {a['display']}")
        elif a["type"] == "create":
            print(f"    CREATE   {a['source_label']} → {a['relationship']} → {a['target_label']}")
        elif a["type"] == "reparent":
            print(f"    REPARENT {a['display']}  →  {a['new_label']}")


async def run():
    dry_run = "--apply" not in sys.argv

    if dry_run:
        print("\n  🔍 DRY-RUN MODE — no changes will be made.")
        print("  Run with --apply to execute.\n")
    else:
        print("\n  ⚡ APPLY MODE — changes WILL be executed.\n")

    edges, deleted_source_id = await load_graph()
    if not edges:
        print("  No edges found. Nothing to audit.")
        return

    show_dashboard(edges, deleted_source_id)

    actions = []
    current_entity = None
    current_matches = None

    while True:
        try:
            raw = input("  > ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break

        if not raw:
            # Show commands based on current state
            if current_entity:
                print("  Commands:")
                print("    <number>         → delete edge")
                print("    <n>=<REL>        → correct relationship")
                print("    <n>↔            → swap direction")
                print("    reparent <label> → repoint orphan edges in this view (e.g. reparent Danny)")
                print("    back             → return to dashboard")
                print("    done             → review and apply changes")
                print("    q                → quit")
            else:
                print("  Commands:")
                print("    <entity name>    → drill into entity (partial match)")
                print("    orphans          → show all orphan edges")
                print("    delete orphans   → queue deletion of all orphans")
                print("    delete unknown   → queue deletion of ALL unknown-sourced edges (old backfill)")
                print("    reparent <id> <label>  → repoint orphan edges to label (e.g. reparent 737788ef Danny)")
                print("    done             → review and apply changes")
                print("    q                → quit")
            continue

        if raw.lower() == "q":
            break

        if raw.lower() == "done":
            if not actions:
                print("  No actions queued. Exiting.")
                return
            show_actions_summary(actions)
            if dry_run:
                print("\n  ✅ Dry-run complete. Re-run with --apply to execute.")
            else:
                print(f"\n  Executing {len(actions)} change(s)...")
                results = await apply_changes(actions)
                for r in results:
                    print(r)
                print("\n  ✅ Done.")
            return

        if raw.lower() == "back":
            current_entity = None
            current_matches = None
            show_dashboard(edges, deleted_source_id)
            continue

        if raw.lower() == "orphans":
            orphans = [e for e in edges if e["orphan"]]
            if not orphans:
                print("  No orphan edges found.")
                continue
            print(f"\n  ❌ Orphan edges ({len(orphans)}):")
            for idx, e in enumerate(orphans, 1):
                print(f"  [{idx:3d}] {e['source_label']} → {e['relationship']} → {e['target_label']}  [{e['source_tag']}] {e['created_at']}")
            print()
            current_entity = "orphans"
            current_matches = orphans
            continue

        if raw.lower() == "delete orphans":
            orphans = [e for e in edges if e["orphan"]]
            if not orphans:
                print("  No orphan edges to delete.")
                continue
            for e in orphans:
                actions.append({
                    "type": "delete",
                    "edge_id": e["id"],
                    "display": e["source_label"] + " → " + e["relationship"] + " → " + e["target_label"],
                })
            print(f"  Queued deletion of all {len(orphans)} orphan edge(s). Type 'done' to review.")
            current_entity = None
            current_matches = None
            continue

        if raw.lower() in ("delete unknown", "delete all unknown"):
            unknown = [e for e in edges if e["icon"] == "?"]
            if not unknown:
                print("  No unknown-sourced edges to delete.")
                continue
            for e in unknown:
                actions.append({
                    "type": "delete",
                    "edge_id": e["id"],
                    "display": e["source_label"] + " → " + e["relationship"] + " → " + e["target_label"],
                })
            print(f"  Queued deletion of all {len(unknown)} unknown-sourced edge(s). Type 'done' to review.")
            current_entity = None
            current_matches = None
            continue

        if raw.lower().startswith("reparent"):
            parts = raw.split(None, 2)
            if len(parts) < 3:
                print("  Format: reparent <deleted_id_prefix> <target_label>")
                print("  Example: reparent 737788ef Danny")
                continue
            del_prefix = parts[1]
            target_label = parts[2]
            tgt_res = supabase.table("graph_nodes").select("id, label").ilike("label", target_label).maybe_single().execute()
            if not tgt_res or not tgt_res.data:
                print(f"  Target label '{target_label}' not found in graph_nodes")
                continue
            tgt_id = tgt_res.data["id"]
            tgt_disp = tgt_res.data["label"]

            target_edges = [e for e in edges if e["orphan"] and (del_prefix in e["source_label"] or del_prefix in e["target_label"])]
            if not target_edges:
                print(f"  No orphan edges matching '{del_prefix}' found.")
                continue

            for e in target_edges:
                fix_source = del_prefix in e["source_label"]
                fix_target = del_prefix in e["target_label"]
                actions.append({
                    "type": "reparent",
                    "edge_id": e["id"],
                    "display": f"{e['source_label']} → {e['relationship']} → {e['target_label']}",
                    "new_id": tgt_id,
                    "new_label": tgt_disp,
                    "fix_source": fix_source,
                    "fix_target": fix_target,
                })
            print(f"  Queued reparent of {len(target_edges)} edge(s) to '{tgt_disp}'. Type 'done' to review.")
            continue

        # Check if we're in an entity drill-down
        if current_matches is not None:
            # Parse actions against current_matches
            if raw.lower().startswith("reparent"):
                parts = raw.split(None, 1)
                if len(parts) < 2:
                    print("  Format: reparent <target_label> (scoped to current view)")
                    continue
                target_label = parts[1]
                tgt_res = supabase.table("graph_nodes").select("id, label").ilike("label", target_label).maybe_single().execute()
                if not tgt_res or not tgt_res.data:
                    print(f"  Target label '{target_label}' not found in graph_nodes")
                    continue
                tgt_id = tgt_res.data["id"]
                tgt_disp = tgt_res.data["label"]
                target_edges = [e for e in current_matches if e["orphan"]]
                if not target_edges:
                    print("  No orphan edges in current view to reparent.")
                    continue
                for e in target_edges:
                    fix_source = "<DELETED>" in e["source_label"]
                    fix_target = "<DELETED>" in e["target_label"]
                    actions.append({
                        "type": "reparent",
                        "edge_id": e["id"],
                        "display": f"{e['source_label']} → {e['relationship']} → {e['target_label']}",
                        "new_id": tgt_id,
                        "new_label": tgt_disp,
                        "fix_source": fix_source,
                        "fix_target": fix_target,
                    })
                print(f"  Queued reparent of {len(target_edges)} edge(s) to '{tgt_disp}'. Type 'done' to review.")
            elif "=" in raw:
                parts = raw.split("=", 1)
                try:
                    num = int(parts[0].strip())
                    new_rel = parts[1].strip().upper()
                except (ValueError, IndexError):
                    print("  Format: <number>=<REL> (e.g. 3=WORKS_AT)")
                    continue
                if num < 1 or num > len(current_matches):
                    print(f"  Number out of range (1-{len(current_matches)})")
                    continue
                e = current_matches[num - 1]
                actions.append({
                    "type": "correct",
                    "edge_id": e["id"],
                    "display": f"{e['source_label']} → {e['relationship']} → {e['target_label']}",
                    "old_rel": e["relationship"],
                    "new_rel": new_rel,
                })
                print(f"  → Queued: CORRECT {e['relationship']} → {new_rel}")
            elif "↔" in raw:
                num_str = raw.replace("↔", "").strip()
                try:
                    num = int(num_str)
                except ValueError:
                    print("  Format: <number>↔ (e.g. 3↔)")
                    continue
                if num < 1 or num > len(current_matches):
                    print(f"  Number out of range (1-{len(current_matches)})")
                    continue
                e = current_matches[num - 1]
                actions.append({
                    "type": "swap",
                    "edge_id": e["id"],
                    "display": f"{e['source_label']} → {e['relationship']} → {e['target_label']}",
                    "new_source": e["target_node_id"],
                    "new_target": e["source_node_id"],
                })
                print(f"  → Queued: SWAP [{num}]")
            elif raw.startswith("+"):
                parts = raw[1:].strip().split(None, 2)
                if len(parts) < 3:
                    print("  Format: +<source_label> <REL> <target_label>")
                    continue
                s_label, rel, t_label = parts[0], parts[1].upper(), parts[2]
                s_res = supabase.table("graph_nodes").select("id, label").ilike("label", s_label).maybe_single().execute()
                t_res = supabase.table("graph_nodes").select("id, label").ilike("label", t_label).maybe_single().execute()
                if not s_res or not s_res.data:
                    print(f"  Source '{s_label}' not found")
                    continue
                if not t_res or not t_res.data:
                    print(f"  Target '{t_label}' not found")
                    continue
                actions.append({
                    "type": "create",
                    "source_id": s_res.data["id"],
                    "target_id": t_res.data["id"],
                    "source_label": s_res.data["label"],
                    "target_label": t_res.data["label"],
                    "relationship": rel,
                })
                print(f"  → Queued: CREATE {s_res.data['label']} → {rel} → {t_res.data['label']}")
            else:
                try:
                    nums = [int(x.strip()) for x in raw.replace(",", " ").split() if x.strip()]
                except ValueError:
                    print("  Enter edge number, e.g. '3', '3,5', '3=WORKS_AT'")
                    continue
                for num in nums:
                    if num < 1 or num > len(current_matches):
                        print(f"  {num}: out of range (1-{len(current_matches)})")
                        continue
                    e = current_matches[num - 1]
                    actions.append({
                        "type": "delete",
                        "edge_id": e["id"],
                        "display": f"{e['source_label']} → {e['relationship']} → {e['target_label']}",
                    })
                    print(f"  → Queued: DELETE [{num}]")
        else:
            # Dashboard level: try entity drill-down
            if raw.startswith("+"):
                parts = raw[1:].strip().split(None, 2)
                if len(parts) < 3:
                    print("  Format: +<source_label> <REL> <target_label>")
                    continue
                s_label, rel, t_label = parts[0], parts[1].upper(), parts[2]
                s_res = supabase.table("graph_nodes").select("id, label").ilike("label", s_label).maybe_single().execute()
                t_res = supabase.table("graph_nodes").select("id, label").ilike("label", t_label).maybe_single().execute()
                if not s_res or not s_res.data:
                    print(f"  Source '{s_label}' not found")
                    continue
                if not t_res or not t_res.data:
                    print(f"  Target '{t_label}' not found")
                    continue
                actions.append({
                    "type": "create",
                    "source_id": s_res.data["id"],
                    "target_id": t_res.data["id"],
                    "source_label": s_res.data["label"],
                    "target_label": t_res.data["label"],
                    "relationship": rel,
                })
                print(f"  → Queued: CREATE {s_res.data['label']} → {rel} → {t_res.data['label']}")
            else:
                matching = show_entity_edges(edges, raw)
                if matching is not None:
                    current_entity = raw
                    current_matches = matching

    if actions:
        show_actions_summary(actions)
        if dry_run:
            print("\n  ✅ Dry-run complete. Re-run with --apply to execute.")
        else:
            print(f"\n  Executing {len(actions)} change(s)...")
            results = await apply_changes(actions)
            for r in results:
                print(r)
            print("\n  ✅ Done.")
    else:
        print("  No actions queued. Exiting.")


def main():
    if "--help" in sys.argv or "-h" in sys.argv:
        print(__doc__)
        return
    asyncio.run(run())


if __name__ == "__main__":
    main()
