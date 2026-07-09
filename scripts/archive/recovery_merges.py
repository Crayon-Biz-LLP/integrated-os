import os
import re

def get_env():
    with open(".env") as f:
        for line in f:
            if line.startswith("SUPABASE_URL="):
                os.environ["SUPABASE_URL"] = line.strip().split("=")[1].strip('"')
            elif line.startswith("SUPABASE_SERVICE_ROLE_KEY="):
                os.environ["SUPABASE_SERVICE_ROLE_KEY"] = line.strip().split("=")[1].strip('"')
get_env()

from core.services.db import get_supabase  # noqa: E402
supabase = get_supabase()

people_res = supabase.table('people').select('id, name, role').ilike('role', '%[MERGED INTO:%').execute()

def merge_nodes(loser_label, winner_label, node_type, create_if_missing=False):
    print(f"--- Merging {loser_label} into {winner_label} ---")
    w_res = supabase.table('graph_nodes').select('id').eq('label', winner_label).execute()
    if not w_res.data:
        print(f"  Error: Winner node '{winner_label}' not found!")
        return
    winner_id = w_res.data[0]['id']
    
    l_res = supabase.table('graph_nodes').select('id, canonical_id').eq('label', loser_label).execute()
    loser_id = None
    if not l_res.data:
        if create_if_missing:
            # Recreate it as an alias
            supabase.table('graph_nodes').insert({
                'label': loser_label,
                'type': node_type,
                'normalized_label': loser_label.strip().lower(),
                'canonical_id': winner_id,
                'metadata': {'source': 'recovery_script'}
            }).execute()
            print(f"  Recreated alias node '{loser_label}' pointing to '{winner_label}'")
            return
        else:
            print(f"  Error: Loser node '{loser_label}' not found!")
            return
    else:
        loser_id = l_res.data[0]['id']
        
    if l_res.data[0]['canonical_id'] == winner_id:
        print("  Already properly aliased. Just rewiring edges.")
        
    # Rewire edges
    loser_out = supabase.table('graph_edges').select('id, target_node_id, relationship').eq('source_node_id', loser_id).execute()
    for l_edge in (loser_out.data or []):
        w_edge = supabase.table('graph_edges').select('id').eq('source_node_id', winner_id).eq('target_node_id', l_edge['target_node_id']).eq('relationship', l_edge['relationship']).execute()
        if w_edge.data:
            supabase.table('graph_edges').delete().eq('id', l_edge['id']).execute()
            print(f"  Deleted duplicate outgoing edge {l_edge['relationship']}")
        else:
            supabase.table('graph_edges').update({'source_node_id': winner_id}).eq('id', l_edge['id']).execute()
            print(f"  Repointed outgoing edge {l_edge['relationship']}")
            
    loser_in = supabase.table('graph_edges').select('id, source_node_id, relationship').eq('target_node_id', loser_id).execute()
    for l_edge in (loser_in.data or []):
        w_edge = supabase.table('graph_edges').select('id').eq('target_node_id', winner_id).eq('source_node_id', l_edge['source_node_id']).eq('relationship', l_edge['relationship']).execute()
        if w_edge.data:
            supabase.table('graph_edges').delete().eq('id', l_edge['id']).execute()
            print(f"  Deleted duplicate incoming edge {l_edge['relationship']}")
        else:
            supabase.table('graph_edges').update({'target_node_id': winner_id}).eq('id', l_edge['id']).execute()
            print(f"  Repointed incoming edge {l_edge['relationship']}")
            
    # Set canonical_id
    supabase.table('graph_nodes').update({'canonical_id': winner_id}).eq('id', loser_id).execute()
    print("  Done.")

# 1. Recover People
for p in people_res.data:
    if p['name'] == 'Bhanu Nandwani':
        continue
    # Extract the last [MERGED INTO: X]
    matches = re.findall(r'\[MERGED INTO:\s*(.*?)\]', p['role'])
    if matches:
        winner = matches[-1].strip()
        merge_nodes(p['name'], winner, 'person')

# 2. Recover Organizations
orgs = [
    ('Crayon Biz', 'Crayon'),
    ('Qhord Inc', 'Qhord'),
    ('Chennai North Fellowship', 'Ashraya Chennai North')
]

for loser, winner in orgs:
    merge_nodes(loser, winner, 'organization', create_if_missing=True)

