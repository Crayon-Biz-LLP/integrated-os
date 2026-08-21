"""Phase 0: Fix existing tenants — create missing graph foundation.

For each tenant:
1. Create "Personal" org graph node if missing
2. Create user person node if missing (name from user_settings.context)
3. Create WORKS_WITH edge: root person → Personal
4. Create edges for other personal_orgs (Family, etc.)

Usage:
    python scripts/fix_tenant_graph_foundation.py [--dry-run] [--owner-id UUID]
"""

import asyncio
import argparse
import json
from supabase import create_client
import os


def get_client():
    url = os.environ.get('SUPABASE_URL')
    key = os.environ.get('SUPABASE_SERVICE_ROLE_KEY')
    if not url or not key:
        raise ValueError("SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY required")
    return create_client(url, key)


def parse_name_from_context(context: str) -> str:
    """Parse user name from user_settings.context (first word(s) before the dash)."""
    if not context:
        return ""
    # "John - AcmeCorp founder" → "John"
    # "Jane Doe — I'm a mother..." → "Jane Doe"
    parts = context.split(' - ')
    if len(parts) > 1:
        name = parts[0].strip()
    else:
        parts = context.split(' — ')
        name = parts[0].strip() if parts else context.strip()
    # Truncate at 50 chars max for person label
    if len(name) > 50:
        name = name[:50].strip()
    return name


def find_or_create_graph_node(client, label: str, node_type: str, owner_id: str, source: str = "tenant_fix") -> str | None:
    """Find existing graph node or create one. Returns node ID."""
    # Check if exists
    existing = client.table('graph_nodes').select('id').ilike('label', label).eq(
        'type', node_type
    ).eq('owner_id', owner_id).eq('is_current', True).limit(1).execute()
    
    if existing and existing.data:
        return existing.data[0]['id']
    
    # Create new
    res = client.table('graph_nodes').insert({
        'label': label,
        'type': node_type,
        'normalized_label': label.lower().strip(),
        'owner_id': owner_id,
        'epistemic_status': 'asserted',
        'is_current': True,
        'metadata': {'source': source},
    }).execute()
    
    if res.data:
        return res.data[0]['id']
    return None


def find_or_create_pending_edge(client, source_label: str, target_label: str, relationship: str, owner_id: str, source_text: str = "") -> bool:
    """Create a pending edge if it doesn't exist."""
    # Check if edge already exists (pending or approved)
    existing = client.table('pending_graph_edges').select('id').eq(
        'source_label', source_label
    ).eq('target_label', target_label).eq(
        'relationship', relationship
    ).eq('owner_id', owner_id).limit(1).execute()
    
    if existing and existing.data:
        return True  # Already exists
    
    # Create pending edge (source_label/target_label are the primary identifiers)
    try:
        res = client.table('pending_graph_edges').insert({
            'source_label': source_label,
            'target_label': target_label,
            'relationship': relationship,
            'owner_id': owner_id,
            'status': 'pending',
            'source_text': source_text,
            'confidence': 1.0,  # Infrastructure edge — high confidence
        }).execute()
        return bool(res.data)
    except Exception as e:
        print(f"  ⚠️ Failed to create edge {source_label} → {relationship} → {target_label}: {e}")
        return False


async def fix_tenant(client, user_id: str, personal_orgs: list, context: str, dry_run: bool = False):
    """Fix graph foundation for one tenant."""
    print(f"\n{'='*60}")
    print(f"Tenant: {user_id[:8]}...")
    
    # Parse name from context
    user_name = parse_name_from_context(context)
    if not user_name:
        print(f"  ⚠️ No name found in context: '{context}'")
        user_name = "User"  # Fallback
    print(f"  Name: {user_name}")
    print(f"  Personal orgs: {personal_orgs}")
    
    # Only create "Personal" and "Family" as personal orgs
    # Other orgs in personal_orgs are business orgs incorrectly categorized
    PERSONAL_ORG_LABELS = {"Personal", "Family"}
    
    # 1. Create "Personal" org graph node if missing
    personal_org_id = None
    if "Personal" in personal_orgs:
        if dry_run:
            # Check if already exists
            existing = client.table('graph_nodes').select('id').ilike('label', 'Personal').eq(
                'type', 'organization'
            ).eq('owner_id', user_id).eq('is_current', True).limit(1).execute()
            if existing and existing.data:
                print(f"  ✅ Personal org already exists: {existing.data[0]['id'][:8]}...")
                personal_org_id = existing.data[0]['id']
            else:
                print("  [DRY] Would create 'Personal' org graph node")
        else:
            personal_org_id = find_or_create_graph_node(client, "Personal", "organization", user_id, source="tenant_fix_personal")
            if personal_org_id:
                print(f"  ✅ Personal org: {personal_org_id[:8]}...")
            else:
                print("  ❌ Failed to create Personal org")
    
    # 2. Create other personal_orgs as graph nodes (only Personal and Family)
    for org_name in personal_orgs:
        if org_name in PERSONAL_ORG_LABELS:
            if org_name == "Personal":
                continue  # Already handled above
            # Create Family org
            if dry_run:
                print(f"  [DRY] Would create '{org_name}' org graph node")
            else:
                org_id = find_or_create_graph_node(client, org_name, "organization", user_id, source="tenant_fix_personal_org")
                if org_id:
                    print(f"  ✅ {org_name} org: {org_id[:8]}...")
        else:
            print(f"  ⏭️ Skipping '{org_name}' (not a personal org)")
    
    # 3. Create user person node if missing
    user_person_id = None
    if dry_run:
        # Check if already exists
        existing = client.table('graph_nodes').select('id').ilike('label', user_name).eq(
            'type', 'person'
        ).eq('owner_id', user_id).eq('is_current', True).limit(1).execute()
        if existing and existing.data:
            print(f"  ✅ User person already exists: {existing.data[0]['id'][:8]}...")
            user_person_id = existing.data[0]['id']
        else:
            print(f"  [DRY] Would create '{user_name}' person node")
    else:
        user_person_id = find_or_create_graph_node(client, user_name, "person", user_id, source="tenant_fix_user_person")
        if user_person_id:
            print(f"  ✅ User person: {user_person_id[:8]}...")
        else:
            print("  ❌ Failed to create user person node")
    
    # 4. Create WORKS_WITH edge: user person → Personal
    if user_person_id and personal_org_id and not dry_run:
        find_or_create_pending_edge(
            client, user_name, "Personal", "WORKS_WITH", user_id,
            source_text="tenant_fix: root → Personal"
        )
        print(f"  ✅ Edge: {user_name} → WORKS_WITH → Personal")
    
    # 5. Create WORKS_WITH edges for other personal_orgs
    for org_name in personal_orgs:
        if org_name == "Personal" or not user_person_id or dry_run:
            continue
        find_or_create_pending_edge(
            client, user_name, org_name, "WORKS_WITH", user_id,
            source_text=f"tenant_fix: root → {org_name}"
        )
        print(f"  ✅ Edge: {user_name} → WORKS_WITH → {org_name}")
    
    if dry_run:
        print("  [DRY RUN — no changes made]")


async def main(dry_run: bool = False, owner_id: str = None):
    client = get_client()
    
    # Get all users
    users = client.table('users').select('id, name').execute()
    
    # Get all user_settings
    settings = client.table('user_settings').select('user_id, personal_orgs, context').execute()
    settings_map = {}
    for s in (settings.data or []):
        # Parse personal_orgs from JSON string if needed
        personal_orgs = s.get('personal_orgs') or []
        if isinstance(personal_orgs, str):
            try:
                personal_orgs = json.loads(personal_orgs)
            except Exception:
                personal_orgs = []
        s['personal_orgs'] = personal_orgs
        settings_map[s['user_id']] = s
    
    # Get root labels from core_config
    configs = client.table('core_config').select('key, content, owner_id').eq('key', 'archive_root_label').execute()
    root_labels = {c['owner_id']: c['content'] for c in (configs.data or []) if c.get('owner_id')}
    
    tenants = users.data or []
    if owner_id:
        tenants = [u for u in tenants if u['id'] == owner_id]
    
    print(f"Fixing {len(tenants)} tenant(s)...")
    
    for user in tenants:
        uid = user['id']
        user_name = user.get('name', '')
        settings_data = settings_map.get(uid, {})
        personal_orgs = settings_data.get('personal_orgs') or []
        context = settings_data.get('context', '') or ''
        root_label = root_labels.get(uid, '')
        
        # Use root_label if available, else context, else user name
        effective_context = root_label or context or user_name or ''
        
        await fix_tenant(client, uid, personal_orgs, effective_context, dry_run)
    
    print(f"\n{'='*60}")
    print("Done.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Fix tenant graph foundation")
    parser.add_argument("--dry-run", action="store_true", help="Preview without making changes")
    parser.add_argument("--owner-id", type=str, help="Fix specific tenant only")
    args = parser.parse_args()
    
    asyncio.run(main(dry_run=args.dry_run, owner_id=args.owner_id))
