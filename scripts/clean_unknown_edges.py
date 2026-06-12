#!/usr/bin/env python3
"""One-time script: delete all graph_edges without a tracked metadata source."""
import json
import os
from supabase import create_client
from dotenv import load_dotenv

load_dotenv()

supa = create_client(os.environ['SUPABASE_URL'], os.environ['SUPABASE_SERVICE_ROLE_KEY'])

print("Fetching all edges...")
res = supa.table('graph_edges').select('id, metadata').execute()
edges = res.data
print(f"Total edges: {len(edges)}")

to_delete = []
for e in edges:
    meta = e.get('metadata', {})
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except Exception:
            meta = {}
    src = (meta or {}).get('source', '')
    if 'inference' in src or 'backfill' in src:
        to_delete.append(e['id'])

print(f"Found {len(to_delete)} edges to delete (missing trusted source).")
if to_delete:
    print("Deleting 1st batch of 100 as test...")
    # Just delete all in batches of 200
    for i in range(0, len(to_delete), 200):
        batch = to_delete[i:i+200]
        supa.table('graph_edges').delete().in_('id', batch).execute()
        print(f"Deleted {len(batch)} edges.")
print("Done.")
