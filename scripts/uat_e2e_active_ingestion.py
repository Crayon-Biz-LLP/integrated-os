import asyncio
import os
from dotenv import load_dotenv

load_dotenv()

from fastapi.testclient import TestClient
from api.index import app
from core.services.db import tenant_aware_client, tenant_scope

TEST_TENANT = 'e87f0279-3ec0-4875-af69-49894ee9da6f'

def run_e2e_test():
    print("============================================================")
    print("E2E UAT: Active Ingestion (App Chat -> Suggestion -> Confirm)")
    print(f"Tenant: Test (Elon Musk) - {TEST_TENANT}")
    print("============================================================\n")


    # Mock modal to force inline execution
    import sys
    sys.modules['modal'] = None

    client = TestClient(app)
    
    # We need to mock the JWT auth for TestClient. The app uses require_api_auth.
    # api/index.py uses user = supabase.auth.get_user(token). We can mock require_api_auth.
    from api import index
    # Monkey-patch require_api_auth to just return the test tenant
    original_auth = getattr(index, 'require_api_auth', None)
    index.require_api_auth = lambda req: TEST_TENANT
    
    try:
        with tenant_scope(TEST_TENANT):
            db = tenant_aware_client()
            
            # STEP 1: App sends a message
            print("[1] App sends message: 'Meeting notes: assign John to BetaCorp, review with Sarah'")
            response = client.post("/api/send-message", json={
                "message": "Meeting notes: assign John to BetaCorp, review with Sarah",
                "local_id": "test_local_123"
            })
            
            print(f"   API /send-message Response: {response.status_code}")
            
            # Find the generated suggestion card in raw_dumps
            dumps = db.table('raw_dumps').select('id, metadata').eq('owner_id', TEST_TENANT).eq('message_type', 'suggestion').order('created_at', desc=True).limit(1).execute()
            if not dumps.data:
                print("   ❌ FAILED: No suggestion card generated.")
                return
                
            card_meta = dumps.data[0]['metadata']
            breakdown = card_meta.get('suggestion_breakdown', {})
            entity_context = card_meta.get('entity_context', {})
            source_id = dumps.data[0]['id']
            
            print("\n[2] Suggestion Card Generated!")
            print(f"   Tasks Extracted: {len(breakdown.get('suggested_actions', []))}")
            for a in breakdown.get('suggested_actions', []):
                print(f"     - [{a.get('type')}] {a.get('title')}")
                
            print(f"   Entities Detected (from context extraction): {len(breakdown.get('suggested_entities', []))}")
            for e in breakdown.get('suggested_entities', []):
                print(f"     - [{e.get('type')}] {e.get('label')} (confidence: {e.get('confidence')})")
                
            print(f"   EntityContext stored in metadata? {'Yes' if entity_context else 'No'}")
            
            # STEP 3: User confirms the card in the App
            print("\n[3] User taps Confirm on the Suggestion Card...")
            
            # Mimic Flutter payload
            confirm_payload = {
                "source_type": "message",
                "source_id": source_id,
                "selected_tasks": breakdown.get('suggested_actions', []),
                "selected_entities": breakdown.get('suggested_entities', [])
            }
            
            conf_resp = client.post("/api/suggestions/confirm", json=confirm_payload)
            print(f"   API /suggestions/confirm Response: {conf_resp.status_code}")
            
            conf_data = conf_resp.json()
            created_items = conf_data.get('created_items', [])
            print(f"   Items Created: {len(created_items)}")
            for item in created_items:
                print(f"     - [{item.get('type')}] {item.get('title')} (ID: {item.get('entity_id')})")
                
            # STEP 4: Database Verification
            print("\n[4] Database Verification:")
            tasks = db.table('tasks').select('id, title, organization_id, pending_org_id').eq('owner_id', TEST_TENANT).ilike('title', '%BetaCorp%').order('created_at', desc=True).limit(1).execute()
            
            if tasks.data:
                t = tasks.data[0]
                print(f"   ✅ Task created: '{t['title']}' (ID: {t['id']})")
                if t['organization_id']:
                    gn = db.table('graph_nodes').select('label').eq('id', t['organization_id']).execute()
                    org_name = gn.data[0]['label'] if gn.data else 'Unknown'
                    print(f"   ✅ Task successfully linked to LIVE Org: {org_name} (ID: {t['organization_id']})")
                else:
                    print(f"   ❌ Task missing organization_id!")
            else:
                print("   ❌ Task not found in database!")

            # Cleanup
            print("\n[5] Cleaning up test data...")
            if tasks.data:
                db.table('tasks').delete().eq('id', tasks.data[0]['id']).execute()
            for item in created_items:
                if item.get('type') == 'organization':
                    db.table('graph_nodes').delete().eq('id', item.get('entity_id')).execute()
            db.table('raw_dumps').delete().eq('id', source_id).execute()
            print("   ✅ Cleanup complete.")

    finally:
        if original_auth:
            index.require_api_auth = original_auth

if __name__ == "__main__":
    run_e2e_test()
