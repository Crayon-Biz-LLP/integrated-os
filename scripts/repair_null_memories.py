import sys
import os

# Add parent dir to path so we can import core modules
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.services.db import get_supabase
from core.pulse.entity_resolver import resolve_entities_from_text

def run():
    supabase = get_supabase()
    
    print("Fetching memories with null org_id and project_id from the last 2 days...")
    
    # We want memories that might have missed entity linkage due to the bug
    # Just look at the last 3 days
    try:
        res = supabase.table('memories') \
            .select('id, content') \
            .is_('organization_id', 'null') \
            .is_('project_id', 'null') \
            .gte('created_at', '2026-06-25T00:00:00Z') \
            .execute()
    except Exception as e:
        print(f"Failed to fetch memories: {e}")
        return
        
    memories = res.data or []
    print(f"Found {len(memories)} candidate memories to check.")
    
    fixed = 0
    for mem in memories:
        mem_id = mem['id']
        content = mem['content']
        
        org_id, proj_id, reason = resolve_entities_from_text(content)
        
        if org_id or proj_id:
            print(f"Memory {mem_id}: Match found -> Org: {org_id}, Proj: {proj_id} (Reason: {reason})")
            update_data = {}
            if org_id:
                update_data['organization_id'] = org_id
            if proj_id:
                update_data['project_id'] = proj_id
            
            try:
                supabase.table('memories').update(update_data).eq('id', mem_id).execute()
                fixed += 1
                print(f"  -> Successfully updated memory {mem_id}")
            except Exception as e:
                print(f"  -> Failed to update memory {mem_id}: {e}")
        else:
            print(f"Memory {mem_id}: No deterministic match found.")
            
    print(f"\nRepair complete. Fixed {fixed} out of {len(memories)} memories.")

if __name__ == '__main__':
    run()
