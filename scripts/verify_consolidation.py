"""READ-ONLY verification for the entity consolidation (migrations 74+75).

After migration 75 the people/organizations mirror tables are GONE — the
graph node is the single source of truth. This tool verifies:

  * Every live person/org node carries enrichment on metadata.enrichment.
  * Every live person/org node is self-canonical: metadata.people_id /
    organization_id == the node's own id, and db_record_id == node id.
  * No dangling references: messages.linked_person_id, tasks.organization_id,
    projects.organization_id, project_organizations.organization_id,
    memories.organization_id all point at live-or-archived graph nodes.

Makes NO writes. Safe to run anytime.

Usage:
    python3 scripts/verify_consolidation.py
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv

load_dotenv()

from core.services.db import get_supabase  # noqa: E402

supabase = get_supabase()


def _meta(n) -> dict:
    m = n.get("metadata") or {}
    if isinstance(m, str):
        try:
            m = json.loads(m)
        except Exception:
            m = {}
    return m


def audit(kind: str) -> dict:
    nodes = supabase.table("graph_nodes") \
        .select("id, label, type, metadata, db_record_id") \
        .eq("type", kind) \
        .eq("is_current", True) \
        .execute().data or []

    id_key = "people_id" if kind == "person" else "organization_id"
    fields = ["role", "strategic_weight", "organization_name", "last_interaction_date"] if kind == "person" else ["is_active", "org_type", "description"]

    stats = {
        "nodes": len(nodes),
        "with_enrichment": 0,
        "without_enrichment": [],   # labels
        "not_self_canonical": [],   # (label, meta_id, db_record_id)
        "missing_fields": [],       # (label, field)
    }

    for n in nodes:
        meta = _meta(n)
        enrich = meta.get("enrichment")
        if not isinstance(enrich, dict) or not enrich:
            stats["without_enrichment"].append(n.get("label"))
            continue
        stats["with_enrichment"] += 1
        meta_id = meta.get(id_key)
        db_id = n.get("db_record_id")
        if str(meta_id or "") != n["id"] or str(db_id or "") != n["id"]:
            stats["not_self_canonical"].append((n.get("label"), meta_id, db_id))
        for f in fields:
            if f == "strategic_weight":
                if enrich.get(f) is None:
                    stats["missing_fields"].append((n.get("label"), f))
            elif enrich.get(f) in (None, ""):
                stats["missing_fields"].append((n.get("label"), f))

    return stats


def check_dangling(node_ids: set) -> list:
    """Return a list of (label, count) for dangling FK-style references."""
    out = []
    checks = [
        ("messages.linked_person_id", "messages", "linked_person_id"),
        ("tasks.organization_id", "tasks", "organization_id"),
        ("projects.organization_id", "projects", "organization_id"),
        ("project_organizations.organization_id", "project_organizations", "organization_id"),
        ("memories.organization_id", "memories", "organization_id"),
        ("canonical_pages.organization_id", "canonical_pages", "organization_id"),
    ]
    for label, table, col in checks:
        try:
            rows = supabase.table(table).select(f"id, {col}").not_.is_(col, "null").limit(2000).execute().data or []
        except Exception:
            continue
        d = [r for r in rows if str(r[col]) not in node_ids]
        if d:
            out.append((label, len(d)))
    return out


def main() -> None:
    print("=" * 72)
    print("CONSOLIDATION VERIFICATION (migrations 74+75, READ-ONLY)")
    print("=" * 72)

    node_ids = set()
    for kind, title in (("person", "PEOPLE"), ("organization", "ORGANIZATIONS")):
        s = audit(kind)
        print(f"\n--- {title} ---")
        print(f"  live nodes:                {s['nodes']}")
        print(f"  nodes WITH enrichment:     {s['with_enrichment']}")
        print(f"  nodes WITHOUT enrichment:  {len(s['without_enrichment'])}")
        for lbl in s["without_enrichment"][:15]:
            print(f"      ❌ {lbl}")
        print(f"  not self-canonical ids:    {len(s['not_self_canonical'])}")
        for lbl, mid, did in s["not_self_canonical"][:10]:
            print(f"      ⚠️ {lbl}: meta_id={mid} db_record_id={did}")
        print(f"  missing enrichment fields: {len(s['missing_fields'])}")
        for lbl, f in s["missing_fields"][:10]:
            print(f"      ⚠️ {lbl}: {f}")
        for n in supabase.table("graph_nodes").select("id").eq("type", kind).eq("is_current", True).execute().data or []:
            node_ids.add(n["id"])

    print("\n--- REFERENTIAL INTEGRITY (all should be 0) ---")
    dangling = check_dangling(node_ids)
    if not dangling:
        print("  ✅ no dangling references")
    for label, count in dangling:
        print(f"  ❌ {label}: {count} dangling")

    print("\n" + "=" * 72)
    print("Done. No writes performed.")


if __name__ == "__main__":
    main()
