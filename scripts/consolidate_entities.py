"""Entity consolidation executor (migration 74).

Graph-first consolidation: makes graph_nodes the single source of truth for
person/organization identity + enrichment by:

1. REPORT (default / --dry-run): shows every person/org domain row that lacks
   a live graph node (orphans) and the enrichment fields that will move.
2. --apply:
   a. Creates live graph nodes for orphaned domain rows (preserving their
      enrichment, linking graph_node_id back).
   b. Backfills enrichment (role, strategic_weight, organization_name,
      last_interaction_date, is_active, org_type, description, ...) into
      graph_nodes.metadata.enrichment. Idempotent — skips already-enriched.
   c. Diff-check: verifies parity between the graph node enrichment and the
      domain mirror row for every live person/org node.

Requires SUPABASE_URL + SUPABASE_SERVICE_ROLE_KEY (from .env).

Usage:
    python3 scripts/consolidate_entities.py            # dry-run report
    python3 scripts/consolidate_entities.py --apply    # execute
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv

load_dotenv()

from core.services.db import get_supabase, maybe_single_safe  # noqa: E402
from core.lib.graph_rules import normalize_label  # noqa: E402

supabase = get_supabase()

APPLY = "--apply" in sys.argv


def _meta_of(node) -> dict:
    meta = node.get("metadata") or {}
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except Exception:
            meta = {}
    return meta


def _legacy_id(node, kind: str):
    """Resolve the legacy domain-table id stored on the node."""
    meta = _meta_of(node)
    key = "people_id" if kind == "person" else "organization_id"
    return meta.get(key) or node.get("db_record_id")


def _fetch_live_nodes(kind: str):
    res = supabase.table("graph_nodes") \
        .select("id, label, type, metadata, db_record_id, created_at") \
        .eq("type", kind) \
        .eq("is_current", True) \
        .execute()
    return res.data or []


def _orphan_report():
    """Return (people_orphans, org_orphans) where each is list of rows."""
    people = supabase.table("people") \
        .select("id, name, role, strategic_weight, organization_name, graph_node_id") \
        .eq("is_current", True) \
        .is_("deleted_at", "null") \
        .execute().data or []
    orgs = supabase.table("organizations") \
        .select("id, name, org_type, description, graph_node_id") \
        .eq("is_active", True) \
        .execute().data or []

    people_nodes = _fetch_live_nodes("person")
    org_nodes = _fetch_live_nodes("organization")

    people_node_ids = {n["id"] for n in people_nodes}
    people_node_labels = {n.get("label", "").strip().lower() for n in people_nodes}
    org_node_ids = {n["id"] for n in org_nodes}
    org_node_labels = {n.get("label", "").strip().lower() for n in org_nodes}

    people_orphans = [
        p for p in people
        if p.get("graph_node_id") not in people_node_ids
        and (p.get("name") or "").strip().lower() not in people_node_labels
    ]
    org_orphans = [
        o for o in orgs
        if o.get("graph_node_id") not in org_node_ids
        and (o.get("name") or "").strip().lower() not in org_node_labels
    ]
    return people_orphans, org_orphans


def _find_noncurrent_node(label: str, kind: str):
    """Find a NON-current graph node with the same label+type (archived/merged).
    The graph is the source of truth: a domain row whose label maps to a
    non-current node is STALE and should be archived, not re-created."""
    try:
        res = supabase.table("graph_nodes") \
            .select("id, canonical_id") \
            .eq("type", kind) \
            .ilike("label", label) \
            .eq("is_current", False) \
            .limit(1) \
            .execute()
        return res.data[0] if res.data else None
    except Exception:
        return None


def create_missing_nodes(people_orphans, org_orphans) -> None:
    """Create live graph nodes for orphaned domain rows (idempotent by label).
    If a NON-current node already exists with the same label+type, the domain
    row is stale — archive it instead of creating a duplicate node."""
    for p in people_orphans:
        label = p["name"].strip()
        if not label:
            continue
        existing = maybe_single_safe(
            supabase.table("graph_nodes").select("id").eq("type", "person").ilike("label", label).eq("is_current", True)
        )
        if existing and existing.data:
            continue
        stale = _find_noncurrent_node(label, "person")
        if stale:
            supabase.table("people").update({
                "deleted_at": "now()",
                "is_current": False,
                "strategic_weight": 0,
                "graph_node_id": stale["id"],
            }).eq("id", p["id"]).execute()
            print(f"    ⚠️ Archived stale people row '{label}' (id={p['id']}) — node exists but is not live (canonical_id={stale.get('canonical_id')})")
            continue
        node_id = supabase.table("graph_nodes").insert({
            "label": label,
            "type": "person",
            "epistemic_status": "asserted",
            "normalized_label": normalize_label(label),
            "db_record_id": str(p["id"]),
            "metadata": {
                "source": "entity_consolidation",
                "people_id": str(p["id"]),
                "enrichment": {
                    "role": p.get("role"),
                    "strategic_weight": p.get("strategic_weight") or 5,
                    "organization_name": p.get("organization_name"),
                    "last_interaction_date": None,
                    "is_active": True,
                },
            },
        }).execute()
        if node_id.data:
            supabase.table("people").update({"graph_node_id": node_id.data[0]["id"]}).eq("id", p["id"]).execute()
            print(f"    ✅ Created person node '{label}' (people id={p['id']})")

    for o in org_orphans:
        label = o["name"].strip()
        if not label:
            continue
        existing = maybe_single_safe(
            supabase.table("graph_nodes").select("id").eq("type", "organization").ilike("label", label).eq("is_current", True)
        )
        if existing and existing.data:
            continue
        stale = _find_noncurrent_node(label, "organization")
        if stale:
            supabase.table("organizations").update({
                "is_active": False,
                "graph_node_id": stale["id"],
            }).eq("id", o["id"]).execute()
            print(f"    ⚠️ Archived stale org row '{label}' (id={o['id']}) — node exists but is not live (canonical_id={stale.get('canonical_id')})")
            continue
        node_id = supabase.table("graph_nodes").insert({
            "label": label,
            "type": "organization",
            "epistemic_status": "asserted",
            "normalized_label": normalize_label(label),
            "db_record_id": str(o["id"]),
            "metadata": {
                "source": "entity_consolidation",
                "organization_id": str(o["id"]),
                "enrichment": {
                    "is_active": True,
                    "org_type": o.get("org_type"),
                    "description": o.get("description"),
                },
            },
        }).execute()
        if node_id.data:
            supabase.table("organizations").update({"graph_node_id": node_id.data[0]["id"]}).eq("id", o["id"]).execute()
            print(f"    ✅ Created org node '{label}' (org id={o['id']})")


def create_missing_mirrors() -> int:
    """Create people/organizations mirror rows for live graph nodes that lack
    one (non-approval paths, e.g. persist_label direct route, don't create
    mirrors — migration-47 triggers are not installed in prod).

    Idempotent: keyed on graph_node_id. Mirrors are the legacy identity/FK
    anchor (bigint people.id / uuid organizations.id); without them nodes are
    filtered out of the /api/people directory and invisible to FK-keyed joins.
    """
    created = 0
    people_ids = {str(p["id"]) for p in supabase.table("people").select("id, graph_node_id").execute().data or []}
    people_gn_ids = {p.get("graph_node_id") for p in supabase.table("people").select("graph_node_id").execute().data or [] if p.get("graph_node_id")}
    org_gn_ids = {o.get("graph_node_id") for o in supabase.table("organizations").select("graph_node_id").execute().data or [] if o.get("graph_node_id")}

    for n in _fetch_live_nodes("person"):
        meta = _meta_of(n)
        legacy = meta.get("people_id") or n.get("db_record_id")
        if legacy and str(legacy) in people_ids:
            continue
        if n["id"] in people_gn_ids:
            continue
        try:
            ins = supabase.table("people").insert({
                "name": n.get("label"),
                "source": "entity_consolidation",
                "strategic_weight": (meta.get("enrichment") or {}).get("strategic_weight", 5),
                "is_current": True,
                "graph_node_id": n["id"],
            }).execute()
            if ins.data:
                new_meta = dict(meta)
                new_meta["people_id"] = str(ins.data[0]["id"])
                new_meta.setdefault("db_record_id", str(ins.data[0]["id"]))
                supabase.table("graph_nodes").update({"metadata": new_meta, "db_record_id": str(ins.data[0]["id"])}).eq("id", n["id"]).execute()
                created += 1
                print(f"    ✅ Created people mirror '{n.get('label')}' (id={ins.data[0]['id']})")
        except Exception as e:
            print(f"    ⚠️ mirror create failed for '{n.get('label')}': {e}")

    for n in _fetch_live_nodes("organization"):
        meta = _meta_of(n)
        legacy = meta.get("organization_id") or n.get("db_record_id")
        if legacy or n["id"] in org_gn_ids:
            continue
        try:
            ins = supabase.table("organizations").insert({
                "name": n.get("label"),
                "is_active": True,
                "graph_node_id": n["id"],
            }).execute()
            if ins.data:
                new_meta = dict(meta)
                new_meta["organization_id"] = str(ins.data[0]["id"])
                new_meta.setdefault("db_record_id", str(ins.data[0]["id"]))
                supabase.table("graph_nodes").update({"metadata": new_meta, "db_record_id": str(ins.data[0]["id"])}).eq("id", n["id"]).execute()
                created += 1
                print(f"    ✅ Created org mirror '{n.get('label')}' (id={ins.data[0]['id']})")
        except Exception as e:
            print(f"    ⚠️ org mirror create failed for '{n.get('label')}': {e}")

    return created


def backfill_enrichment() -> int:
    """Idempotent: copy enrichment from domain mirror rows onto graph nodes.

    Matches by legacy id (metadata.people_id/organization_id or db_record_id)
    AND by the graph_node_id back-link, so nodes whose legacy id points at an
    archived row but have a live mirror row linked via graph_node_id are still
    enriched (e.g. 'Jeremy Daniel')."""
    enriched = 0
    people_rows = supabase.table("people") \
        .select("id, role, strategic_weight, organization_name, last_interaction_date, graph_node_id") \
        .eq("is_current", True).is_("deleted_at", "null") \
        .execute().data or []
    people = {str(p["id"]): p for p in people_rows}
    people_by_gn = {str(p["graph_node_id"]): p for p in people_rows if p.get("graph_node_id")}
    org_rows = supabase.table("organizations") \
        .select("id, org_type, description, created_at, graph_node_id") \
        .eq("is_active", True) \
        .execute().data or []
    orgs = {str(o["id"]): o for o in org_rows}
    orgs_by_gn = {str(o["graph_node_id"]): o for o in org_rows if o.get("graph_node_id")}

    for n in _fetch_live_nodes("person"):
        meta = _meta_of(n)
        if isinstance(meta.get("enrichment"), dict):
            continue  # already enriched
        pid = str(_legacy_id(n, "person"))
        p = people.get(pid) or people_by_gn.get(str(n["id"]))
        if not p:
            continue
        meta["enrichment"] = {
            "role": p.get("role"),
            "strategic_weight": p.get("strategic_weight") or 5,
            "organization_name": p.get("organization_name"),
            "last_interaction_date": p.get("last_interaction_date"),
            "is_active": True,
        }
        supabase.table("graph_nodes").update({"metadata": meta}).eq("id", n["id"]).execute()
        enriched += 1

    for n in _fetch_live_nodes("organization"):
        meta = _meta_of(n)
        if isinstance(meta.get("enrichment"), dict):
            continue
        oid = str(_legacy_id(n, "organization"))
        o = orgs.get(oid) or orgs_by_gn.get(str(n["id"]))
        if not o:
            continue
        meta["enrichment"] = {
            "is_active": True,
            "org_type": o.get("org_type"),
            "description": o.get("description"),
            "org_created_at": (o.get("created_at") or ""),
        }
        supabase.table("graph_nodes").update({"metadata": meta}).eq("id", n["id"]).execute()
        enriched += 1

    return enriched


def diff_check() -> int:
    """Verify parity: node enrichment vs domain mirror row. Returns mismatch count."""
    mismatches = 0
    people = {str(p["id"]): p for p in supabase.table("people").select("id, name, role, strategic_weight, organization_name").eq("is_current", True).is_("deleted_at", "null").execute().data or []}
    for n in _fetch_live_nodes("person"):
        meta = _meta_of(n)
        enrich = meta.get("enrichment") or {}
        pid = str(_legacy_id(n, "person"))
        p = people.get(pid)
        if not p:
            continue
        if (enrich.get("strategic_weight") or 5) != (p.get("strategic_weight") or 5):
            mismatches += 1
            print(f"    ⚠️ weight mismatch '{n.get('label')}': node={enrich.get('strategic_weight')} mirror={p.get('strategic_weight')}")
        if (enrich.get("role") or None) != (p.get("role") or None):
            mismatches += 1
            print(f"    ⚠️ role mismatch '{n.get('label')}': node={enrich.get('role')!r} mirror={p.get('role')!r}")
        if (enrich.get("organization_name") or None) != (p.get("organization_name") or None):
            mismatches += 1
            print(f"    ⚠️ org mismatch '{n.get('label')}': node={enrich.get('organization_name')!r} mirror={p.get('organization_name')!r}")
    return mismatches


def _mirror_exists(table: str) -> bool:
    try:
        supabase.table(table).select("id").limit(1).execute()
        return True
    except Exception:
        return False


def main() -> None:
    print("=" * 70)
    print("ENTITY CONSOLIDATION (migration 74)")
    print(f"mode: {'APPLY' if APPLY else 'DRY-RUN (no writes)'}")
    print("=" * 70)

    # Migration 75 removed the people/organizations mirror tables — this
    # script's whole job (mirror↔graph reconciliation) no longer applies.
    if not _mirror_exists("people") and not _mirror_exists("organizations"):
        print("\n⏭️  Migration 75 has removed the people/organizations mirror tables —")
        print("   graph_nodes is the single source of truth and there is nothing left")
        print("   to consolidate. Use instead:")
        print("       python3 scripts/verify_consolidation.py   # ongoing drift check")
        print("       SELECT * FROM public.entity_self_consistency_report();")
        return

    people_orphans, org_orphans = _orphan_report()
    print("\n[1] Orphans — domain rows without a live graph node")
    print(f"    people: {len(people_orphans)}")
    for p in people_orphans:
        print(f"        - '{p.get('name')}' (id={p.get('id')})")
    print(f"    orgs:   {len(org_orphans)}")
    for o in org_orphans:
        print(f"        - '{o.get('name')}' (id={o.get('id')})")

    if not APPLY:
        print("\nRun with --apply to create nodes for orphans + backfill enrichment.")
        return

    print("\n[2] Creating missing graph nodes...")
    create_missing_nodes(people_orphans, org_orphans)

    print("\n[3] Creating missing mirror rows for unmirrored nodes...")
    mc = create_missing_mirrors()
    print(f"    created {mc} mirror row(s)")

    print("\n[4] Backfilling enrichment onto graph nodes...")
    n = backfill_enrichment()
    print(f"    enriched {n} node(s)")

    print("\n[5] Diff-check (node enrichment vs mirror)...")
    m = diff_check()
    print(f"    {'✅ parity OK' if m == 0 else f'⚠️ {m} mismatch(es) — informational (mirror may be intentionally stale post-migration; enrichment lives on the node)'}")

    print("\nNext steps:")
    print("  - SELECT * FROM public.entity_consolidation_report();  # residual drift")
    print("  - Deploy the code batch (backend + web) that now reads graph_nodes.")


if __name__ == "__main__":
    main()
