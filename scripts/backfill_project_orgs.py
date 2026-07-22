"""Backfill: Link existing orphan projects and people to their organizations.

Scans:
1. Projects with organization_id IS NULL for approved BELONGS_TO edges
2. People with organization_name IS NULL for approved WORKS_AT edges

Usage:
    PYTHONPATH=. python3 scripts/backfill_project_orgs.py --dry-run
    PYTHONPATH=. python3 scripts/backfill_project_orgs.py  # actually write
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from core.services.db import get_supabase
from dotenv import load_dotenv
load_dotenv()

supabase = get_supabase()
dry_run = "--dry-run" in sys.argv


def backfill_projects():
    """Find projects without org, find approved BELONGS_TO edges, update."""
    print(f"{'[DRY-RUN] ' if dry_run else ''}=== Backfilling Projects ===")
    
    r = supabase.table('projects').select('id, name').is_('organization_id', 'null').eq('is_current', True).execute()
    orphans = r.data or []
    print(f"Projects without org: {len(orphans)}")
    
    fixed = 0
    for proj in orphans:
        name = proj['name']
        pid = proj['id']
        
        # Check graph_edges for approved BELONGS_TO
        ge = supabase.table('graph_edges') \
            .select('target_node_id, source_ref') \
            .ilike('source_ref', f'%{name}%') \
            .eq('relationship', 'BELONGS_TO') \
            .eq('is_current', True) \
            .limit(5) \
            .execute()
        
        org_id = None
        for edge in (ge.data or []):
            # Get org node's db_record_id
            tn = supabase.table('graph_nodes').select('db_record_id') \
                .eq('id', edge['target_node_id']) \
                .limit(1) \
                .execute()
            if tn.data and tn.data[0].get('db_record_id'):
                org_id = tn.data[0]['db_record_id']
                break
        
        # Fallback: check pending_graph_edges for approved BELONGS_TO
        if not org_id:
            pe = supabase.table('pending_graph_edges') \
                .select('target_label') \
                .eq('source_label', name) \
                .eq('relationship', 'BELONGS_TO') \
                .eq('status', 'approved') \
                .limit(5) \
                .execute()
            for pending in (pe.data or []):
                target = pending.get('target_label')
                if target:
                    tn = supabase.table('graph_nodes').select('db_record_id') \
                        .ilike('label', target) \
                        .eq('type', 'organization') \
                        .limit(1) \
                        .execute()
                    if tn.data and tn.data[0].get('db_record_id'):
                        org_id = tn.data[0]['db_record_id']
                        break
        
        # Fallback: try to match by name (org name in project name)
        if not org_id:
            r_org = supabase.table('organizations').select('id, name').execute()
            source_lower = name.lower()
            for o in (r_org.data or []):
                if o['name'].lower() in source_lower:
                    org_id = o['id']
                    break
        
        if org_id:
            if dry_run:
                print(f"  Would update project '{name}' (ID={pid}) → org_id={str(org_id)[:12]}...")
            else:
                supabase.table('projects').update({'organization_id': str(org_id)}).eq('id', pid).execute()
                print(f"  ✅ Updated project '{name}' (ID={pid}) → org_id set")
            fixed += 1
        else:
            print(f"  ⏭️  Skipped project '{name}' — no org found")
    
    return fixed


def backfill_people():
    """Find people without org, find approved WORKS_AT edges, update."""
    print(f"\n{'[DRY-RUN] ' if dry_run else ''}=== Backfilling People ===")
    
    r = supabase.table('people').select('id, name').is_('organization_name', 'null').eq('is_current', True).execute()
    orphans = r.data or []
    print(f"People without org: {len(orphans)}")
    
    fixed = 0
    for person in orphans:
        name = person['name']
        pid = person['id']
        
        # Check pending_graph_edges for approved WORKS_AT
        pe = supabase.table('pending_graph_edges') \
            .select('target_label') \
            .eq('source_label', name) \
            .eq('relationship', 'WORKS_AT') \
            .eq('status', 'approved') \
            .limit(5) \
            .execute()
        
        org_name = None
        for pending in (pe.data or []):
            target = pending.get('target_label')
            if target:
                org_name = target
                break
        
        # Fallback: check graph_edges
        if not org_name:
            # Get person's graph node
            gn = supabase.table('graph_nodes').select('id').filter('metadata->>people_id', 'eq', str(pid)).limit(1).execute()
            if gn.data:
                node_id = gn.data[0]['id']
                ge = supabase.table('graph_edges') \
                    .select('target_node_id') \
                    .eq('source_node_id', node_id) \
                    .eq('relationship', 'WORKS_AT') \
                    .eq('is_current', True) \
                    .limit(5) \
                    .execute()
                for edge in (ge.data or []):
                    tn = supabase.table('graph_nodes').select('label').eq('id', edge['target_node_id']).limit(1).execute()
                    if tn.data and tn.data[0].get('label'):
                        org_name = tn.data[0]['label']
                        break
        
        if org_name:
            if dry_run:
                print(f"  Would update person '{name}' (ID={pid}) → org_name='{org_name}'")
            else:
                supabase.table('people').update({'organization_name': org_name}).eq('id', pid).execute()
                print(f"  ✅ Updated person '{name}' (ID={pid}) → org_name='{org_name}'")
            fixed += 1
        else:
            print(f"  ⏭️  Skipped person '{name}' — no org found")
    
    return fixed


if __name__ == '__main__':
    print(f"Mode: {'DRY RUN (no changes)' if dry_run else 'LIVE (will write changes)'}")
    p_fixed = backfill_projects()
    pe_fixed = backfill_people()
    print(f"\nTotal: {p_fixed} projects + {pe_fixed} people fixed")
    if dry_run:
        print("Run without --dry-run to apply changes.")
