import os
import json
from supabase import create_client, Client
from dotenv import load_dotenv

dotenv_path = os.path.join(os.path.dirname(__file__), '..', '..', '.env')
load_dotenv(dotenv_path)

supabase_url = os.getenv("SUPABASE_URL")
supabase_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY")

if not supabase_url or not supabase_key:
    raise ValueError("SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must be set")

supabase: Client = create_client(supabase_url, supabase_key)


def migrate_people():
    print("Fetching people from people table...")
    result = supabase.table("people").select("id, name").execute()
    people = result.data or []
    print(f"Found {len(people)} people")

    if not people:
        return 0

    node_records = []
    for person in people:
        node_records.append({
            "label": person["name"],
            "type": "person",
            "metadata": {
                "legacy_id": person["id"],
                "origin": "people_table"
            }
        })

    supabase.table("graph_nodes").upsert(
        node_records,
        on_conflict="label"
    ).execute()

    return len(node_records)


def migrate_projects():
    print("🚀 Fetching projects from legacy table...")
    # Using 'name' instead of 'title' to match your Supabase schema
    res = supabase.table('projects').select('id, name, description, org_tag').execute()
    projects = res.data or []
    
    count = 0
    for p in projects:
        # Map the project to the graph
        node_data = {
            "label": p['name'], # This is the primary name for the graph
            "type": "project",
            "metadata": {
                "legacy_id": p['id'],
                "origin": "projects_table",
                "org_tag": p.get('org_tag'),
                "description": p.get('description', '')
            }
        }
        
        try:
            # Upsert into graph_nodes to prevent duplicates
            supabase.table('graph_nodes').upsert(node_data, on_conflict='label').execute()
            count += 1
        except Exception as e:
            print(f"❌ Error bridging project {p['name']}: {e}")

    print(f"✅ Successfully bridged {count} projects into the graph.")
    return count


def run_migration():
    people_count = migrate_people()
    projects_count = migrate_projects()

    total = people_count + projects_count
    print(f"\nMigration complete!")
    print(f"  People bridged: {people_count}")
    print(f"  Projects bridged: {projects_count}")
    print(f"  Total nodes migrated: {total}")


if __name__ == "__main__":
    run_migration()