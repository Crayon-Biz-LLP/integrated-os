"""
Dedupe Pending Nodes — Background deduplication for pending_graph_nodes.

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
    # Get all pending nodes that haven't been proposed for merge yet
    result = supabase.table('pending_graph_nodes') \
        .select('id, label, type') \
        .eq('status', 'pending') \
        .is_('merge_candidate_id', 'null') \
        .execute()
    
    pending = result.data or []
    print(f"[DEDUPE] Scanning {len(pending)} pending nodes...")
    
    proposed = 0
    for node in pending:
        # We only try to dedupe people, organizations, and projects (ignore concept/resource for now if any)
        # Actually find_similar_node filters by type so it's safe.
        matches = find_similar_node(node['label'], node['type'], threshold=0.55)
        
        if matches:
            best = matches[0]
            # Double check the types match just in case
            if best['type'] != node['type']:
                continue
                
            try:
                supabase.table('pending_graph_nodes') \
                    .update({
                        'status': 'merge_proposed',
                        'merge_candidate_id': best['id'],
                        'confidence': best['score']
                    }) \
                    .eq('id', node['id']) \
                    .execute()
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
