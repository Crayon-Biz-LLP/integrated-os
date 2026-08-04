import os
import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from supabase import create_client
from dotenv import load_dotenv
from core.services.db import maybe_single_safe

load_dotenv()

def validate_deployment(deploy_timestamp: str):
    supabase = create_client(
        os.getenv("SUPABASE_URL"),
        os.getenv("SUPABASE_SERVICE_ROLE_KEY")
    )
    
    print("--- VALIDATION WINDOW ---")
    
    # 1. Leak check: no orgs slipped through Step 1.5
    res = supabase.table('graph_nodes').select('label, type, id, created_at').eq('type', 'organization').gt('created_at', deploy_timestamp).execute()
    
    if res.data:
        # Migration 75: orgs are graph nodes only — the leak check is now a
        # self-canonical identity check (node must carry its own id + enrichment).
        leaked = []
        import json as _json
        for row in res.data:
            org_check = maybe_single_safe(supabase.table('graph_nodes').select('metadata, db_record_id').eq('id', row['id']))
            meta = (org_check.data.get('metadata') or {}) if org_check and org_check.data else {}
            if isinstance(meta, str):
                try:
                    meta = _json.loads(meta)
                except Exception:
                    meta = {}
            if (meta.get('organization_id') != row['id']) or \
               ((org_check.data or {}).get('db_record_id') != row['id']):
                pend = maybe_single_safe(supabase.table('pending_nodes').select('id').eq('label', row['label']))
                if not getattr(pend, 'data', None):
                    leaked.append(row)
        if leaked:
            print(f"❌ LEAK DETECTED: {len(leaked)} organizations slipped through.")
            for org in leaked:
                print(f"  - {org['label']}")
        else:
            print("✅ No organization leak detected.")
    else:
        print("✅ No organization leak detected.")
        
    # 2. Check pending_nodes routing
    # The SDK count doesn't group, so we group locally
    p_nodes_all = supabase.table('pending_nodes').select('status').gt('created_at', deploy_timestamp).execute()
    
    status_counts = {}
    for row in p_nodes_all.data:
        st = row.get('status')
        status_counts[st] = status_counts.get(st, 0) + 1
        
    print("Pending Node Routing Post-Deploy:")
    for st, c in status_counts.items():
        print(f"  {st}: {c}")

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python validate_deployment.py <timestamp>")
    else:
        validate_deployment(sys.argv[1])
