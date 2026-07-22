"""Backfill: Sync existing orphan entities between graph_nodes and domain tables.

This script cleans up existing drift that accumulated before the DB triggers
(db/47_graph_nodes_domain_sync_triggers.sql) were installed.

Two directions:
  1. graph_nodes → domain:  Find orphan graph_nodes (no matching domain row)
                            and create default domain rows.
  2. domain → graph_nodes:  Find orphan domain rows (no matching graph_node)
                            and create default graph_nodes.

Usage:
    PYTHONPATH=. python3 scripts/backfill_entity_sync.py --dry-run
    PYTHONPATH=. python3 scripts/backfill_entity_sync.py
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from core.services.db import get_supabase
from dotenv import load_dotenv
load_dotenv()

supabase = get_supabase()
dry_run = "--dry-run" in sys.argv


def backfill_graph_to_domain():
    """Find graph_nodes of type person/project/org without matching domain rows, create them."""
    print(f"\n{'[DRY-RUN] ' if dry_run else ''}=== Direction 1: graph_nodes → domain rows ===")

    fixed = {"person": 0, "project": 0, "organization": 0}

    for node_type in ('person', 'project', 'organization'):
        # Get all graph_nodes of this type
        nodes = supabase.table('graph_nodes') \
            .select('id, label, type, db_record_id') \
            .eq('type', node_type) \
            .eq('is_current', True) \
            .execute()
        nodes_data = nodes.data or []
        print(f"\n  Checking {len(nodes_data)} graph_nodes of type '{node_type}'...")

        for node in nodes_data:
            db_id = node.get('db_record_id')
            label = node['label'].strip()
            node_id = node['id']

            # Check if domain row exists
            exists = False
            if node_type == 'person':
                if db_id:
                    r = supabase.table('people').select('id').eq('id', db_id).eq('is_current', True).limit(1).execute()
                    if r.data:
                        exists = True
                if not exists:
                    # Try by name
                    r = supabase.table('people').select('id').ilike('name', label).eq('is_current', True).limit(1).execute()
                    if r.data:
                        exists = True
                        # Back-fill db_record_id
                        if not dry_run:
                            supabase.table('graph_nodes').update({'db_record_id': str(r.data[0]['id'])}).eq('id', node_id).execute()

            elif node_type == 'project':
                if db_id:
                    r = supabase.table('projects').select('id').eq('id', db_id).eq('is_current', True).limit(1).execute()
                    if r.data:
                        exists = True
                if not exists:
                    r = supabase.table('projects').select('id').ilike('name', label).eq('is_current', True).limit(1).execute()
                    if r.data:
                        exists = True
                        if not dry_run:
                            supabase.table('graph_nodes').update({'db_record_id': str(r.data[0]['id'])}).eq('id', node_id).execute()

            elif node_type == 'organization':
                if db_id:
                    r = supabase.table('organizations').select('id').eq('id', db_id).eq('is_active', True).limit(1).execute()
                    if r.data:
                        exists = True
                if not exists:
                    r = supabase.table('organizations').select('id').ilike('name', label).eq('is_active', True).limit(1).execute()
                    if r.data:
                        exists = True
                        if not dry_run:
                            supabase.table('graph_nodes').update({'db_record_id': str(r.data[0]['id'])}).eq('id', node_id).execute()

            if exists:
                continue

            # No domain row found — create one
            if dry_run:
                print(f"    Would create {node_type} row for graph_node '{label}' (ID={node_id})")
                fixed[node_type] += 1
            else:
                try:
                    if node_type == 'person':
                        r = supabase.table('people').insert({
                            'name': label,
                            'source': 'graph_backfill',
                            'strategic_weight': 5,
                            'is_current': True,
                        }).execute()
                        if r.data:
                            domain_id = r.data[0]['id']
                            supabase.table('graph_nodes').update({'db_record_id': str(domain_id)}).eq('id', node_id).execute()
                            supabase.table('people').update({'graph_node_id': node_id}).eq('id', domain_id).execute()
                            print(f"    ✅ Created people row for '{label}' (graph_node_id={node_id})")
                            fixed[node_type] += 1

                    elif node_type == 'project':
                        r = supabase.table('projects').insert({
                            'name': label,
                            'status': 'active',
                            'is_active': True,
                            'is_current': True,
                        }).execute()
                        if r.data:
                            domain_id = r.data[0]['id']
                            supabase.table('graph_nodes').update({'db_record_id': str(domain_id)}).eq('id', node_id).execute()
                            print(f"    ✅ Created projects row for '{label}' (graph_node_id not stored)")

                    elif node_type == 'organization':
                        r = supabase.table('organizations').insert({
                            'name': label,
                            'is_active': True,
                        }).execute()
                        if r.data:
                            domain_id = r.data[0]['id']
                            supabase.table('graph_nodes').update({'db_record_id': str(domain_id)}).eq('id', node_id).execute()
                            supabase.table('organizations').update({'graph_node_id': node_id}).eq('id', domain_id).execute()
                            print(f"    ✅ Created organizations row for '{label}' (graph_node_id={node_id})")
                            fixed[node_type] += 1

                except Exception as e:
                    print(f"    ❌ Failed to create {node_type} row for '{label}': {e}")

    total = sum(fixed.values())
    print(f"\n  Direction 1 total: {total} domain rows would be created." if dry_run else f"\n  Direction 1 total: {total} domain rows created.")
    return total


def backfill_domain_to_graph():
    """Find domain rows without matching graph_nodes, create them."""
    print(f"\n{'[DRY-RUN] ' if dry_run else ''}=== Direction 2: domain rows → graph_nodes ===")

    fixed = {"person": 0, "project": 0, "organization": 0}

    # ── People ──
    people = supabase.table('people') \
        .select('id, name') \
        .eq('is_current', True) \
        .is_('graph_node_id', 'null') \
        .execute()
    people_data = people.data or []
    print(f"\n  Checking {len(people_data)} people rows without graph_node_id...")

    for p in people_data:
        pid = p['id']
        name = p['name'].strip()

        # Check if a graph_node already exists for this person (by label)
        gn = supabase.table('graph_nodes') \
            .select('id') \
            .eq('type', 'person') \
            .ilike('label', name) \
            .eq('is_current', True) \
            .limit(1) \
            .execute()

        if gn.data:
            # Graph node exists but graph_node_id wasn't set — back-link
            if not dry_run:
                supabase.table('people').update({'graph_node_id': gn.data[0]['id']}).eq('id', pid).execute()
                print(f"    🔗 Back-linked people '{name}' → graph_node_id={gn.data[0]['id']}")
            fixed['person'] += 1
        else:
            if dry_run:
                print(f"    Would create graph_node for people '{name}' (ID={pid})")
                fixed['person'] += 1
            else:
                try:
                    r = supabase.table('graph_nodes').upsert({
                        'label': name,
                        'type': 'person',
                        'db_record_id': str(pid),
                        'is_current': True,
                        'normalized_label': name.lower().strip(),
                        'metadata': {'source': 'domain_backfill', 'people_id': str(pid)},
                    }, on_conflict='normalized_label, type').execute()
                    if r.data:
                        gn_id = r.data[0]['id']
                        supabase.table('people').update({'graph_node_id': gn_id}).eq('id', pid).execute()
                    print(f"    ✅ Created graph_node for people '{name}'")
                    fixed['person'] += 1
                except Exception as e:
                    print(f"    ❌ Failed: {e}")

    # ── Projects ──
    projects = supabase.table('projects') \
        .select('id, name') \
        .eq('is_current', True) \
        .execute()
    projects_data = projects.data or []
    # Filter to those WITHOUT matching graph_node
    orphan_projects = []
    for p in projects_data:
        pid = p['id']
        name = p['name'].strip()
        # Check by db_record_id first, then by label
        gn = supabase.table('graph_nodes') \
            .select('id') \
            .eq('type', 'project') \
            .eq('db_record_id', str(pid)) \
            .eq('is_current', True) \
            .limit(1) \
            .execute()
        if not gn.data:
            gn2 = supabase.table('graph_nodes') \
                .select('id') \
                .eq('type', 'project') \
                .ilike('label', name) \
                .eq('is_current', True) \
                .limit(1) \
                .execute()
            if not gn2.data:
                orphan_projects.append(p)

    print(f"\n  Checking {len(orphan_projects)} projects rows without graph_nodes...")
    for p in orphan_projects:
        if dry_run:
            print(f"    Would create graph_node for project '{p['name']}' (ID={p['id']})")
            fixed['project'] += 1
        else:
            try:
                supabase.table('graph_nodes').upsert({
                    'label': p['name'].strip(),
                    'type': 'project',
                    'db_record_id': str(p['id']),
                    'is_current': True,
                    'normalized_label': p['name'].strip().lower(),
                    'metadata': {'source': 'domain_backfill', 'project_id': str(p['id'])},
                }, on_conflict='normalized_label, type').execute()
                print(f"    ✅ Created graph_node for project '{p['name']}'")
                fixed['project'] += 1
            except Exception as e:
                print(f"    ❌ Failed: {e}")

    # ── Organizations ──
    orgs = supabase.table('organizations') \
        .select('id, name, graph_node_id') \
        .eq('is_active', True) \
        .is_('graph_node_id', 'null') \
        .execute()
    orgs_data = orgs.data or []
    print(f"\n  Checking {len(orgs_data)} organizations rows without graph_node_id...")
    for o in orgs_data:
        if dry_run:
            print(f"    Would create graph_node for org '{o['name']}' (ID={o['id']})")
            fixed['organization'] += 1
        else:
            try:
                r = supabase.table('graph_nodes').upsert({
                    'label': o['name'].strip(),
                    'type': 'organization',
                    'db_record_id': str(o['id']),
                    'is_current': True,
                    'normalized_label': o['name'].strip().lower(),
                    'metadata': {'source': 'domain_backfill', 'organization_id': str(o['id'])},
                }, on_conflict='normalized_label, type').execute()
                if r.data:
                    supabase.table('organizations').update({'graph_node_id': r.data[0]['id']}).eq('id', o['id']).execute()
                    print(f"    ✅ Created graph_node for org '{o['name']}'")
                    fixed['organization'] += 1
            except Exception as e:
                print(f"    ❌ Failed: {e}")

    total = sum(fixed.values())
    print(f"\n  Direction 2 total: {total} graph_nodes would be created/linked." if dry_run else f"\n  Direction 2 total: {total} graph_nodes created/linked.")
    return total


if __name__ == '__main__':
    print(f"{'[DRY RUN] ' if dry_run else '[LIVE] '}Backfill entity sync")
    print("=" * 60)
    d1 = backfill_graph_to_domain()
    d2 = backfill_domain_to_graph()
    print(f"\n{'=' * 60}")
    print(f"Grand total: {d1 + d2} entities to fix." if dry_run else f"Grand total: {d1 + d2} entities fixed.")
    if dry_run:
        print("Run without --dry-run to apply changes.")
