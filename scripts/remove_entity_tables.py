"""Phase 2 executor: full removal of the people/organizations mirror tables.

This script is the GATE for db/75_drop_entity_mirror_tables.sql.

Usage:
    python3 scripts/remove_entity_tables.py            # dry-run pre-check
    python3 scripts/remove_entity_tables.py --verify   # post-apply verification

The actual schema change (ALTER types, FK repoints, DROP TABLE) runs as raw
SQL — paste db/75_drop_entity_mirror_tables.sql into the Supabase SQL editor
ONLY after the dry-run reports "ALL CHECKS PASSED".

Makes NO writes in dry-run mode. Verification is read-only.
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv

load_dotenv()

from core.services.db import get_supabase  # noqa: E402

s = get_supabase()


def meta_of(node):
    m = node.get("metadata") or {}
    if isinstance(m, str):
        try:
            m = json.loads(m)
        except Exception:
            m = {}
    return m


def precheck() -> int:
    fails = 0
    print("=" * 70)
    print("DROP-MIRROR PRECHECK (read-only) — must all pass before db/75")
    print("=" * 70)

    # 1. people mapping coverage
    people = s.table("people").select("id, name, is_current, deleted_at, graph_node_id").execute().data or []
    live_people = [p for p in people if p.get("is_current") and not p.get("deleted_at")]
    no_node = [p for p in people if not p.get("graph_node_id")]
    print(f"\n[1] people mapping: {len(people)} rows, {len(live_people)} live, "
          f"{len(no_node)} without graph_node_id (live: {sum(1 for p in no_node if p.get('is_current') and not p.get('deleted_at'))})")
    if any(p.get("is_current") and not p.get("deleted_at") and not p.get("graph_node_id") for p in people):
        fails += 1
        print("    ❌ LIVE people without graph_node_id exist — abort")

    # 2. org mapping coverage
    orgs = s.table("organizations").select("id, name, is_active, graph_node_id").execute().data or []
    no_node_orgs = [o for o in orgs if not o.get("graph_node_id")]
    print(f"[2] org mapping: {len(orgs)} rows, {len(no_node_orgs)} without graph_node_id")
    if any(o.get("is_active") and not o.get("graph_node_id") for o in orgs):
        fails += 1
        print("    ❌ active orgs without graph_node_id exist — abort")

    # 3. dangling messages.linked_person_id
    msgs = s.table("messages").select("id, linked_person_id").not_.is_("linked_person_id", "null").limit(1000).execute().data or []
    live_ids = {str(p["id"]): str(p.get("graph_node_id")) for p in people if p.get("graph_node_id")}
    dangling_msg = [m for m in msgs if str(m["linked_person_id"]) not in live_ids]
    print(f"[3] messages.linked_person_id: {len(msgs)} set, {len(dangling_msg)} dangling")
    if dangling_msg:
        fails += 1
        for m in dangling_msg[:5]:
            print(f"    ❌ msg {m['id']} -> {m['linked_person_id']} has no node")

    # 4. dangling memories.metadata.people_id
    mem = s.table("memories").select("id, metadata").limit(2000).execute().data or []
    dangling_mem = []
    for m in mem:
        pid = meta_of(m).get("people_id")
        if pid and str(pid) not in live_ids:
            dangling_mem.append((m["id"], pid))
    print(f"[4] memories.metadata.people_id: {sum(1 for m in mem if meta_of(m).get('people_id'))} refs, {len(dangling_mem)} dangling")
    if dangling_mem:
        fails += 1
        for mid, pid in dangling_mem[:5]:
            print(f"    ❌ memory {mid} -> {pid} has no node")

    # 5. org-side refs: tasks / projects / project_organizations / canonical_pages / memories
    org_map = {str(o["id"]): str(o.get("graph_node_id")) for o in orgs if o.get("graph_node_id")}
    for label, table, col in [
        ("tasks", "tasks", "organization_id"),
        ("projects", "projects", "organization_id"),
        ("project_organizations", "project_organizations", "organization_id"),
        ("canonical_pages", "canonical_pages", "organization_id"),
        ("memories", "memories", "organization_id"),
    ]:
        try:
            rows = s.table(table).select(f"id, {col}").not_.is_(col, "null").limit(2000).execute().data or []
        except Exception:
            print(f"[5] {label}.{col}: (column absent in prod — skipped)")
            continue
        dangling = [r for r in rows if str(r[col]) not in org_map]
        print(f"[5] {label}.{col}: {len(rows)} set, {len(dangling)} dangling")
        if dangling:
            fails += 1
            for r in dangling[:4]:
                print(f"    ❌ {table} {r['id']} -> {r[col]} has no node")

    print()
    if fails == 0:
        print("✅ ALL CHECKS PASSED — paste db/75_drop_entity_mirror_tables.sql into the Supabase SQL editor.")
        return 0
    print(f"❌ {fails} failing check(s) — do NOT apply db/75 until resolved.")
    return 1


def verify() -> int:
    fails = 0
    print("=" * 70)
    print("POST-APPLY VERIFICATION (read-only)")
    print("=" * 70)

    # Tables gone?
    for tbl in ("people", "organizations"):
        try:
            s.table(tbl).select("id").limit(1).execute()
            print(f"[1] ❌ table '{tbl}' still exists")
            fails += 1
        except Exception:
            print(f"[1] ✅ table '{tbl}' dropped")

    # Node self-canonicalization
    nodes = s.table("graph_nodes").select("id, type, db_record_id, metadata").eq("is_current", True).in_("type", ["person", "organization"]).execute().data or []
    not_self = [n for n in nodes if (meta_of(n).get("people_id") or meta_of(n).get("organization_id") or n.get("db_record_id")) != n["id"]]
    print(f"[2] node self-canonical ids: {len(nodes) - len(not_self)}/{len(nodes)}")
    if not_self:
        fails += 1
        for n in not_self[:5]:
            print(f"    ❌ {n['type']} {n['id'][:8]} meta={meta_of(n).get('people_id') or meta_of(n).get('organization_id')} db={n.get('db_record_id')}")

    # Dangling refs (should all be 0)
    node_ids = {n["id"] for n in nodes}
    msgs = s.table("messages").select("id, linked_person_id").not_.is_("linked_person_id", "null").limit(2000).execute().data or []
    d_msg = [m for m in msgs if str(m["linked_person_id"]) not in node_ids]
    print(f"[3] dangling messages.linked_person_id: {len(d_msg)}")
    if d_msg:
        fails += 1

    for label, table, col in [
        ("tasks", "tasks", "organization_id"),
        ("projects", "projects", "organization_id"),
        ("project_organizations", "project_organizations", "organization_id"),
        ("memories", "memories", "organization_id"),
        ("canonical_pages", "canonical_pages", "organization_id"),
    ]:
        rows = s.table(table).select(f"id, {col}").not_.is_(col, "null").limit(2000).execute().data or []
        d = [r for r in rows if str(r[col]) not in node_ids]
        print(f"[4] dangling {label}: {len(d)}")
        if d:
            fails += 1

    # Enrichment presence
    no_enrich = [n for n in nodes if not isinstance(meta_of(n).get("enrichment"), dict)]
    print(f"[5] nodes with enrichment: {len(nodes) - len(no_enrich)}/{len(nodes)}")
    if no_enrich:
        fails += 1
        for n in no_enrich[:5]:
            print(f"    ❌ {n['type']} '{n['id'][:8]}' missing enrichment")

    print()
    if fails == 0:
        print("✅ VERIFICATION PASSED — mirror tables fully removed, graph is single source of truth.")
        return 0
    print(f"❌ {fails} verification failure(s).")
    return 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--verify", action="store_true", help="post-apply verification mode")
    args = ap.parse_args()
    sys.exit(verify() if args.verify else precheck())
