#!/usr/bin/env python3
"""Backfill: set is_current=False on all merged graph_nodes.

Before our fix, execute_graph_node_merge() set canonical_id but NOT
is_current=False. This means merged entities still appear in queries
that filter by is_current=True (like brief selection).

This script finds all graph_nodes with canonical_id set and marks them
is_current=False, so the brief selection and Live tab see only
un-merged entities.
"""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

_env = os.path.join(os.path.dirname(__file__), '..', '.env')
if os.path.exists(_env):
    with open(_env) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                k, v = line.split('=', 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'").strip())

from core.services.db import get_supabase

s = get_supabase()

# Find all merged nodes that still have is_current=True
res = s.table("graph_nodes") \
    .select("id, label, type, canonical_id") \
    .not_.is_("canonical_id", "null") \
    .eq("is_current", True) \
    .execute()

merged = res.data or []
print(f"Found {len(merged)} merged nodes with is_current=True (needs fix)")

by_type = {}
for n in merged:
    t = n.get("type", "?")
    by_type[t] = by_type.get(t, 0) + 1

for t, c in sorted(by_type.items()):
    print(f"  {t}: {c}")

# Show the merged entities
print()
print("=== Affected entities ===")
for n in sorted(merged, key=lambda x: (x.get("type",""), x.get("label",""))):
    print(f"  {n['label']:<35} {n['type']:<14} canonical_id={str(n['canonical_id'])[:8]}")

ids = [n["id"] for n in merged]
total = len(ids)
if total == 0:
    print("\nNothing to fix. Exiting.")
    sys.exit(0)

print(f"\nSetting is_current=False on {total} merged nodes (one at a time)...")
for i, node_id in enumerate(ids, 1):
    try:
        s.table("graph_nodes").update({"is_current": False}).eq("id", node_id).execute()
        if i % 10 == 0 or i == total:
            print(f"  [{i}/{total}] Updated node {node_id[:8]}...")
    except Exception as e:
        print(f"  [{i}/{total}] FAILED node {node_id[:8]}...: {e}")

# Verify
verify = s.table("graph_nodes") \
    .select("id, label, type") \
    .not_.is_("canonical_id", "null") \
    .eq("is_current", True) \
    .execute()

remaining = verify.data or []
print(f"\n✅ Done. Remaining merged nodes with is_current=True: {len(remaining)}")

# Show current state of unmerged entities
print()
print("=== Current unmerged entities (person/org/project, is_current=true, canonical_id=null) ===")
clean = s.table("graph_nodes") \
    .select("label, type") \
    .in_("type", ["organization", "project", "person"]) \
    .eq("is_current", True) \
    .is_("canonical_id", "null") \
    .execute()

clean_data = clean.data or []
for n in sorted(clean_data, key=lambda x: (x.get("type",""), x.get("label",""))):
    print(f"  [{n['type']:>12}] {n['label']}")
print(f"\nTotal clean unmerged entities: {len(clean_data)}")

# Also check what brief selection would pick
print()
print("=== Brief eligible entities (what select_entities_to_refresh sees) ===")
eligible = [n for n in clean_data if n.get("type") in ("organization", "project", "person")]
for n in sorted(eligible, key=lambda x: (x.get("type",""), x.get("label",""))):
    print(f"  [{n['type']:>12}] {n['label']}")
print(f"\nTotal brief-eligible: {len(eligible)}")
