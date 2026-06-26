import sys
import os
import argparse
from typing import List, Dict, Tuple
from collections import defaultdict

# Add parent dir to path so we can import core modules
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.services.db import get_supabase
from core.lib.graph_rules import normalize_label_comparison, execute_graph_node_merge

def classify_group(group: List[dict]) -> str:
    """Classify a group of duplicate nodes as AUTO_SAFE or MANUAL_REVIEW."""
    if len(group) > 2:
        return "MANUAL_REVIEW: >2 variants"
        
    n1, n2 = group[0], group[1]
    
    if n1['type'] != n2['type']:
        return "MANUAL_REVIEW: Mixed types"
        
    l1, l2 = n1['label'], n2['label']
    
    # Check acronyms/abbreviations (short all-caps strings, or big length differences)
    if (len(l1) <= 4 and l1.isupper()) or (len(l2) <= 4 and l2.isupper()):
        return "MANUAL_REVIEW: Potential acronym"
        
    if abs(len(l1) - len(l2)) > 3:
        return "MANUAL_REVIEW: Significant length difference (potential abbreviation)"
        
    # Check semantic ambiguity (same comparison key, but completely different words? 
    # normalize_label_comparison handles space/punctuation collapse, so they should be very similar)
    if normalize_label_comparison(l1) != normalize_label_comparison(l2):
        return "MANUAL_REVIEW: Comparison keys do not match exactly"
        
    return "AUTO_SAFE"

def fetch_all_active_nodes():
    supabase = get_supabase()
    all_nodes = []
    page = 0
    page_size = 1000
    while True:
        # Fetch nodes that are not merged (canonical_id is null)
        res = supabase.table("graph_nodes").select("*").is_("canonical_id", "null").range(page * page_size, (page + 1) * page_size - 1).execute()
        if not res or not res.data:
            break
        all_nodes.extend(res.data)
        if len(res.data) < page_size:
            break
        page += 1
    return all_nodes

def group_nodes(nodes: List[dict]) -> Dict[str, List[dict]]:
    groups = defaultdict(list)
    for n in nodes:
        key = f"{n['type']}|{normalize_label_comparison(n['label'])}"
        groups[key].append(n)
        
    # Filter to only groups with duplicates
    return {k: v for k, v in groups.items() if len(v) > 1}

def group_fuzzy(nodes: List[dict]) -> Dict[str, List[dict]]:
    """Secondary grouping: catch same-type person nodes where one label is a substring
    or close token variant of another (e.g. 'Abhishek' vs 'Abhishek Paul')."""
    import difflib
    person_nodes = [n for n in nodes if n.get('type') == 'person']
    groups = {}
    assigned = set()
    
    for i, n1 in enumerate(person_nodes):
        if n1['id'] in assigned:
            continue
        cluster = [n1]
        assigned.add(n1['id'])
        l1 = n1['label'].lower().strip()
        
        for j, n2 in enumerate(person_nodes):
            if n2['id'] in assigned or i == j:
                continue
            l2 = n2['label'].lower().strip()
            
            # Substring check: one label contains the other (token boundary)
            if l1 in l2 or l2 in l1:
                cluster.append(n2)
                assigned.add(n2['id'])
                continue
            
            # SequenceMatcher for token overlap
            ratio = difflib.SequenceMatcher(None, l1, l2).ratio()
            if ratio >= 0.5:
                cluster.append(n2)
                assigned.add(n2['id'])
        
        if len(cluster) > 1:
            key = f"person|fuzzy_{cluster[0]['label']}_{cluster[-1]['label']}"
            groups[key] = cluster
    
    return groups

def get_node_edge_count(supabase, node_id: str) -> int:
    out_res = supabase.table("graph_edges").select("id", count="exact").eq("source_node_id", node_id).execute()
    in_res = supabase.table("graph_edges").select("id", count="exact").eq("target_node_id", node_id).execute()
    
    out_count = out_res.count if hasattr(out_res, 'count') and out_res.count is not None else (len(out_res.data) if out_res and out_res.data else 0)
    in_count = in_res.count if hasattr(in_res, 'count') and in_res.count is not None else (len(in_res.data) if in_res and in_res.data else 0)
    
    return out_count + in_count

def select_target_and_source(group: List[dict], supabase) -> Tuple[dict, List[str]]:
    """Select the best target (canonical) node and return (target_node, [source_ids])."""
    # Prefer nodes with more edges, then longest label (preserves casing/punctuation best)
    scored = []
    for n in group:
        edges = get_node_edge_count(supabase, n['id'])
        # Score: mostly based on edges, tiebreaker on label length (prefer title case / more detail)
        # Also prefer nodes that have more uppercase letters (Title Case vs lowercase)
        caps = sum(1 for c in n['label'] if c.isupper())
        scored.append((n, edges, caps))
        
    # Sort by edges descending, then caps descending, then label length descending
    scored.sort(key=lambda x: (x[1], x[2], len(x[0]['label'])), reverse=True)
    
    target_node = scored[0][0]
    source_ids = [s[0]['id'] for s in scored[1:]]
    return target_node, source_ids

def main():
    parser = argparse.ArgumentParser(description="Clean duplicate graph nodes.")
    parser.add_argument("--dry-run", action="store_true", help="Print plan without executing")
    parser.add_argument("--apply-safe-only", action="store_true", help="Apply AUTO_SAFE merges")
    parser.add_argument("--apply-group", type=str, help="Apply merge for a specific group normalized label key")
    
    args = parser.parse_args()
    
    # Default to dry-run if no apply flags provided
    is_dry_run = args.dry_run or (not args.apply_safe_only and not args.apply_group)
    
    print("Fetching active nodes...")
    nodes = fetch_all_active_nodes()
    print(f"Total active nodes: {len(nodes)}")
    
    groups = group_nodes(nodes)
    print(f"Found {len(groups)} exact duplicate groups.\n")
    
    fuzzy_groups = group_fuzzy(nodes)
    if fuzzy_groups:
        print(f"Found {len(fuzzy_groups)} fuzzy alias groups.\n")
    
    supabase = get_supabase()
    
    safe_groups = 0
    manual_groups = 0
    
    # Process exact groups
    for key, group in groups.items():
        classification = classify_group(group)
        is_safe = classification == "AUTO_SAFE"
        
        if is_safe:
            safe_groups += 1
        else:
            manual_groups += 1
            
        print(f"Group: {key} [{classification}]")
        for i, n in enumerate(group):
            edges = get_node_edge_count(supabase, n['id'])
            print(f"  {i+1}. {n['label']} ({n['id']}) - {edges} edges")
            
        if args.apply_group and key == args.apply_group:
            print(f"--> Applying merge for requested group: {key}")
            target, sources = select_target_and_source(group, supabase)
            print(f"    Selected target: {target['label']} ({target['id']})")
            for src_id in sources:
                res = execute_graph_node_merge(src_id, target['id'], provenance="duplicate_cleanup_script")
                print(f"    Merge result: {res['message']}")
            print()
            continue
            
        if not is_dry_run and args.apply_safe_only and is_safe:
            print(f"--> Applying AUTO_SAFE merge for: {key}")
            target, sources = select_target_and_source(group, supabase)
            print(f"    Selected target: {target['label']} ({target['id']})")
            for src_id in sources:
                res = execute_graph_node_merge(src_id, target['id'], provenance="duplicate_cleanup_script")
                print(f"    Merge result: {res['message']}")
        print()
    
    # Process fuzzy groups (always MANUAL_REVIEW)
    for key, group in fuzzy_groups.items():
        manual_groups += 1
        classification = "MANUAL_REVIEW: Fuzzy alias match"
        print(f"Group: {key} [{classification}]")
        for i, n in enumerate(group):
            edges = get_node_edge_count(supabase, n['id'])
            print(f"  {i+1}. {n['label']} ({n['id']}) - {edges} edges")
        
        if args.apply_group and args.apply_group in key:
            print(f"--> Applying merge for fuzzy group: {key}")
            target, sources = select_target_and_source(group, supabase)
            print(f"    Selected target: {target['label']} ({target['id']})")
            for src_id in sources:
                res = execute_graph_node_merge(src_id, target['id'], provenance="duplicate_cleanup_script")
                print(f"    Merge result: {res['message']}")
        print()
        
    print("Summary:")
    print(f"  AUTO_SAFE groups: {safe_groups}")
    print(f"  MANUAL_REVIEW groups: {manual_groups}")
    
    if is_dry_run:
        print("\nDry run complete. Use --apply-safe-only to execute safe merges, or --apply-group 'type|key' to execute manually.")

if __name__ == "__main__":
    main()
