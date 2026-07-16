"""
Dedupe Pending Nodes — Background deduplication for pending_nodes.

Fuzzy-matches pending nodes against live graph_nodes table.
When a match above threshold is found, sets status='merge_proposed' 
and merge_candidate_id on the pending node, so the Decisions UI 
can show it as an automatic merge candidate.
"""
import sys
from core.services.db import get_supabase
from core.lib.graph_rules import find_similar_node

supabase = get_supabase()

def dedupe_pending():
    from core.lib.node_tables import insert_merge_proposal
    
    # Get all pending nodes not yet matched
    result = supabase.table('pending_nodes') \
        .select('id, label, node_type') \
        .eq('status', 'pending') \
        .execute()
    
    pending = result.data or []
    print(f"[DEDUPE] Scanning {len(pending)} pending nodes...")
    
    proposed = 0
    for node in pending:
        matches = find_similar_node(node['label'], node['node_type'], threshold=0.55)
        
        if matches:
            best = matches[0]
            if best['type'] != node['node_type']:
                continue
                
            try:
                # Write merge proposal to merge_proposals table
                mp_id = insert_merge_proposal(
                    source_label=node['label'],
                    source_type=node['node_type'],
                    target_node_id=best['id'],
                    target_label=best['label'],
                    rationale=f"Auto-dedup (score={best['score']})",
                )
                if mp_id:
                    proposed += 1
                    print(f"  [+] Proposed merge: '{node['label']}' -> '{best['label']}' (score={best['score']})")
            except Exception as e:
                print(f"  [!] Error proposing merge for '{node['label']}': {e}")
                
    print(f"[DEDUPE] Done. Proposed {proposed} merges.")
    return proposed

if __name__ == "__main__":
    print("Starting dedupe_pending...")
    count = dedupe_pending()
    print(f"Total merge proposals: {count}")
    sys.exit(0)
