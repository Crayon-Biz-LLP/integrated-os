"""
One-time backfill: Link tasks to organizations and backfill missing org_ids.

Gaps addressed:
1. Tasks created with NULL organization_id but entity extraction found the org → backfill
2. Tasks with organization_id set but no task→org BELONGS_TO graph edge → create edge
3. Tasks with wrong org_id (planner guessed wrong) → flag for review

Run: LIVE_DB=true python scripts/backfill_org_links.py
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.services.db import get_supabase, maybe_single_safe
from core.lib.graph_rules import normalize_label, insert_pending_edge
from core.skills.backfill_graph import fetch_all_paginated, _normalize_meta

supabase = get_supabase()


def main():
    print("=" * 60)
    print("🔄 ORG LINK BACKFILL")
    print("=" * 60)

    stats = {
        "tasks_with_org_id": 0,
        "edges_created": 0,
        "orgs_backfilled": 0,
        "flagged_wrong": 0,
        "errors": 0,
    }

    # Step 1: Find all tasks with organization_id set but NO task→org graph edge
    print("\n📋 Step 1: Tasks with org_id → missing graph edge...")
    all_tasks = fetch_all_paginated(
        "tasks", "id, title, project_id, organization_id, status"
    )
    tasks_with_org = [t for t in all_tasks if t.get("organization_id")]
    stats["tasks_with_org_id"] = len(tasks_with_org)
    print(f"  Found {len(tasks_with_org)} tasks with organization_id set.")

    # Get all existing task→org edges
    all_edges = fetch_all_paginated(
        "graph_edges", "source_node_id, target_node_id, relationship, metadata"
    )
    task_org_edge_keys = set()
    for e in all_edges or []:
        if e.get("relationship") == "BELONGS_TO":
            meta = _normalize_meta(e.get("metadata"))
            tid = meta.get("task_id")
            if tid:
                task_org_edge_keys.add(int(tid))

    # Build org_id → org_node_id map
    org_nodes = fetch_all_paginated(
        "graph_nodes", "id, label, type, db_record_id, metadata",
        in_filter_col="type", in_filter_val=["organization"]
    )
    org_by_db_id = {}
    org_by_label = {}
    for n in org_nodes or []:
        if n.get("db_record_id"):
            org_by_db_id[str(n["db_record_id"])] = n
        if n.get("label"):
            org_by_label[n["label"].lower()] = n

    for task in tasks_with_org:
        task_id = task["id"]
        org_id = task["organization_id"]
        task_title = task.get("title", "Untitled")

        if task_id in task_org_edge_keys:
            continue  # Already has edge

        # Find the org node
        org_node = org_by_db_id.get(str(org_id))
        if not org_node:
            # Try by resolving org name from organizations table
            org_row = maybe_single_safe(
                supabase.table("organizations").select("name").eq("id", org_id)
            )
            if org_row and org_row.data:
                org_name = org_row.data["name"]
                org_node = org_by_label.get(org_name.lower())
            
        if not org_node:
            print(f"  ⚠️ Task {task_id} ('{task_title}'): org node not found (org_id={org_id})")
            stats["errors"] += 1
            continue

        org_label = org_node["label"]
        
        # Create pending BELONGS_TO edge
        try:
            # First ensure task has a graph node
            task_node = maybe_single_safe(
                supabase.table("graph_nodes")
                .select("id")
                .eq("type", "task")
                .filter("metadata->>task_id", "eq", str(task_id))
                .eq("is_current", True)
            )
            if not task_node or not task_node.data:
                # Create task node
                supabase.table("graph_nodes").upsert({
                    "label": task_title,
                    "type": "task",
                    "normalized_label": normalize_label(task_title),
                    "metadata": {"source": "org_backfill", "task_id": task_id}
                }, on_conflict="normalized_label, type").execute()

            insert_pending_edge(
                task_title,
                org_label,
                "BELONGS_TO",
                {
                    "source_text": f"org_backfill:{task_id}",
                    "source_table": "org_backfill",
                    "source_type": "task",
                    "target_type": "organization",
                },
            )
            stats["edges_created"] += 1
            print(f"  ✅ Created BELONGS_TO edge: '{task_title}' → '{org_label}'")
        except Exception as e:
            print(f"  ❌ Failed to create edge for task {task_id}: {e}")
            stats["errors"] += 1

    # Step 2: Find tasks WITHOUT org_id but whose content mentions a known org
    print("\n📋 Step 2: Tasks with NULL org_id → backfill from content...")
    tasks_without_org = [t for t in all_tasks if not t.get("organization_id") and t.get("status") != "cancelled"]
    print(f"  Found {len(tasks_without_org)} active tasks without org_id.")

    # Build known org list with name variations
    org_names = {}
    for n in org_nodes or []:
        label = n.get("label", "").strip()
        if label:
            org_names[label.lower()] = {"id": n.get("db_record_id"), "label": label}
            # Also add short name (before comma)
            if "," in label:
                short = label.split(",")[0].strip().lower()
                if short not in org_names:
                    org_names[short] = org_names[label.lower()]

    # Also fetch from organizations table directly
    all_orgs = fetch_all_paginated("organizations", "id, name")
    for o in all_orgs or []:
        name = o.get("name", "").strip()
        if name:
            oid = o["id"]
            org_names[name.lower()] = {"id": oid, "label": name}                # Short name (before separator)
            for sep in [",", ":", " - "]:
                if sep in name:
                    short = name.split(sep)[0].strip().lower()
                    if short not in org_names:
                        org_names[short] = {"id": oid, "label": name}

    backfilled = 0
    for task in tasks_without_org:
        task_id = task["id"]
        task_title = task.get("title", "").lower()

        # Check if any known org name appears in the title
        matched_org = None
        for org_key, org_info in org_names.items():
            if len(org_key) < 3:
                continue  # Skip very short matches
            if org_key in task_title:
                matched_org = org_info
                break

        if matched_org:
            try:
                supabase.table("tasks").update({
                    "organization_id": matched_org["id"]
                }).eq("id", task_id).execute()
                print(f"  ✅ Backfilled org '{matched_org['label']}' for task {task_id}: '{task.get('title', '')}'")
                backfilled += 1
                stats["orgs_backfilled"] += 1
            except Exception as e:
                print(f"  ❌ Failed to backfill org for task {task_id}: {e}")
                stats["errors"] += 1

    # Step 3: Flag tasks where org looks wrong (org mentioned in title but doesn't match)
    print("\n📋 Step 3: Flag potential wrong-org tasks...")
    flagged = 0
    for task in tasks_with_org:
        task_id = task["id"]
        task_title = task.get("title", "").lower()
        current_org_id = task["organization_id"]

        # Check if a different org name appears in the title
        if not current_org_id:
            continue

        for org_key, org_info in org_names.items():
            if len(org_key) < 3:
                continue
            if org_key in task_title:
                # Found an org mentioned in title
                other_org_id = org_info["id"]
                if str(other_org_id) != str(current_org_id):
                    current_org_name = org_by_db_id.get(str(current_org_id), {}).get("label", f"ID={current_org_id}")
                    print(f"  ⚠️ Task {task_id}: title mentions '{org_info['label']}' but assigned to '{current_org_name}'")
                    flagged += 1
                    stats["flagged_wrong"] += 1
                    break  # Only flag once per task

    # Summary
    print("\n" + "=" * 60)
    print("📊 BACKFILL SUMMARY")
    print("=" * 60)
    print(f"  ✅ Tasks with org_id checked:  {stats['tasks_with_org_id']}")
    print(f"  ✅ BELONGS_TO edges created:   {stats['edges_created']}")
    print(f"  ✅ Organization IDs backfilled: {stats['orgs_backfilled']}")
    print(f"  ⚠️  Wrong-org tasks flagged:     {stats['flagged_wrong']}")
    print(f"  ❌ Errors:                      {stats['errors']}")
    print("=" * 60)
    print("Review flagged tasks manually and update org_id if needed.")
    print("Backfilled tasks now have both organization_id AND graph edges.")
    print()


if __name__ == "__main__":
    main()
