"""UAT Scenarios 2-5: Person detection, org-to-org edges, pending approval, enrichment queue."""
import asyncio
import os
from supabase import create_client
from core.services.db import tenant_scope

client = create_client(
    os.environ.get('SUPABASE_URL'),
    os.environ.get('SUPABASE_SERVICE_ROLE_KEY')
)

# Use Tenant-1's tenant (correct UUID, has full graph)
TENANT_1 = 'c302706e-fe61-422a-b384-68e3bc8f6f8e'


async def scenario_1_person_detection():
    """Test 1: Person detection/extraction."""
    print('\n' + '=' * 60)
    print('SCENARIO 1: Person Detection')
    print('=' * 60)

    with tenant_scope(TENANT_1):
        from core.lib.entity_context import extract_context_from_source

        # Test with text containing known persons
        # Tenant-1's graph has John Smith, Bob Williams, etc.
        tests = [
            'Call John about the AcmeCorp proposal',
            'Meeting with Bob about XYZ review',
            'Discuss with Henry Dsouza about the project',
        ]

        for msg in tests:
            ctx = await extract_context_from_source(msg)
            print(f'   "{msg}"')
            print(f'     Persons found: {ctx.person_names or "(none)"}')
            print(f'     Person IDs: {ctx.person_ids}')
            print(f'     Pending persons: {ctx.pending_person_ids}')

        # Check: are persons being detected at all?
        print('\n   Analysis:')
        print('   If persons are empty, deterministic detection is not finding them.')
        print('   This could be because:')
        print('   - detect_entities() uses n-gram matching which may not match "John" to "John Smith"')
        print('   - The LLM pass currently only extracts orgs, not persons')
        print('   - Person detection may need a separate LLM call or the existing extract_and_link_entities')


async def scenario_2_org_to_org_edges():
    """Test 2: Org-to-org edge creation."""
    print('\n' + '=' * 60)
    print('SCENARIO 2: Org-to-Org Edge Creation')
    print('=' * 60)

    with tenant_scope(TENANT_1):
        from core.lib.entity_context import extract_context_from_source

        # Multi-org text: BetaCorp (unknown) + AcmeCorp (known)
        msg = 'Minutes of meeting with John about BetaCorp trip finances for AcmeCorp'
        ctx = await extract_context_from_source(msg)

        print(f'   Input: "{msg}"')
        print(f'   Primary org: {ctx.organization_name or ctx.pending_org_label}')
        print(f'   Org-to-org edges: {ctx.org_to_org_edges}')

        # Check if edges were proposed
        if ctx.org_to_org_edges:
            print('   ✅ Org-to-org edges proposed')
            for edge in ctx.org_to_org_edges:
                print(f'     - {edge["source_label"]} → {edge["target_label"]} ({edge["relationship"]})')
        else:
            print('   ❌ No org-to-org edges proposed')
            print('   Expected: BetaCorp → AcmeCorp (CLIENT_OF)')
            print('   The LLM should detect both orgs, but _propose_org_to_org_edges may not be triggering')

        # Cleanup
        if ctx.pending_org_id:
            client.table('pending_nodes').delete().eq('id', ctx.pending_org_id).execute()
            print(f'   Cleaned up pending node {ctx.pending_org_id}')


async def scenario_3_pending_approval():
    """Test 3: Pending node approval flow."""
    print('\n' + '=' * 60)
    print('SCENARIO 3: Pending Node Approval Flow')
    print('=' * 60)

    with tenant_scope(TENANT_1):
        from core.lib.entity_context import extract_context_from_source

        # Create a pending org
        msg = 'Review AcmeCorporation invoice for 500 dollars'
        ctx = await extract_context_from_source(msg)

        print(f'   Created pending org: {ctx.pending_org_label} (id={ctx.pending_org_id})')

        if not ctx.pending_org_id:
            print('   ❌ No pending node created — cannot test approval')
            return

        # Simulate approval: update pending_nodes status to 'approved'
        print(f'\n   Simulating approval of pending node {ctx.pending_org_id}...')
        client.table('pending_nodes').update({'status': 'approved'}).eq('id', ctx.pending_org_id).execute()

        # Check if pending node is now approved
        pn = client.table('pending_nodes').select('id, label, status').eq('id', ctx.pending_org_id).single().execute()
        print(f'   Pending node status: {pn.data["status"]}')

        # Now check if the resolution function would find it
        from core.pulse.graph import _resolve_pending_org_on_approval
        print('\n   Calling _resolve_pending_org_on_approval...')

        try:
            _resolve_pending_org_on_approval(ctx.pending_org_id, TENANT_1)
            print('   ✅ Resolution completed')

            # Check if a graph_node was created
            gn = client.table('graph_nodes').select('id, label, type').ilike(
                'label', 'AcmeCorporation'
            ).eq('type', 'organization').eq('is_current', True).eq('owner_id', TENANT_1).execute()

            if gn.data:
                print(f'   ✅ Graph node created: {gn.data[0]["label"]} (id={gn.data[0]["id"]})')
            else:
                print('   ❌ No graph node created after approval')

            # Check if tasks with pending_org_id were updated
            tasks = client.table('tasks').select('id, title, pending_org_id, organization_id').eq(
                'owner_id', TENANT_1
            ).ilike('title', '%AcmeCorporation%').execute()

            for t in tasks.data:
                print(f'   Task "{t["title"]}": pending_org_id={t.get("pending_org_id")}, org_id={t.get("organization_id")}')

        except Exception as e:
            print(f'   ❌ Resolution failed: {e}')

        # Cleanup
        client.table('pending_nodes').delete().eq('id', ctx.pending_org_id).execute()
        print(f'   Cleaned up pending node {ctx.pending_org_id}')


async def scenario_4_enrichment_queue():
    """Test 4: EntityContext flowing through enrichment queue."""
    print('\n' + '=' * 60)
    print('SCENARIO 4: Enrichment Queue Integration')
    print('=' * 60)

    with tenant_scope(TENANT_1):
        from core.lib.entity_context import extract_context_from_source
        from core.pulse.tools import create_task_direct

        # Create a task with EntityContext
        ctx = await extract_context_from_source('Call John about AcmeCorp proposal')
        print(f'   Extracted context: org={ctx.organization_name}, method={ctx.extraction_method}')

        # Create task
        result = await create_task_direct(
            title='UAT Enrichment Test Task',
            entity_context=ctx,
            notes='Test message for enrichment queue'
        )
        task_id = result.get('task_id') if isinstance(result, dict) else result
        print(f'   Created task: {task_id}')

        if not task_id:
            print('   ❌ Task creation failed')
            return

        # Check if enrichment job was created
        jobs = client.table('pending_enrichment_jobs').select(
            'id, job_type, target_type, target_id, status'
        ).eq('target_id', str(task_id)).execute()

        print(f'   Enrichment jobs created: {len(jobs.data)}')
        for j in jobs.data:
            print(f'     - Job {j["id"]}: {j["job_type"]} ({j["status"]})')

        # Check if the job has entity_context in its content
        for j in jobs.data:
            job_detail = client.table('pending_enrichment_jobs').select('*').eq('id', j['id']).single().execute()
            content = job_detail.data.get('content', '')
            print(f'     Job content: {content[:200] if content else "(empty)"}')

        # Cleanup
        client.table('pending_enrichment_jobs').delete().eq('target_id', str(task_id)).execute()
        client.table('tasks').delete().eq('id', task_id).execute()
        print('   Cleaned up task and jobs')


async def main():
    print('=' * 60)
    print('UAT SCENARIOS 1-4: Untested Scenarios')
    print('Tenant: Tenant-1 (c302706e)')
    print('=' * 60)

    await scenario_1_person_detection()
    await scenario_2_org_to_org_edges()
    await scenario_3_pending_approval()
    await scenario_4_enrichment_queue()

    print('\n' + '=' * 60)
    print('ALL SCENARIOS COMPLETE')
    print('=' * 60)


if __name__ == '__main__':
    asyncio.run(main())
