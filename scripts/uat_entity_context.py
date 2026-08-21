"""Mock UAT for Entity Context Pipeline — Test tenant."""
import asyncio
import os
from supabase import create_client
from core.services.db import tenant_scope


async def main():
    client = create_client(
        os.environ.get('SUPABASE_URL'),
        os.environ.get('SUPABASE_SERVICE_ROLE_KEY')
    )

    TEST_TENANT = 'e87f0279-3ec0-4875-af69-49894ee9da6f'

    print('=' * 60)
    print('MOCK UAT: Entity Context Pipeline')
    print('Tenant: Test (Elon Musk)')
    print('=' * 60)

    # ── 1. Current State ────────────────────────────────────────────────
    print('\n[1] Current State:')
    nodes = client.table('graph_nodes').select('id, label, type').eq(
        'owner_id', TEST_TENANT).eq('is_current', True).execute()
    print(f'   Graph nodes: {len(nodes.data)}')
    for n in nodes.data:
        print(f'     - {n["label"]} ({n["type"]})')

    tasks = client.table('tasks').select(
        'id, title, organization_id, pending_org_id'
    ).eq('owner_id', TEST_TENANT).execute()
    print(f'   Tasks: {len(tasks.data)}')
    orphan_tasks = [t for t in tasks.data
                    if not t.get('organization_id') and not t.get('pending_org_id')]
    print(f'   Orphan tasks (no org): {len(orphan_tasks)}')
    for t in orphan_tasks:
        print(f'     - "{t["title"]}"')

    memories = client.table('memories').select(
        'id, content, organization_id, pending_org_id'
    ).eq('owner_id', TEST_TENANT).execute()
    print(f'   Memories: {len(memories.data)}')
    orphan_memories = [m for m in memories.data
                       if not m.get('organization_id') and not m.get('pending_org_id')]
    print(f'   Orphan memories (no org): {len(orphan_memories)}')

    # ── 2. Entity Extraction ────────────────────────────────────────────
    print('\n[2] Testing Entity Extraction:')
    from core.lib.entity_context import extract_context_from_source

    test_messages = [
        'Call John about AcmeCorp trip finances',
        'Meeting with BetaCorp team tomorrow',
        'Buy groceries from LPG store',
        'Review XYZ proposal document',
    ]

    for msg in test_messages:
        ctx = await extract_context_from_source(msg, TEST_TENANT)
        org = ctx.organization_name or ctx.pending_org_label or '(none)'
        persons = ctx.person_names or []
        method = ctx.extraction_method
        print(f'   "{msg}"')
        print(f'     → Org: {org}, Persons: {persons or "(none)"}, Method: {method}')
        print(f'     → org_id={ctx.organization_id}, pending_org_id={ctx.pending_org_id}')

    # ── 3. Task Creation with EntityContext ──────────────────────────────
    print('\n[3] Testing Task Creation with EntityContext:')
    from core.pulse.tools import create_task_direct
    from core.lib.entity_context import EntityContext

    # Test A: Empty EntityContext — should fall back to Personal
    empty_ctx = EntityContext(
        extraction_method='test_uat_empty',
        source_text='Test task from UAT with empty context'
    )
    with tenant_scope(TEST_TENANT):
        task_id_a = await create_task_direct(
        title='UAT Task A - Empty Context (should get Personal)',
                entity_context=empty_ctx
    )
    task_id_a = task_id_a['task_id']
    print(f'   Task A created: {task_id_a}')
    if task_id_a:
        task_a = client.table('tasks').select(
            'id, title, organization_id, pending_org_id'
        ).eq('id', task_id_a).single().execute()
        has_org = bool(task_a.data.get('organization_id') or task_a.data.get('pending_org_id'))
        print(f'   Task A has org linkage: {has_org}')
        print(f'     organization_id: {task_a.data.get("organization_id")}')
        print(f'     pending_org_id: {task_a.data.get("pending_org_id")}')
        client.table('tasks').delete().eq('id', task_id_a).execute()
        print('   ✓ Cleaned up Task A')

    # Test B: EntityContext with person names — verify person linkage
    person_ctx = EntityContext(
        person_names=['John Smith'],
        extraction_method='test_uat_person',
        source_text='Test task with person'
    )
    with tenant_scope(TEST_TENANT):
        task_id_b = await create_task_direct(
        title='UAT Task B - With Person',
                entity_context=person_ctx
    )
    task_id_b = task_id_b['task_id']
    print(f'   Task B created: {task_id_b}')
    if task_id_b:
        task_b = client.table('tasks').select(
            'id, title, organization_id, pending_org_id'
        ).eq('id', task_id_b).single().execute()
        has_org = bool(task_b.data.get('organization_id') or task_b.data.get('pending_org_id'))
        print(f'   Task B has org linkage: {has_org}')
        client.table('tasks').delete().eq('id', task_id_b).execute()
        print('   ✓ Cleaned up Task B')

    # ── 4. Verify Personal Org Exists ───────────────────────────────────
    print('\n[4] Verifying Personal Org:')
    personal = client.table('graph_nodes').select('id, label, type').eq(
        'owner_id', TEST_TENANT).eq('type', 'organization').ilike(
        'label', 'Personal').eq('is_current', True).execute()
    if personal.data:
        print(f'   ✓ Personal org exists: {personal.data[0]["id"]}')
    else:
        print('   ✗ Personal org MISSING!')

    # ── 5. Summary ──────────────────────────────────────────────────────
    print('\n' + '=' * 60)
    tasks_after = client.table('tasks').select('id').eq(
        'owner_id', TEST_TENANT).execute()
    orphan_after = [t for t in tasks_after.data
                    if not t.get('organization_id') and not t.get('pending_org_id')]
    print(f'UAT COMPLETE — Tasks remaining: {len(tasks_after.data)}, Orphans: {len(orphan_after)}')
    print('=' * 60)


if __name__ == '__main__':
    asyncio.run(main())
