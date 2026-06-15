import os
import sys
import difflib

# Add parent directory to path so we can import core modules
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

def load_env():
    with open('.env') as f:
        for line in f:
            line = line.strip()
            if '=' in line and not line.startswith('#'):
                k, v = line.split('=', 1)
                os.environ[k] = v.strip('"')

# Load env before importing core modules
load_env()

from supabase import create_client  # noqa: E402
from core.lib.graph_rules import propose_merge  # noqa: E402

def is_potential_dupe(a: str, b: str) -> bool:
    """Returns True if a and b might refer to the same entity."""
    a, b = a.lower().strip(), b.lower().strip()
    
    if a == b:
        return True
        
    if (a.startswith(b) or b.startswith(a)) and min(len(a), len(b)) >= 4:
        return True
        
    if difflib.SequenceMatcher(None, a, b).ratio() >= 0.85:
        return True
        
    return False

def run_dedup():
    supabase = create_client(os.environ['SUPABASE_URL'], os.environ['SUPABASE_SERVICE_ROLE_KEY'])
    
    print("Fetching nodes...")
    res = supabase.table('graph_nodes').select('id, label, type, canonical_id').execute()
    nodes = res.data or []
    print(f"Found {len(nodes)} nodes.")
    
    by_type = {}
    for n in nodes:
        by_type.setdefault(n['type'], []).append(n)
        
    proposals_created = 0
    skipped_existing = 0
    skipped_not_dupe = 0
    
    for t, t_nodes in by_type.items():
        for i in range(len(t_nodes)):
            for j in range(i + 1, len(t_nodes)):
                n1 = t_nodes[i]
                n2 = t_nodes[j]
                
                if n1.get('canonical_id') or n2.get('canonical_id'):
                    continue
                    
                if is_potential_dupe(n1['label'], n2['label']):
                    # Heuristic for which is canonical: prefer shorter label, or lexicographically first
                    if len(n1['label']) < len(n2['label']):
                        canonical, dupe = n1, n2
                    elif len(n1['label']) > len(n2['label']):
                        canonical, dupe = n2, n1
                    else:
                        canonical, dupe = (n1, n2) if n1['label'] < n2['label'] else (n2, n1)
                            
                    print(f"Potential dupe ({t}): '{dupe['label']}' → '{canonical['label']}'")
                    result = propose_merge(dupe['id'], canonical['id'])
                    if result['success']:
                        proposals_created += 1
                        print("  ✅ Created proposal")
                    else:
                        skipped_existing += 1
                        print(f"  ⏭️ Skipped: {result['message']}")
                else:
                    skipped_not_dupe += 1
                    
    print(f"\nFinished. {proposals_created} proposals created. {skipped_existing} already proposed. {skipped_not_dupe} pairs skipped as non-dupes.")

if __name__ == '__main__':
    run_dedup()
