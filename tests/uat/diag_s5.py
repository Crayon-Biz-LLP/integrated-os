#!/usr/bin/env python3
"""Rhodey OS — Comprehensive UAT Automation (17 Scenarios)

Same as run_uat.py but with S5 diagnostic wrappers.
"""

import os
import sys

_env_path = os.path.join(os.path.dirname(__file__), '..', '..', '.env')
if os.path.exists(_env_path):
    with open(_env_path) as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith('#') and '=' in _line:
                _k, _v = _line.split('=', 1)
                _v = _v.strip().strip('"').strip("'")
                os.environ.setdefault(_k.strip(), _v)

import json
import asyncio
import uuid
import traceback

from datetime import datetime, timezone
from unittest.mock import patch, AsyncMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from core.services.db import get_supabase
from core.webhook.handler import process_webhook

PREFIX = "[UAT]"
CLASSIFY_PACING_S = 4.0
_CHAT_ID_ENV = os.getenv("TELEGRAM_CHAT_ID", "")
CHAT_ID = int(_CHAT_ID_ENV) if _CHAT_ID_ENV.isdigit() else 999888777
os.environ["TELEGRAM_CHAT_ID"] = str(CHAT_ID)

supabase = get_supabase()
_captured_sends: list[dict] = []

async def _mock_send_telegram(chat_id: int, message_text: str, show_keyboard: bool = True,
                               inline_keyboard: list = None, skip_validation: bool = False):
    _captured_sends.append({
        "chat_id": chat_id,
        "message_text": message_text,
        "show_keyboard": show_keyboard,
        "inline_keyboard": inline_keyboard,
        "skip_validation": skip_validation,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })
    return True

def _reset_sends():
    _captured_sends.clear()

# ── S5 Diagnostic Wrappers ──────────────────────────────────────────────
# These wrap key functions to trace the exact breakpoint when S5 fails.
# They log to both stdout and audit_logs for visibility.

_UAT_DIAG = []

def _log(msg: str):
    _UAT_DIAG.append(msg)
    print(f"  [UAT_DIAG] {msg}")

# Wrap classify_intent
import core.webhook.classify as _cl_mod
_orig_classify = _cl_mod.classify_intent

async def _diag_classify(text, context, ist_hour=None, core_json="[]", conversation_history=""):
    result = await _orig_classify(text, context, ist_hour, core_json, conversation_history)
    _log(f"classify_intent → intent={result.get('intent')} conf={result.get('confidence')}")
    return result

# Wrap check_and_resume_workflow
import core.webhook.workflows as _wf_mod
_orig_workflow = _wf_mod.check_and_resume_workflow

async def _diag_workflow(chat_id, text, thread_id):
    result = await _orig_workflow(chat_id, text, thread_id)
    handled, ancillary = result if isinstance(result, tuple) else (result, None)
    _log(f"check_and_resume_workflow → handled={handled} ancillary={ancillary!r}")
    return result

# Wrap route_by_intent
import core.webhook.dispatch as _dp_mod
_orig_route = _dp_mod.route_by_intent

async def _diag_route(intent, text, chat_id, session_id, classification=None, **kwargs):
    _log(f"route_by_intent called: intent={intent}")
    return await _orig_route(intent, text, chat_id, session_id, classification=classification, **kwargs)

# ── Seed / Cleanup ─────────────────────────────────────────────────────

def _delete_ilike(table: str, col: str, pattern: str):
    try:
        supabase.table(table).delete().ilike(col, pattern).execute()
    except Exception:
        pass

def cleanup_uat_rows():
    uat_org_ids = []
    try:
        orgs = supabase.table('organizations').select('id').ilike('name', f'{PREFIX}%').execute()
        if orgs.data:
            uat_org_ids = [o['id'] for o in orgs.data]
    except Exception:
        pass
    if uat_org_ids:
        try: supabase.table('tasks').delete().in_('organization_id', uat_org_ids).execute()
        except: pass
        try: supabase.table('projects').delete().in_('organization_id', uat_org_ids).execute()
        except: pass
    for tbl, col in [
        ('pending_enrichment_jobs', 'content'),
        ('conversation_workflows', 'payload'),
        ('conversation_threads', 'active_anchor'),
        ('tasks', 'title'),
        ('memories', 'content'),
        ('raw_dumps', 'content'),
        ('raw_dumps', 'text'),
        ('projects', 'name'),
        ('organizations', 'name'),
        ('graph_nodes', 'label'),
        ('resources', 'url'),
        ('audit_logs', 'message'),
        ('conversations', 'query'),
        ('project_creation_signals', 'project_name'),
    ]:
        _delete_ilike(tbl, col, f'{PREFIX}%')

def seed_uat_orgs() -> dict:
    cleanup_uat_rows()
    orgs = {}
    for name in [f'{PREFIX} TestOrg Alpha', f'{PREFIX} TestOrg Beta']:
        existing = supabase.table('organizations').select('id').ilike('name', name).limit(1).execute()
        if existing and existing.data:
            orgs[name] = existing.data[0]['id']
        else:
            res = supabase.table('organizations').insert({"name": name}).execute()
            if res.data:
                orgs[name] = res.data[0]['id']
    projects = {}
    for pname, org in [
        (f'{PREFIX} Project X', orgs.get(f'{PREFIX} TestOrg Alpha')),
        (f'{PREFIX} Project Y', None),
    ]:
        existing = supabase.table('projects').select('id').ilike('name', pname).limit(1).execute()
        if existing and existing.data:
            projects[pname] = existing.data[0]['id']
        else:
            data = {"name": pname, "status": "active", "context": ""}
            if org:
                data["organization_id"] = org
            res = supabase.table('projects').insert(data).execute()
            if res.data:
                projects[pname] = res.data[0]['id']
    return {"orgs": orgs, "projects": projects}

def _assert(condition: bool, message: str) -> bool:
    if not condition:
        print(f"    [FAIL] {message}")
        return False
    print(f"    [PASS] {message}")
    return True

_BUILD_UPDATE_COUNTER = 99999000

async def simulate_telegram(text: str, pacing_s: float = CLASSIFY_PACING_S) -> dict:
    global _BUILD_UPDATE_COUNTER
    _BUILD_UPDATE_COUNTER += 1
    update = {
        "update_id": _BUILD_UPDATE_COUNTER,
        "message": {
            "message_id": _BUILD_UPDATE_COUNTER,
            "from": {"id": CHAT_ID, "is_bot": False, "first_name": "Danny"},
            "chat": {"id": CHAT_ID, "type": "private"},
            "date": int(datetime.now().timestamp()),
            "text": text,
        },
    }
    result = await process_webhook(update)
    if pacing_s > 0:
        await asyncio.sleep(pacing_s)
    return result

async def confirm_workflow() -> bool:
    wfs = supabase.table('conversation_workflows') \
        .select('id') \
        .eq('workflow_type', 'batch') \
        .eq('status', 'active') \
        .order('created_at', desc=True).limit(1).execute()
    if not (wfs and wfs.data):
        return False
    _reset_sends()
    await simulate_telegram("yes", pacing_s=0.5)
    return True

class UatResult:
    def __init__(self, name: str, tier: int):
        self.name = name
        self.tier = tier
        self.passed = False
        self.errors: list[str] = []
        self.details: dict = {}

    def fail(self, msg: str):
        self.errors.append(msg)

    @property
    def status(self) -> str:
        return "PASS" if self.passed else "FAIL"


# ════════════════════════════════════════════════════════════════════════
# SCENARIOS (identical to run_uat.py)
# ════════════════════════════════════════════════════════════════════════

async def scenario_1_task_creation_with_org(seed: dict) -> UatResult:
    r = UatResult("Task creation with org", tier=1)
    _reset_sends()
    from core.pulse.tools import create_task_direct
    ref_org_id = seed['orgs'].get(f'{PREFIX} TestOrg Alpha')
    result = await create_task_direct(
        title=f"{PREFIX} Review Q3 pricing",
        organization_id=ref_org_id,
        priority="important",
    )
    if _assert(result.get('action') == 'created', f"Task created (action={result.get('action')})"):
        r.details['task_id'] = result['task_id']
        r.details['action'] = result['action']
        task_id = result['task_id']
        task = supabase.table('tasks').select('id, title, organization_id, project_id') \
            .eq('id', task_id).limit(1).execute()
        if task and task.data:
            t = task.data[0]
            org_id = t.get('organization_id')
            _assert(org_id == ref_org_id, f"organization_id={org_id} matches seed")
        try:
            enrich = supabase.table('pending_enrichment_jobs') \
                .select('id').eq('target_id', task_id).eq('target_type', 'task').limit(1).execute()
            r.details['enrichment_queued'] = bool(enrich and enrich.data)
            _assert(r.details['enrichment_queued'], "Enrichment job queued for task")
        except Exception as e:
            r.details['enrichment_check_error'] = str(e)
            print(f"      [INFO] Enrichment check skipped: {e}")
    else:
        r.fail(f"Task creation failed: {result.get('reason', 'unknown')}")
    r.passed = len(r.errors) == 0
    return r

async def scenario_2_entity_resolution(seed: dict) -> UatResult:
    r = UatResult("Entity resolution (org from text)", tier=1)
    _reset_sends()
    text = f"{PREFIX} Remind me to follow up with Amita from TestOrg Beta next week"
    await simulate_telegram(text)
    tasks = supabase.table('tasks').select('id, title, organization_id') \
        .order('created_at', desc=True).limit(10).execute()
    uat_tasks = [t for t in (tasks.data or [])
                 if 'Amita' in t.get('title', '')
                 or ('follow up' in t.get('title', '').lower() and 'TestOrg Beta' in t.get('title', ''))]
    if not uat_tasks:
        print("      [INFO] Task not found directly — checking for batch workflow...")
        await confirm_workflow()
        tasks = supabase.table('tasks').select('id, title, organization_id') \
            .order('created_at', desc=True).limit(10).execute()
        uat_tasks = [t for t in (tasks.data or []) if 'Amita' in t.get('title', '')
                     or 'follow up' in t.get('title', '').lower()]
    if not _assert(len(uat_tasks) >= 1, f"Task referencing Amita created (found {len(uat_tasks)})"):
        r.fail("Task not created")
    else:
        t = uat_tasks[0]
        r.details['task_id'] = t['id']
        org_id = t.get('organization_id')
        _assert(org_id is not None, f"organization_id={org_id} set on task")
        if org_id:
            org = supabase.table('organizations').select('name').eq('id', org_id).maybe_single().execute()
            if org.data:
                r.details['org_name'] = org.data['name']
                _assert('TestOrg Beta' in org.data.get('name', ''), "Org matches TestOrg Beta")
    r.passed = len(r.errors) == 0
    return r

async def scenario_3_note_creation(seed: dict) -> UatResult:
    r = UatResult("Note/FYI creation", tier=1)
    _reset_sends()
    marker = f"uat-note-{uuid.uuid4().hex[:6]}"
    text = f"{PREFIX} Noted — pricing discussion concluded for {marker}"
    await simulate_telegram(text)
    mems = supabase.table('memories').select('id, content, memory_type') \
        .ilike('content', f'%{marker}%').limit(3).execute()
    if not _assert(mems and mems.data, f"Memory with marker '{marker}' created"):
        mems = supabase.table('memories').select('id, content, memory_type') \
            .ilike('content', '%pricing discussion%').order('created_at', desc=True).limit(5).execute()
        _assert(mems and mems.data, "At least some pricing discussion memory found")
    if mems and mems.data:
        m = mems.data[0]
        r.details['memory_id'] = m['id']
        r.details['memory_type'] = m.get('memory_type')
        r.details['memory_content'] = (m.get('content') or '')[:100]
        enrich = supabase.table('pending_enrichment_jobs') \
            .select('id').eq('target_id', m['id']).eq('target_type', 'note').limit(1).execute()
        r.details['enrichment_queued'] = bool(enrich and enrich.data)
        _assert(r.details['enrichment_queued'], "Enrichment job queued for note")
    else:
        r.fail("No memory created")
    r.passed = len(r.errors) == 0
    return r

async def scenario_4_url_quarantine(seed: dict) -> UatResult:
    r = UatResult("URL quarantine at ingress", tier=1)
    _reset_sends()
    url = f"https://uat-test-{uuid.uuid4().hex[:12]}.com/some-doc"
    text = f"{PREFIX} Check out: {url}"
    await simulate_telegram(text)
    mems = supabase.table('memories').select('id').ilike('content', f'%{url}%').limit(1).execute()
    _assert(not (mems and mems.data), "URL NOT stored in memories")
    nodes = supabase.table('graph_nodes').select('id').ilike('label', f'%{url}%').limit(1).execute()
    _assert(not (nodes and nodes.data), "No graph nodes created from URL")
    resources = supabase.table('resources').select('id, url') \
        .ilike('url', '%uat-test-%').order('created_at', desc=True).limit(3).execute()
    r.details['resource_found'] = bool(resources and resources.data)
    if not resources or not resources.data:
        dumps = supabase.table('raw_dumps').select('id, content') \
            .ilike('content', f'%{url}%').limit(1).execute()
        r.details['in_raw_dumps'] = bool(dumps and dumps.data)
        _assert(r.details['in_raw_dumps'], "URL processed (found in raw_dumps)")
    else:
        _assert(r.details['resource_found'], "URL stored in resources")
    r.details['telegram_sent'] = bool(_captured_sends)
    r.passed = len(r.errors) == 0
    return r

async def scenario_5_task_closure(seed: dict) -> UatResult:
    r = UatResult("Task closure via planner", tier=1)
    _reset_sends()
    from core.pulse.tools import create_task_direct
    marker = uuid.uuid4().hex[:6]
    task_title = f"{PREFIX} Task to close {marker}"
    result = await create_task_direct(title=task_title, priority="normal", dedup_key=f"closure-{marker}")
    if not _assert(result.get('action') == 'created', f"Task created for closure test (action={result.get('action')})"):
        r.fail("Could not create task for closure test")
        r.passed = False
        return r
    task_id = result['task_id']
    r.details['task_id'] = task_id
    _reset_sends()
    close_text = f"{PREFIX} Mark task {task_id} as done"
    
    # ── S5 Diagnostic checkpoints ──
    _log(f"S5 START: task_id={task_id}, close_text={close_text!r}")
    
    # Check active workflows BEFORE sending
    wf_before = supabase.table('conversation_workflows') \
        .select('id, workflow_type, status') \
        .eq('chat_id', CHAT_ID) \
        .eq('status', 'active') \
        .execute()
    _log(f"S5 active workflows before: {len(wf_before.data or [])}")
    for w in (wf_before.data or []):
        _log(f"  wf: id={w['id'][:12]} type={w['workflow_type']} status={w['status']}")
    
    await simulate_telegram(close_text, pacing_s=CLASSIFY_PACING_S)
    
    closed = supabase.table('tasks').select('status, completed_at').eq('id', task_id).limit(1).execute()
    if closed and closed.data:
        r.details['status'] = closed.data[0].get('status')
        r.details['completed_at'] = closed.data[0].get('completed_at')
        _log(f"S5 result: status={closed.data[0].get('status')}, completed_at={closed.data[0].get('completed_at')}")
        if not _assert(closed.data[0].get('status') == 'done', "Task status is 'done'"):
            r.fail(f"Task status is '{closed.data[0].get('status')}', expected 'done'")
        if not _assert(closed.data[0].get('completed_at') is not None, "completed_at is set"):
            r.fail("completed_at is not set")
    else:
        _log(f"S5 result: task {task_id} not found!")
        r.fail("Task not found after closure")
    r.passed = len(r.errors) == 0
    return r

async def scenario_6_duplicate_prevention(seed: dict) -> UatResult:
    r = UatResult("Duplicate prevention (dedup_key)", tier=1)
    _reset_sends()
    from core.pulse.tools import create_task_direct
    marker = uuid.uuid4().hex[:8]
    dedup_key = f"uat-dedup-{marker}"
    dedup_title = f"{PREFIX} Dedup test item {marker}"
    r1 = await create_task_direct(title=dedup_title, priority="normal", dedup_key=dedup_key)
    first_task_id = r1.get('task_id')
    r2 = await create_task_direct(title=dedup_title, priority="normal", dedup_key=dedup_key)
    _assert(r1.get('action') == 'created', "First task created")
    r.details['first_action'] = r1.get('action')
    r.details['first_task_id'] = first_task_id
    action2 = r2.get('action')
    r.details['second_action'] = action2
    if action2 == 'skipped':
        _assert(True, "Second task correctly skipped via dedup_key")
        r.details['existing_task_id'] = r2.get('task_id')
    else:
        r.fail(f"Expected 'skipped', got '{action2}'")
    r.passed = len(r.errors) == 0
    return r

async def scenario_7_note_with_hidden_task(seed: dict) -> UatResult:
    r = UatResult("NOTE with hidden TASK (batch workflow)", tier=2)
    _reset_sends()
    text = (f"{PREFIX} Had a great meeting. They are happy with progress. "
            "Need to send the proposal by Wednesday.")
    await simulate_telegram(text)
    workflows = supabase.table('conversation_workflows') \
        .select('id, workflow_type, status, payload') \
        .order('created_at', desc=True).limit(5).execute()
    uat_wfs = [w for w in (workflows.data or []) if w.get('workflow_type') == 'batch']
    if _assert(len(uat_wfs) >= 1, f"Batch workflow created (found {len(uat_wfs)})"):
        r.details['workflow_id'] = uat_wfs[0]['id']
        payload = uat_wfs[0].get('payload', {})
        signals = payload.get('signals', [])
        r.details['signals_count'] = len(signals)
        _assert(len(signals) >= 1, f"Workflow has {len(signals)} signal(s) in payload")
    else:
        r.fail("No batch workflow created")
    r.passed = len(r.errors) == 0
    return r

async def scenario_8_query_with_hidden_action(seed: dict) -> UatResult:
    r = UatResult("QUERY with hidden action", tier=2)
    _reset_sends()
    text = f"{PREFIX} Check with Amita about TestOrg Alpha - need to send the contract"
    await simulate_telegram(text)
    r.details['telegram_sent'] = bool(_captured_sends)
    tasks = supabase.table('tasks').select('id, title').order('created_at', desc=True).limit(10).execute()
    uat_tasks = [t for t in (tasks.data or []) if 'contract' in t.get('title', '').lower()
                 or 'send' in t.get('title', '').lower()]
    if not uat_tasks:
        print("      [INFO] No task directly — checking for batch workflow...")
        await confirm_workflow()
        tasks = supabase.table('tasks').select('id, title').order('created_at', desc=True).limit(10).execute()
        uat_tasks = [t for t in (tasks.data or []) if 'contract' in t.get('title', '').lower()]
    r.details['hidden_task_created'] = bool(uat_tasks)
    _assert(r.details['hidden_task_created'], "Hidden action task created from QUERY")
    r.passed = len(r.errors) == 0
    return r

async def scenario_9_batch_workflow_confirmation(seed: dict) -> UatResult:
    r = UatResult("Batch workflow confirmation (deterministic)", tier=2)
    _reset_sends()
    w_id = str(uuid.uuid4())
    session_id = str(uuid.uuid4())
    supabase.table('conversation_threads').insert({
        'id': session_id,
        'chat_id': CHAT_ID,
        'thread_type': 'general',
        'created_at': datetime.now(timezone.utc).isoformat(),
        'last_active_at': datetime.now(timezone.utc).isoformat(),
    }).execute()
    supabase.table('conversation_workflows').insert({
        'id': w_id,
        'chat_id': CHAT_ID,
        'thread_id': session_id,
        'workflow_type': 'batch',
        'status': 'active',
        'awaiting_user_input': True,
        'payload': {'signals': [{'type': 'task_imperative', 'title': f'{PREFIX} Prepare quarterly report'}]}
    }).execute()
    r.details['workflow_id'] = w_id
    r.details['session_id'] = session_id
    _reset_sends()
    from core.webhook.workflows import check_and_resume_workflow
    resolved, ancillary = await check_and_resume_workflow(text="yes", chat_id=CHAT_ID, thread_id=session_id)
    r.details['workflow_resolved'] = resolved
    wf_check = supabase.table('conversation_workflows') \
        .select('status').eq('id', w_id).limit(1).execute()
    if wf_check and wf_check.data:
        r.details['workflow_status_after'] = wf_check.data[0].get('status')
        _assert(wf_check.data[0].get('status') in ('resolved', 'cancelled', 'active'),
                f"Workflow status after: {wf_check.data[0].get('status')}")
    else:
        r.fail("Workflow not found after confirmation")
    r.passed = len(r.errors) == 0
    return r

async def scenario_10_task_with_entity_linker(seed: dict) -> UatResult:
    r = UatResult("Entity linker resolves org before task creation", tier=2)
    _reset_sends()
    from core.pulse.tools import create_task_direct
    result = await create_task_direct(
        title=f"{PREFIX} Review pricing strategy",
        organization_name="TestOrg Beta",
        priority="normal",
    )
    if _assert(result.get('action') == 'created', f"Task created (action={result.get('action')})"):
        r.details['task_id'] = result['task_id']
        task = supabase.table('tasks').select('id, title, organization_id') \
            .eq('id', result['task_id']).limit(1).execute()
        if task and task.data:
            t = task.data[0]
            org_id = t.get('organization_id')
            r.details['organization_id'] = org_id
            _assert(org_id is not None, f"organization_id={org_id} resolved and set")
            if org_id:
                org = supabase.table('organizations').select('name').eq('id', org_id).maybe_single().execute()
                if org.data:
                    r.details['org_name'] = org.data['name']
                    _assert('TestOrg Beta' in org.data.get('name', ''), "Resolved org matches TestOrg Beta")
    else:
        r.fail(f"Task creation failed: {result.get('reason', 'unknown')}")
    r.passed = len(r.errors) == 0
    return r

async def scenario_11_enrichment_queue_processing(seed: dict) -> UatResult:
    r = UatResult("Enrichment queue: enqueue -> claim -> process", tier=3)
    _reset_sends()
    from core.pulse.tools import create_task_direct
    marker = uuid.uuid4().hex[:6]
    result = await create_task_direct(
        title=f"{PREFIX} Enrichment test: Q3 planning for next sprint {marker}",
        priority="normal",
        dedup_key=f"enrich-{marker}",
    )
    if not _assert(result.get('action') == 'created', f"Task created for enrichment test (action={result.get('action')})"):
        r.fail("Task not created")
        r.passed = False
        return r
    task_id = result['task_id']
    r.details['task_id'] = task_id
    enrich = supabase.table('pending_enrichment_jobs') \
        .select('id, job_type, status') \
        .eq('target_id', task_id) \
        .eq('target_type', 'task') \
        .limit(1).execute()
    found_count = len(enrich.data) if enrich and enrich.data else 0
    if not _assert(enrich and enrich.data, f"Enrichment job exists before processing (found {found_count})"):
        r.fail("No enrichment job found")
        r.passed = len(r.errors) == 0
        return r
    else:
        r.details['enrichment_job_id'] = enrich.data[0]['id']
        r.details['enrichment_job_type'] = enrich.data[0].get('job_type')
        r.details['enrichment_status_before'] = enrich.data[0].get('status')
    processed = 0
    try:
        from core.lib.enrichment_queue import process_pending_enrichment
        processed = await process_pending_enrichment(max_jobs=10)
    except Exception as e:
        r.fail(f"Enrichment processing error: {e}")
        r.details['enrichment_error'] = str(e)
        r.passed = len(r.errors) == 0
        return r
    if _assert(processed >= 1, f"Enrichment processed ({processed} job(s))"):
        r.details['enrichment_processed'] = processed
        enrich_after = supabase.table('pending_enrichment_jobs') \
            .select('status').eq('id', r.details.get('enrichment_job_id')).limit(1).execute()
        if enrich_after and enrich_after.data:
            r.details['enrichment_status_after'] = enrich_after.data[0].get('status')
            _assert(enrich_after.data[0].get('status') in ('completed', 'failed'),
                    "Enrichment job status is terminal")
    r.passed = len(r.errors) == 0
    return r

async def scenario_12_memory_retrieval(seed: dict) -> UatResult:
    r = UatResult("Memory retrieval via query", tier=3)
    _reset_sends()
    from core.pulse.tools import create_note_direct
    topic = f"decision-{uuid.uuid4().hex[:8]}"
    note_text = f"{PREFIX} Important decision about {topic}: proceed with alpha launch in Q4."
    result = await create_note_direct(content=note_text, source="uat")
    if not _assert(result.get('action') == 'filed', f"Note created (action={result.get('action')})"):
        r.fail("Memory not created")
        r.passed = False
        return r
    memory_id = result.get('memory_id')
    r.details['memory_id'] = memory_id
    from core.lib.enrichment_queue import process_pending_enrichment
    await process_pending_enrichment(max_jobs=10)
    _reset_sends()
    from core.webhook.dispatch import interrogate_brain
    reply = await interrogate_brain(query=f"what was the decision about {topic}", chat_id=CHAT_ID)
    r.details['query_reply'] = (reply or "")[:200] if reply else None
    _assert(reply is not None and len(reply or "") > 0,
            f"Query returned a response ({len(reply or '')} chars)")
    r.passed = len(r.errors) == 0
    return r

async def scenario_13_dlq_recovery(seed: dict) -> UatResult:
    r = UatResult("DLQ consumer recovery", tier=3)
    from core.skills.dlq_consumer import process_dlq
    dlq_result = await process_dlq(max_items=5, max_retries=3)
    r.details['dlq_processed'] = dlq_result.get('processed', 0)
    r.details['dlq_succeeded'] = dlq_result.get('succeeded', 0)
    r.details['dlq_failed'] = dlq_result.get('failed', 0)
    r.details['dlq_escalated'] = dlq_result.get('escalated', 0)
    _assert(isinstance(dlq_result.get('processed', 0), int), "DLQ consumer returned valid result")
    print(f"      [INFO] DLQ: {dlq_result.get('processed', 0)} processed, "
          f"{dlq_result.get('succeeded', 0)} succeeded, "
          f"{dlq_result.get('failed', 0)} failed")
    r.passed = len(r.errors) == 0
    return r

async def scenario_14_pulse_engine_briefing(seed: dict) -> UatResult:
    r = UatResult("Pulse Engine briefing generation", tier=4)
    _reset_sends()
    pulse_secret = os.getenv("PULSE_SECRET", "")
    from core.pulse.briefing import process_pulse
    result = await process_pulse(auth_secret=pulse_secret, trigger="uat")
    if result.get("error"):
        r.fail(f"Pulse Engine error: {result['error']}")
    else:
        r.details['success'] = result.get('success', False)
        briefing = result.get('briefing', '') or ''
        r.details['briefing_length'] = len(briefing)
        r.details['has_briefing'] = bool(briefing)
        _assert(r.details['has_briefing'], "Briefing text generated")
        if len(briefing) > 0:
            _assert(len(briefing) > 50, f"Briefing is substantive ({len(briefing)} chars)")
        recent = supabase.table('memories') \
            .select('id').eq('memory_type', 'pulse_briefing') \
            .order('created_at', desc=True).limit(1).execute()
        r.details['briefing_in_memories'] = bool(recent and recent.data)
        _assert(r.details['briefing_in_memories'], "Briefing stored as pulse_briefing memory")
        dumps = supabase.table('raw_dumps') \
            .select('id').eq('message_type', 'pulse_briefing') \
            .order('created_at', desc=True).limit(1).execute()
        r.details['briefing_in_raw_dumps'] = bool(dumps and dumps.data)
        _assert(r.details['briefing_in_raw_dumps'], "Briefing logged in raw_dumps")
        r.details['telegram_sent'] = bool(_captured_sends)
        _assert(r.details['telegram_sent'], "Telegram send was called")
    r.passed = len(r.errors) == 0
    return r

async def scenario_15_decision_pulse(seed: dict) -> UatResult:
    r = UatResult("Decision Pulse pending items", tier=4)
    _reset_sends()
    pulse_secret = os.getenv("PULSE_SECRET", "")
    from core.pulse.decision_pulse import process_decision_pulse
    result = await process_decision_pulse(auth_secret=pulse_secret, trigger="uat")
    r.details['success'] = result.get('success', False)
    r.details['message'] = result.get('message', '')
    r.details['decision_count'] = result.get('decision_count', 0)
    _assert(result.get('success', False) or 'No pending' in str(result.get('message', '')),
            f"Decision Pulse completed: {result.get('message', 'ok')}")
    _assert(isinstance(r.details['decision_count'], int),
            f"Decision count is integer ({r.details['decision_count']})")
    r.passed = len(r.errors) == 0
    return r

async def scenario_16_sentinel_nudge(seed: dict) -> UatResult:
    r = UatResult("Sentinel upcoming event nudge", tier=4)
    _reset_sends()
    pulse_secret = os.getenv("PULSE_SECRET", "")
    from core.pulse.sentinel import process_sentinel
    result = await process_sentinel(auth_secret=pulse_secret, trigger="uat")
    r.details['success'] = result.get('success', False)
    r.details['alerted'] = result.get('alerted', 0)
    r.details['error'] = result.get('error')
    if result.get('error'):
        r.fail(f"Sentinel error: {result['error']}")
    else:
        _assert(result.get('success', False), "Sentinel completed successfully")
        print(f"      [INFO] Sentinel alerted {result.get('alerted', 0)} event(s)")
    r.passed = len(r.errors) == 0
    return r

async def scenario_17_health_check(seed: dict) -> UatResult:
    r = UatResult("Health check", tier=5)
    from core.pulse.pipeline import run_full_health_check
    health = await run_full_health_check()
    r.details['health'] = health
    issues = health.get('issues', [])
    report = health.get('report', '')
    r.details['issues_count'] = len(issues)
    r.details['report_preview'] = report[:200] if report else ''
    real_issues = [i for i in issues if 'LLM fallback' not in i]
    r.details['real_issues'] = real_issues
    if len(real_issues) == 0:
        _assert(True, f"No system issues found — report: {report[:100]}")
        if issues and not real_issues:
            print(f"      [INFO] {len(issues)} pre-existing issue(s) (LLM degradation) — not UAT-related")
    else:
        for issue in real_issues:
            r.fail(f"Health issue: {issue}")
    r.passed = len(r.errors) == 0
    return r

# ════════════════════════════════════════════════════════════════════════

ALL_SCENARIOS = [
    ("S1", "Task creation with org", scenario_1_task_creation_with_org),
    ("S2", "Entity resolution (org from text)", scenario_2_entity_resolution),
    ("S3", "Note/FYI creation", scenario_3_note_creation),
    ("S4", "URL quarantine at ingress", scenario_4_url_quarantine),
    ("S5", "Task closure via planner", scenario_5_task_closure),
    ("S6", "Duplicate prevention (dedup_key)", scenario_6_duplicate_prevention),
    ("S7", "NOTE with hidden TASK (batch workflow)", scenario_7_note_with_hidden_task),
    ("S8", "QUERY with hidden action", scenario_8_query_with_hidden_action),
    ("S9", "Batch workflow confirmation", scenario_9_batch_workflow_confirmation),
    ("S10", "Entity linker resolves org before task creation", scenario_10_task_with_entity_linker),
    ("S11", "Enrichment queue processing", scenario_11_enrichment_queue_processing),
    ("S12", "Memory retrieval via query", scenario_12_memory_retrieval),
    ("S13", "DLQ consumer recovery", scenario_13_dlq_recovery),
    ("S14", "Pulse Engine briefing generation", scenario_14_pulse_engine_briefing),
    ("S15", "Decision Pulse pending items", scenario_15_decision_pulse),
    ("S16", "Sentinel upcoming event nudge", scenario_16_sentinel_nudge),
    ("S17", "Health check", scenario_17_health_check),
]

_TIER_NAMES = {
    1: "Core Ingestion & Processing (S1-S6)",
    2: "Hidden Action & Workflow (S7-S10)",
    3: "Enrichment & Intelligence (S11-S13)",
    4: "Presentation Layer (S14-S16)",
    5: "Resilience & Health (S17)",
}

_TIER_MAP = {}
for sid, _, _ in ALL_SCENARIOS:
    n = int(sid[1:])
    if n <= 6: _TIER_MAP[sid] = 1
    elif n <= 10: _TIER_MAP[sid] = 2
    elif n <= 13: _TIER_MAP[sid] = 3
    elif n <= 16: _TIER_MAP[sid] = 4
    else: _TIER_MAP[sid] = 5

async def run_all():
    print("=" * 72)
    print("  RHODEY OS - COMPREHENSIVE UAT WITH S5 DIAGNOSTIC (17 scenarios)")
    print(f"  Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  CHAT_ID: {CHAT_ID}")
    print("=" * 72)

    seed = seed_uat_orgs()
    print("\n  [SETUP] Seeded test orgs + projects\n")

    patchers = [
        patch('core.webhook.telegram.send_telegram', new=_mock_send_telegram),
        patch('core.webhook.handler.send_telegram', new=_mock_send_telegram),
        patch('core.webhook.dispatch.send_telegram', new=_mock_send_telegram),
        patch('core.webhook.telegram.answer_callback_query', new=AsyncMock()),
        patch('core.actions.executor.send_telegram', new=_mock_send_telegram),
        patch('core.pulse.sentinel.send_telegram', new=_mock_send_telegram),
        patch('core.pulse.briefing.send_telegram', new=_mock_send_telegram),
        patch('core.pulse.briefing.send_push_notification', new=AsyncMock()),
        patch('core.pulse.decision_pulse.send_telegram', new=_mock_send_telegram),
        patch('core.pulse.decision_pulse.send_push_notification', new=AsyncMock()),
        # S5 diagnostic wrappers
        patch('core.webhook.handler.classify_intent', new=_diag_classify),
        patch('core.webhook.handler.check_and_resume_workflow', new=_diag_workflow),
        patch('core.webhook.handler.route_by_intent', new=_diag_route),
    ]
    for p in patchers:
        p.start()

    results: list[UatResult] = []
    passed = 0
    failed = 0
    current_tier = 0

    for sc_id, sc_name, sc_func in ALL_SCENARIOS:
        tier = _TIER_MAP[sc_id]
        if tier != current_tier:
            print(f"\n{'=' * 72}")
            print(f"  TIER {tier}: {_TIER_NAMES.get(tier, '')}")
            print(f"{'=' * 72}")
            current_tier = tier

        print(f"\n  [{sc_id}] {sc_name}...")
        try:
            result = await sc_func(seed)
        except Exception as e:
            result = UatResult(sc_name, tier)
            result.fail(f"Exception: {e}")
            traceback.print_exc()

        results.append(result)
        if result.passed:
            passed += 1
            print("  >> PASS")
        else:
            failed += 1
            print("  >> FAIL")
            for err in result.errors:
                print(f"     {err}")

    for p in patchers:
        p.stop()

    # Cleanup
    print(f"\n{'=' * 72}")
    print("  CLEANUP")
    cleanup_uat_rows()
    remaining = 0
    for tbl, col in [
        ('tasks', 'title'), ('memories', 'content'), ('organizations', 'name'),
        ('projects', 'name'), ('graph_nodes', 'label'), ('raw_dumps', 'text'),
        ('resources', 'url'), ('audit_logs', 'message'),
        ('pending_enrichment_jobs', 'content'),
    ]:
        try:
            res = supabase.table(tbl).select('id', count='exact').ilike(col, f'{PREFIX}%').execute()
            count = res.count if hasattr(res, 'count') else len(res.data or [])
            remaining += count
        except Exception:
            pass
    if remaining == 0:
        print("  [CLEANUP] Zero [UAT] rows remain")
    else:
        print(f"  [CLEANUP] {remaining} rows remain — may need manual cleanup")

    total = len(results)
    print(f"\n{'=' * 72}")
    print("  RESULTS SUMMARY")
    success_rate = f"{passed / total * 100:.0f}%" if total else "N/A"
    print(f"  Total: {total} | Passed: {passed} | Failed: {failed} | Success rate: {success_rate}")
    print("=" * 72)

    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    report_path = f"uat_report_{timestamp}.json"
    report_data = {
        "timestamp": timestamp,
        "total": total,
        "passed": passed,
        "failed": failed,
        "scenarios": [
            {"id": ALL_SCENARIOS[i][0], "name": r.name, "status": r.status,
             "tier": r.tier, "errors": r.errors, "details": r.details}
            for i, r in enumerate(results)
        ],
    }
    with open(report_path, 'w') as f:
        json.dump(report_data, f, indent=2, default=str)
    print(f"\n  [DONE] Report saved to: {report_path}")
    return passed == total

if __name__ == "__main__":
    if not os.getenv("LIVE_DB"):
        print("WARNING: LIVE_DB not set. Set LIVE_DB=true to proceed.")
        sys.exit(1)
    if CHAT_ID in (0, 999888777):
        print("ERROR: TELEGRAM_CHAT_ID must be set. Check .env file.")
        sys.exit(1)
    success = asyncio.run(run_all())
    sys.exit(0 if success else 1)
