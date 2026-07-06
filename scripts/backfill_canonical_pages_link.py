import asyncio
from core.services.db import get_supabase

async def main():
    supabase = get_supabase()
    pages_res = supabase.table('canonical_pages').select('id, title').eq('is_current', True).execute()
    pages = pages_res.data or []
    print(f"Found {len(pages)} current canonical pages.")
    
    for page in pages:
        gn_res = supabase.table('graph_nodes').select('id').ilike('label', page['title']).limit(1).execute()
        if gn_res and gn_res.data:
            gn_uuid = gn_res.data[0]['id']
            print(f"Linking page {page['title']} (ID {page['id']}) to graph node {gn_uuid}")
            supabase.table('canonical_pages').update({'entity_id': gn_uuid}).eq('id', page['id']).execute()
            supabase.table('graph_nodes').update({'canonical_page_id': page['id']}).eq('id', gn_uuid).execute()
        else:
            print(f"Could not find graph node for page {page['title']}")
            
if __name__ == "__main__":
    asyncio.run(main())
