import os
import sys
import argparse
from dotenv import load_dotenv

# Load env before imports that might use it
load_dotenv()

# Setup paths
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.services.db import get_supabase  # noqa: E402

def backfill_project_org_edges(dry_run: bool = True):
    """
    Job A: Projects → Org edges
    For every project with organization_id, create a BELONGS_TO edge to its org.
    """
    print(f"\n📁 Backfill Job A: Project -> Org BELONGS_TO edges {'(DRY RUN)' if dry_run else '(LIVE RUN)'}")
    
    supabase = get_supabase()
    
    # 1. Get all projects with an organization_id
    projects_res = supabase.table('projects').select('id, name, organization_id').not_.is_('organization_id', 'null').execute()
    projects = projects_res.data or []
    
    if not projects:
        print("  No projects with organization_id found.")
        return
        
    print(f"  Found {len(projects)} projects with organization_id.")
    
    # 2. Get all organization graph nodes
    org_nodes_res = supabase.table('graph_nodes').select('id, label, db_record_id').eq('type', 'organization').execute()
    org_nodes = org_nodes_res.data or []
    org_node_map = {}
    for on in org_nodes:
        if on.get('db_record_id'):
            org_node_map[str(on['db_record_id'])] = on['label']
            
    # 3. Get all project graph nodes
    proj_nodes_res = supabase.table('graph_nodes').select('id, label, db_record_id').eq('type', 'project').execute()
    proj_nodes = proj_nodes_res.data or []
    proj_node_map = {}
    for pn in proj_nodes:
        if pn.get('db_record_id'):
            proj_node_map[str(pn['db_record_id'])] = pn['label']
            
    edges_to_create = []
    
    for p in projects:
        pid = str(p['id'])
        oid = str(p['organization_id'])
        
        proj_label = proj_node_map.get(pid, p['name'])
        org_label = org_node_map.get(oid)
        
        if not org_label:
            # Try to get it from organizations table directly if graph_nodes mapping failed
            try:
                o_res = supabase.table('organizations').select('name').eq('id', oid).execute()
                if o_res.data:
                    org_label = o_res.data[0]['name']
            except Exception:
                pass
                
        if not org_label:
            print(f"  ⚠️ Skipping project '{proj_label}' - could not resolve organization name for ID {oid}")
            continue
            
        edges_to_create.append({
            "source_label": proj_label,
            "target_label": org_label,
            "relationship": "BELONGS_TO",
            "status": "pending",
            "source_text": f"project_org_backfill:{pid}",
            "source_table": "projects",
            "source_type": "project",
            "target_type": "organization"
        })
        
    print(f"  Prepared {len(edges_to_create)} candidate edges.")
    
    if not edges_to_create:
        return
        
    # Deduplicate against existing graph edges
    from core.lib.graph_rules import insert_pending_edge
    
    created = 0
    skipped = 0
    
    for edge in edges_to_create:
        if dry_run:
            print(f"  [DRY RUN] Would create: {edge['source_label']} --[BELONGS_TO]--> {edge['target_label']}")
            continue
            
        res = insert_pending_edge(
            edge["source_label"],
            edge["target_label"],
            edge["relationship"],
            {
                "source_text": edge["source_text"],
                "source_table": edge["source_table"],
                "source_type": edge["source_type"],
                "target_type": edge["target_type"]
            }
        )
        
        if res.get("status") == "inserted":
            created += 1
            print(f"  ✅ Created pending edge: {edge['source_label']} --[BELONGS_TO]--> {edge['target_label']}")
        else:
            skipped += 1
            print(f"  ⏭️ Skipped edge: {edge['source_label']} --[BELONGS_TO]--> {edge['target_label']} (reason: {res.get('status')})")
            
    if not dry_run:
        print(f"  ✅ Job A Complete: {created} pending edges inserted, {skipped} skipped/deduped.")


def backfill_task_project_edges(dry_run: bool = True):
    """
    Job B: Active Tasks → Project edges
    For every active task with project_id that lacks a BELONGS_TO graph edge, create one.
    """
    print(f"\n📋 Backfill Job B: Active Task -> Project BELONGS_TO edges {'(DRY RUN)' if dry_run else '(LIVE RUN)'}")
    
    supabase = get_supabase()
    
    # 1. Get all active tasks with project_id
    tasks_res = supabase.table('tasks') \
        .select('id, title, project_id') \
        .in_('status', ['todo', 'in_progress', 'waiting']) \
        .eq('is_current', True) \
        .not_.is_('project_id', 'null') \
        .execute()
        
    tasks = tasks_res.data or []
    
    if not tasks:
        print("  No active tasks with project_id found.")
        return
        
    print(f"  Found {len(tasks)} active tasks with project_id.")
    
    # 2. Get all project graph nodes
    proj_nodes_res = supabase.table('graph_nodes').select('id, label, db_record_id').eq('type', 'project').execute()
    proj_nodes = proj_nodes_res.data or []
    proj_node_map = {}
    for pn in proj_nodes:
        if pn.get('db_record_id'):
            proj_node_map[str(pn['db_record_id'])] = pn['label']
            
    edges_to_create = []
    
    for t in tasks:
        tid = str(t['id'])
        pid = str(t['project_id'])
        task_title = t['title']
        
        proj_label = proj_node_map.get(pid)
        
        if not proj_label:
            # Try to get it from projects table directly
            try:
                p_res = supabase.table('projects').select('name').eq('id', pid).execute()
                if p_res.data:
                    proj_label = p_res.data[0]['name']
            except Exception:
                pass
                
        if not proj_label:
            print(f"  ⚠️ Skipping task '{task_title}' - could not resolve project name for ID {pid}")
            continue
            
        edges_to_create.append({
            "source_label": task_title,
            "target_label": proj_label,
            "relationship": "BELONGS_TO",
            "status": "pending",
            "source_text": f"task_project_backfill:{tid}",
            "source_table": "tasks",
            "source_type": "task",
            "target_type": "project"
        })
        
    print(f"  Prepared {len(edges_to_create)} candidate edges.")
    
    if not edges_to_create:
        return
        
    # Deduplicate against existing graph edges
    from core.lib.graph_rules import insert_pending_edge
    
    created = 0
    skipped = 0
    
    for edge in edges_to_create:
        if dry_run:
            print(f"  [DRY RUN] Would create: '{edge['source_label']}' --[BELONGS_TO]--> {edge['target_label']}")
            continue
            
        res = insert_pending_edge(
            edge["source_label"],
            edge["target_label"],
            edge["relationship"],
            {
                "source_text": edge["source_text"],
                "source_table": edge["source_table"],
                "source_type": edge["source_type"],
                "target_type": edge["target_type"]
            }
        )
        
        if res.get("status") == "inserted":
            created += 1
            print(f"  ✅ Created pending edge: '{edge['source_label']}' --[BELONGS_TO]--> {edge['target_label']}")
        else:
            skipped += 1
            # Don't print skips in live mode to avoid noise, usually it's just deduped
            
    if not dry_run:
        print(f"  ✅ Job B Complete: {created} pending edges inserted, {skipped} skipped/deduped.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Backfill missing graph edges")
    parser.add_argument("--live", action="store_true", help="Run in live mode (actually insert data)")
    parser.add_argument("--job", choices=["A", "B", "all"], default="all", help="Which job to run")
    
    args = parser.parse_args()
    dry_run = not args.live
    
    if args.job in ["A", "all"]:
        backfill_project_org_edges(dry_run)
        
    if args.job in ["B", "all"]:
        backfill_task_project_edges(dry_run)
