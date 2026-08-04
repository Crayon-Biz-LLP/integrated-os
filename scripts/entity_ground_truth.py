"""READ-ONLY ground-truth inventory for the entity consolidation migration.

Verifies (1) graph_nodes schema, (2) migration-47 domain-sync triggers are
installed, (3) the people/organizations ↔ graph_nodes drift inventory, and
(4) the enrichment fields that the backfill will move.

Makes NO writes. Safe to run anytime.

Usage:
    python3 scripts/entity_ground_truth.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv

load_dotenv()

from core.services.db import get_supabase  # noqa: E402

supabase = get_supabase()


def run_sql(sql: str) -> list:
    """Execute raw SQL read-only via rpc if available, else return []."""
    try:
        res = supabase.rpc("run_sql", {"query": sql}).execute()
        return res.data or []
    except Exception as e:
        # Not all projects expose run_sql — degrade gracefully
        print(f"  (rpc run_sql unavailable: {e})")
        return []


def _mirror_exists(table: str) -> bool:
    try:
        supabase.table(table).select("id").limit(1).execute()
        return True
    except Exception:
        return False


def main() -> None:
    print("=" * 70)
    print("ENTITY CONSOLIDATION — GROUND TRUTH (READ-ONLY)")
    print("=" * 70)

    if not _mirror_exists("people") and not _mirror_exists("organizations"):
        print("\n⏭️  Migration 75 removed the people/organizations mirror tables.")
        print("   The mirror↔graph drift inventory no longer applies. Use instead:")
        print("       python3 scripts/verify_consolidation.py")
        print("       python3 scripts/remove_entity_tables.py --verify")
        return

    # ── 1. graph_nodes schema ──────────────────────────────────────────────
    print("\n[1] graph_nodes columns")
    cols = run_sql("""
        SELECT column_name, data_type
        FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = 'graph_nodes'
        ORDER BY ordinal_position
    """)
    if cols:
        for c in cols:
            print(f"    {c.get('column_name','?'):24} {c.get('data_type','?')}")
    else:
        print("    (could not introspect via rpc — falling back to known usage)")

    # ── 2. Domain-sync triggers (migration 47) ─────────────────────────────
    print("\n[2] migration-47 graph→domain triggers installed?")
    trig = run_sql("""
        SELECT tgname
        FROM pg_trigger
        WHERE tgname IN (
            'trg_graph_node_insert_sync_domain',
            'trg_graph_node_delete_archive_domain',
            'trg_graph_node_soft_delete_archive_domain',
            'trg_graph_node_type_change_migrate_domain'
        )
    """)
    installed = {t.get("tgname") for t in trig}
    for name in (
        "trg_graph_node_insert_sync_domain",
        "trg_graph_node_delete_archive_domain",
        "trg_graph_node_soft_delete_archive_domain",
        "trg_graph_node_type_change_migrate_domain",
    ):
        print(f"    {'✅' if name in installed else '❌ MISSING'} {name}")

    # ── 3. People inventory ────────────────────────────────────────────────
    print("\n[3] PEOPLE inventory")
    try:
        people = supabase.table("people").select("id, name, role, strategic_weight, organization_name, is_current, deleted_at, graph_node_id").execute().data or []
        people_current = [p for p in people if p.get("is_current") and not p.get("deleted_at")]
        print(f"    total people rows:          {len(people)}")
        print(f"    current (live) people rows: {len(people_current)}")

        nodes = supabase.table("graph_nodes").select("id, label, type, metadata, db_record_id, is_current, created_at, canonical_id").eq("type", "person").execute().data or []
        nodes_live = [n for n in nodes if n.get("is_current")]
        print(f"    person graph_nodes total:   {len(nodes)}")
        print(f"    person graph_nodes live:    {len(nodes_live)}")

        # Orphans: live people rows with no live graph node
        live_node_labels = {n.get("label", "").strip().lower() for n in nodes_live}
        orphans_people = [
            p for p in people_current
            if (p.get("graph_node_id") not in {n["id"] for n in nodes_live})
            and (p.get("name") or "").strip().lower() not in live_node_labels
        ]
        print(f"    ⚠️ live people rows w/o live graph node: {len(orphans_people)}")
        for p in orphans_people[:15]:
            print(f"        - '{p.get('name')}' (id={p.get('id')}, role={p.get('role')})")

        # Graph nodes whose people_id/db_record_id point at missing people rows
        dangling = 0
        for n in nodes_live:
            pid = None
            if isinstance(n.get("metadata"), dict):
                pid = n["metadata"].get("people_id") or n["metadata"].get("db_record_id") or n.get("db_record_id")
            else:
                pid = n.get("db_record_id")
            if pid and not any(str(p.get("id")) == str(pid) for p in people):
                dangling += 1
        print(f"    ⚠️ person nodes w/ dangling people_id ref: {dangling}")

        # Strategic weight distribution (what the backfill will move)
        weights = {}
        for p in people_current:
            w = p.get("strategic_weight") or 0
            weights[w] = weights.get(w, 0) + 1
        print(f"    strategic_weight dist (live): {dict(sorted(weights.items()))}")
        roles = sum(1 for p in people_current if p.get("role"))
        orgs = sum(1 for p in people_current if p.get("organization_name"))
        print(f"    live people with role: {roles}, with organization_name: {orgs}")
    except Exception as e:
        print(f"    ERROR: {e}")

    # ── 4. Organizations inventory ─────────────────────────────────────────
    print("\n[4] ORGANIZATIONS inventory")
    try:
        orgs = supabase.table("organizations").select("id, name, is_active, org_type, graph_node_id, created_at").execute().data or []
        orgs_active = [o for o in orgs if o.get("is_active")]
        print(f"    total org rows:          {len(orgs)}")
        print(f"    active org rows:         {len(orgs_active)}")

        org_nodes = supabase.table("graph_nodes").select("id, label, type, metadata, db_record_id, is_current, created_at, canonical_id").eq("type", "organization").execute().data or []
        org_nodes_live = [n for n in org_nodes if n.get("is_current")]
        print(f"    org graph_nodes total:   {len(org_nodes)}")
        print(f"    org graph_nodes live:    {len(org_nodes_live)}")

        live_org_labels = {n.get("label", "").strip().lower() for n in org_nodes_live}
        live_org_ids = {n["id"] for n in org_nodes_live}
        orphans_orgs = [
            o for o in orgs_active
            if o.get("graph_node_id") not in live_org_ids
            and (o.get("name") or "").strip().lower() not in live_org_labels
        ]
        print(f"    ⚠️ active org rows w/o live graph node: {len(orphans_orgs)}")
        for o in orphans_orgs[:15]:
            print(f"        - '{o.get('name')}' (id={o.get('id')})")

        # How many tasks/projects reference organizations (FK anchor load)
        try:
            t = supabase.table("tasks").select("organization_id", count="exact").not_.is_("organization_id", "null").limit(1).execute()
            print(f"    tasks with organization_id set: ~{t.count}")
        except Exception as e:
            print(f"    (task FK count unavailable: {e})")

        # id unification check: how many org rows already share id with their graph node
        same_id = sum(1 for o in orgs_active if o.get("graph_node_id") == o.get("id"))
        print(f"    active orgs whose id == graph_node_id: {same_id} / {len(orgs_active)}")
    except Exception as e:
        print(f"    ERROR: {e}")

    # ── 5. Existing enrichment on graph nodes ──────────────────────────────
    print("\n[5] existing metadata.enrichment on graph nodes")
    try:
        enriched = 0
        for n in nodes_live + org_nodes_live:
            meta = n.get("metadata")
            if isinstance(meta, dict) and isinstance(meta.get("enrichment"), dict):
                enriched += 1
        print(f"    nodes already carrying enrichment: {enriched}")
    except Exception as e:
        print(f"    ERROR: {e}")

    print("\nDone. No writes performed.")


if __name__ == "__main__":
    main()
