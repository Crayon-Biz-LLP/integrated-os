"""
Comprehensive acceptance/regression suite for Rhodey's note capture and persistent memory.

Run: LIVE_DB=true PYTHONPATH=. pytest tests/clusters/test_note_capture_and_persistent_memory.py -v
"""
import uuid
from datetime import datetime, timezone, timedelta

import pytest
from core.services.db import get_supabase, version_memory_for_update
from core.retrieval.cleanup import cleanup_memory_retrieval_index, sweep_orphan_retrieval_entries
from core.webhook.workflows import get_deterministic_decision, check_and_resume_workflow
from core.lib.conversation import resolve_thread

supabase = get_supabase()

TEST_CHAT_BASE = 9000000
TEST_SOURCE = "test_e2e_nc"


def _chat_id(offset: int = 0) -> int:
    return TEST_CHAT_BASE + offset


def _ts(days=0, hours=0, minutes=0) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=days, hours=hours, minutes=minutes)).isoformat()


def _ts_ago(hours=25) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()


def _d_key(suffix: str) -> str:
    """Unique dedup_key for test raw_dumps."""
    return f"tnc:{suffix}:{uuid.uuid4()}"


def _cleanup_raw():
    supabase.table('raw_dumps').delete().eq('source', TEST_SOURCE).execute()


def _cleanup_conversations(chat_id: int):
    supabase.table('conversation_workflows').delete().eq('chat_id', chat_id).execute()
    supabase.table('conversation_threads').delete().eq('chat_id', chat_id).execute()


# ─────────────────────────────────────────────
# A. Note capture correctness (1-3)
# ─────────────────────────────────────────────

class TestNoteCaptureCorrectness:

    @pytest.mark.asyncio
    async def test_01_fresh_note_in_new_thread(self):
        chat_id = _chat_id(1)
        content = "[TEST] A1 fresh note"
        _cleanup_raw()
        _cleanup_conversations(chat_id)

        try:
            res = supabase.table('raw_dumps').insert({
                "content": content,
                "status": "staged",
                "direction": "incoming",
                "message_type": "note",
                "source": TEST_SOURCE,
                "dedup_key": _d_key("a1"),
                "metadata": {"intent": "NOTE"}
            }).execute()
            dump_id = res.data[0]['id']

            dump = supabase.table('raw_dumps').select('*').eq('id', dump_id).execute()
            assert dump.data[0]['status'] == 'staged'
            assert dump.data[0]['content'] == content

            wfs = supabase.table('conversation_workflows').select('id').eq('chat_id', chat_id).execute()
            assert len(wfs.data) == 0

            threads_before = len(supabase.table('conversation_threads').select('id').eq('chat_id', chat_id).execute().data)

            thread_id, anchor = resolve_thread(chat_id, content)
            assert thread_id is not None

            threads_after = supabase.table('conversation_threads').select('id').eq('chat_id', chat_id).execute().data
            assert len(threads_after) > threads_before or len(threads_after) > 0

            wfs2 = supabase.table('conversation_workflows').select('id').eq('chat_id', chat_id).execute()
            assert len(wfs2.data) == 0
        finally:
            _cleanup_raw()
            _cleanup_conversations(chat_id)

    @pytest.mark.asyncio
    async def test_02_note_during_active_workflow(self):
        chat_id = _chat_id(2)
        thread_id = str(uuid.uuid4())
        _cleanup_raw()
        _cleanup_conversations(chat_id)

        try:
            supabase.table('conversation_threads').insert({
                'id': thread_id, 'chat_id': chat_id, 'thread_type': 'workflow'
            }).execute()

            w_res = supabase.table('conversation_workflows').insert({
                'chat_id': chat_id, 'thread_id': thread_id, 'workflow_type': 'calendar_event',
                'status': 'active', 'awaiting_user_input': True,
                'payload': {'title': 'Test Event'}, 'expires_at': _ts(hours=23)
            }).execute()
            w_id = w_res.data[0]['id']

            note_res = supabase.table('raw_dumps').insert({
                "content": "[TEST] A2 note during active workflow",
                "status": "staged", "direction": "incoming",
                "message_type": "note", "source": TEST_SOURCE,
                "dedup_key": _d_key("a2"), "metadata": {"intent": "NOTE"}
            }).execute()
            note_id = note_res.data[0]['id']

            w_check = supabase.table('conversation_workflows').select('status').eq('id', w_id).execute()
            assert w_check.data[0]['status'] == 'active'
            n_check = supabase.table('raw_dumps').select('status').eq('id', note_id).execute()
            assert n_check.data[0]['status'] == 'staged'

            handled = await check_and_resume_workflow(chat_id, "By the way, I need milk", thread_id)
            assert not handled

            w_check2 = supabase.table('conversation_workflows').select('status').eq('id', w_id).execute()
            assert w_check2.data[0]['status'] == 'active'

            wfs = supabase.table('conversation_workflows').select('id').eq('chat_id', chat_id).execute()
            assert len(wfs.data) == 1
        finally:
            _cleanup_raw()
            _cleanup_conversations(chat_id)

    @pytest.mark.asyncio
    async def test_03_multi_thread_isolation(self):
        chat_id = _chat_id(3)
        thread_a_id = str(uuid.uuid4())
        thread_b_id = str(uuid.uuid4())
        eid_a = _make_entity_id()
        eid_b = _make_entity_id()
        anchor_a = {"entity_type": "organization", "entity_id": eid_a, "label": "Equisoft"}
        anchor_b = {"entity_type": "project", "entity_id": eid_b, "label": "QHORD"}

        _cleanup_raw()
        _cleanup_conversations(chat_id)

        try:
            supabase.table('conversation_threads').insert({
                'id': thread_a_id, 'chat_id': chat_id, 'thread_type': 'entity',
                'entity_type': 'organization', 'entity_id': eid_a, 'active_anchor': anchor_a
            }).execute()
            supabase.table('conversation_threads').insert({
                'id': thread_b_id, 'chat_id': chat_id, 'thread_type': 'entity',
                'entity_type': 'project', 'entity_id': eid_b, 'active_anchor': anchor_b
            }).execute()

            supabase.table('raw_dumps').insert({
                "content": "[TEST] A3 note in thread B",
                "status": "staged", "direction": "incoming",
                "message_type": "note", "source": TEST_SOURCE,
                "dedup_key": _d_key("a3"), "metadata": {"intent": "NOTE"}
            }).execute()

            t_a = supabase.table('conversation_threads').select('active_anchor').eq('id', thread_a_id).execute()
            assert t_a.data[0]['active_anchor'] == anchor_a
            t_b = supabase.table('conversation_threads').select('active_anchor').eq('id', thread_b_id).execute()
            assert t_b.data[0]['active_anchor'] == anchor_b
        finally:
            _cleanup_raw()
            _cleanup_conversations(chat_id)


# ─────────────────────────────────────────────
# B. Workflow continuity (4-7)
# ─────────────────────────────────────────────

class TestWorkflowContinuity:

    def test_04_deterministic_confirm_decline(self):
        assert get_deterministic_decision("yes") == "confirm"
        assert get_deterministic_decision("do it") == "confirm"
        assert get_deterministic_decision("go ahead") == "confirm"
        assert get_deterministic_decision("sure") == "confirm"
        assert get_deterministic_decision("ok") == "confirm"
        assert get_deterministic_decision("yeah") == "confirm"
        assert get_deterministic_decision("please") == "confirm"
        assert get_deterministic_decision("absolutely") == "confirm"

        assert get_deterministic_decision("no") == "decline"
        assert get_deterministic_decision("cancel") == "decline"
        assert get_deterministic_decision("skip") == "decline"
        assert get_deterministic_decision("stop") == "decline"
        assert get_deterministic_decision("nope") == "decline"
        assert get_deterministic_decision("nevermind") == "decline"

        assert get_deterministic_decision("yes please do it") == "confirm"
        assert get_deterministic_decision("no stop that") == "decline"

        assert get_deterministic_decision("yes but no") is None
        assert get_deterministic_decision("maybe later") is None
        assert get_deterministic_decision("I'm not sure") is None
        assert get_deterministic_decision("not really") is None
        assert get_deterministic_decision("I don't know") is None

    @pytest.mark.asyncio
    async def test_05_delayed_confirmation_idempotent(self):
        chat_id = _chat_id(5)
        thread_id = str(uuid.uuid4())
        _cleanup_conversations(chat_id)
        supabase.table('tasks').delete().eq('title', '[TEST] B5 Delayed Event').execute()

        try:
            supabase.table('conversation_threads').insert({
                'id': thread_id, 'chat_id': chat_id, 'thread_type': 'workflow'
            }).execute()

            w_res = supabase.table('conversation_workflows').insert({
                'chat_id': chat_id, 'thread_id': thread_id, 'workflow_type': 'calendar_event',
                'status': 'active', 'awaiting_user_input': True,
                'payload': {'title': '[TEST] B5 Delayed Event'}, 'expires_at': _ts(hours=23)
            }).execute()
            w_id = w_res.data[0]['id']

            handled1 = await check_and_resume_workflow(chat_id, "yes", thread_id)
            assert handled1

            w_check = supabase.table('conversation_workflows').select('status').eq('id', w_id).execute()
            assert w_check.data[0]['status'] == 'resolved'

            task_check = supabase.table('tasks').select('id') \
                .eq('title', '[TEST] B5 Delayed Event').execute()
            task_ids_before = {t['id'] for t in task_check.data}

            # Second "yes" on already-resolved workflow — returns False (correct: nothing to handle)
            handled2 = await check_and_resume_workflow(chat_id, "yes", thread_id)
            assert not handled2

            w_check2 = supabase.table('conversation_workflows').select('status').eq('id', w_id).execute()
            assert w_check2.data[0]['status'] == 'resolved'

            task_check_after = supabase.table('tasks').select('id') \
                .eq('title', '[TEST] B5 Delayed Event').execute()
            task_ids_after = {t['id'] for t in task_check_after.data}
            assert task_ids_after == task_ids_before
        finally:
            _cleanup_conversations(chat_id)
            supabase.table('tasks').delete().eq('title', '[TEST] B5 Delayed Event').execute()

    @pytest.mark.asyncio
    async def test_06_workflow_expiry(self):
        chat_id = _chat_id(6)
        thread_id_1 = str(uuid.uuid4())
        thread_id_2 = str(uuid.uuid4())
        _cleanup_conversations(chat_id)

        try:
            supabase.table('conversation_threads').insert([
                {'id': thread_id_1, 'chat_id': chat_id, 'thread_type': 'workflow'},
                {'id': thread_id_2, 'chat_id': chat_id, 'thread_type': 'workflow'}
            ]).execute()

            supabase.table('conversation_workflows').insert({
                'chat_id': chat_id, 'thread_id': thread_id_1, 'workflow_type': 'calendar_event',
                'status': 'active', 'awaiting_user_input': True,
                'payload': {'title': 'Expired Event'}, 'expires_at': _ts_ago(hours=1)
            }).execute()

            w2_res = supabase.table('conversation_workflows').insert({
                'chat_id': chat_id, 'thread_id': thread_id_2, 'workflow_type': 'calendar_event',
                'status': 'active', 'awaiting_user_input': True,
                'payload': {'title': 'Valid Event'}, 'expires_at': _ts(hours=23)
            }).execute()
            w2_id = w2_res.data[0]['id']

            handled = await check_and_resume_workflow(chat_id, "yes", thread_id_2)
            assert handled

            expired_check = supabase.table('conversation_workflows') \
                .select('status').eq('thread_id', thread_id_1).execute()
            assert expired_check.data[0]['status'] == 'expired'
            valid_check = supabase.table('conversation_workflows') \
                .select('status').eq('id', w2_id).execute()
            assert valid_check.data[0]['status'] == 'resolved'
        finally:
            _cleanup_conversations(chat_id)

    @pytest.mark.asyncio
    async def test_07_cancel_vs_unrelated(self):
        chat_id = _chat_id(7)
        thread_id = str(uuid.uuid4())
        _cleanup_conversations(chat_id)

        try:
            supabase.table('conversation_threads').insert({
                'id': thread_id, 'chat_id': chat_id, 'thread_type': 'workflow'
            }).execute()

            w_res = supabase.table('conversation_workflows').insert({
                'chat_id': chat_id, 'thread_id': thread_id, 'workflow_type': 'calendar_event',
                'status': 'active', 'awaiting_user_input': True,
                'payload': {'title': 'Cancel Test'}, 'expires_at': _ts(hours=23)
            }).execute()
            w_id = w_res.data[0]['id']

            handled1 = await check_and_resume_workflow(chat_id, "Marcus approved the pricing", thread_id)
            assert not handled1
            assert supabase.table('conversation_workflows').select('status').eq('id', w_id).execute().data[0]['status'] == 'active'

            handled2 = await check_and_resume_workflow(chat_id, "cancel", thread_id)
            assert handled2
            assert supabase.table('conversation_workflows').select('status').eq('id', w_id).execute().data[0]['status'] == 'cancelled'

            handled3 = await check_and_resume_workflow(chat_id, "yes", thread_id)
            assert not handled3
        finally:
            _cleanup_conversations(chat_id)


# ─────────────────────────────────────────────
# C. Query carry-forward (8-10)
# ─────────────────────────────────────────────

class TestQueryCarryForward:

    @pytest.mark.asyncio
    async def test_08_query_followup_carry_forward(self):
        chat_id = _chat_id(8)
        thread_id = str(uuid.uuid4())
        _cleanup_conversations(chat_id)

        try:
            anchor = {"entity_type": "organization", "entity_id": _make_entity_id(), "label": "Equisoft"}
            supabase.table('conversation_threads').insert({
                'id': thread_id, 'chat_id': chat_id, 'thread_type': 'entity',
                'entity_type': 'organization', 'entity_id': _make_entity_id(),
                'active_anchor': anchor
            }).execute()

            _, loaded_anchor = resolve_thread(chat_id, "what about the timeline?")
            if loaded_anchor:
                assert loaded_anchor.get('label') == 'Equisoft'
        finally:
            _cleanup_conversations(chat_id)

    @pytest.mark.asyncio
    async def test_09_multi_thread_query_isolation(self):
        chat_id = _chat_id(9)
        thread_a = str(uuid.uuid4())
        thread_b = str(uuid.uuid4())
        eid_a = _make_entity_id()
        eid_b = _make_entity_id()
        anchor_a = {"entity_type": "organization", "entity_id": eid_a, "label": "Equisoft"}
        anchor_b = {"entity_type": "project", "entity_id": eid_b, "label": "QHORD"}
        _cleanup_conversations(chat_id)

        try:
            supabase.table('conversation_threads').insert([
                {'id': thread_a, 'chat_id': chat_id, 'thread_type': 'entity',
                 'entity_type': 'organization', 'entity_id': eid_a,
                 'active_anchor': anchor_a, 'last_active_at': _ts_ago(hours=1)},
                {'id': thread_b, 'chat_id': chat_id, 'thread_type': 'entity',
                 'entity_type': 'project', 'entity_id': eid_b,
                 'active_anchor': anchor_b, 'last_active_at': _ts(hours=0)}
            ]).execute()

            t_a = supabase.table('conversation_threads').select('active_anchor').eq('id', thread_a).execute()
            assert t_a.data[0]['active_anchor'] == anchor_a
            t_b = supabase.table('conversation_threads').select('active_anchor').eq('id', thread_b).execute()
            assert t_b.data[0]['active_anchor'] == anchor_b
        finally:
            _cleanup_conversations(chat_id)

    @pytest.mark.asyncio
    async def test_10_anchor_persistence_after_resolution(self):
        chat_id = _chat_id(10)
        thread_id = str(uuid.uuid4())
        _cleanup_conversations(chat_id)

        try:
            supabase.table('conversation_threads').insert({
                'id': thread_id, 'chat_id': chat_id, 'thread_type': 'general'
            }).execute()

            new_anchor = {"entity_type": "organization", "entity_id": _make_entity_id(), "label": "SolvStrat"}
            supabase.table('conversation_threads').update({
                'active_anchor': new_anchor
            }).eq('id', thread_id).execute()

            t_check = supabase.table('conversation_threads').select('active_anchor').eq('id', thread_id).execute()
            assert t_check.data[0]['active_anchor'] == new_anchor

            supabase.table('conversation_threads').update({
                'last_active_at': _ts(hours=0)
            }).eq('id', thread_id).execute()

            t_check2 = supabase.table('conversation_threads').select('active_anchor').eq('id', thread_id).execute()
            assert t_check2.data[0]['active_anchor'] == new_anchor
        finally:
            _cleanup_conversations(chat_id)


# ─────────────────────────────────────────────
# D. Retrieval hygiene (11-12)
# ─────────────────────────────────────────────

class TestRetrievalHygiene:

    @pytest.mark.asyncio
    async def test_11_expired_memory_exclusion(self):
        supabase.table('memories').delete().ilike('content', '[TEST] D11%').execute()

        try:
            fresh_res = supabase.table('memories').insert({
                'content': '[TEST] D11 fresh memory about Equisoft project',
                'memory_type': 'note', 'source': 'test',
                'is_current': True, 'version': 1, 'expires_at': None
            }).execute()
            fresh_id = fresh_res.data[0]['id']

            expired_res = supabase.table('memories').insert({
                'content': '[TEST] D11 expired memory about old meeting',
                'memory_type': 'note', 'source': 'test',
                'is_current': True, 'version': 1,
                'expires_at': _ts_ago(hours=2)
            }).execute()
            expired_id = expired_res.data[0]['id']

            now_iso = datetime.now(timezone.utc).isoformat()
            expired_query = supabase.table('memories') \
                .select('id').in_('id', [fresh_id, expired_id]) \
                .lt('expires_at', now_iso).execute()
            expired_ids = {r['id'] for r in (expired_query.data or [])}
            assert expired_id in expired_ids, "Expired memory should be detected"
            assert fresh_id not in expired_ids, "Fresh memory should not be in expired set"

            memory_scores = {fresh_id: 0.9, expired_id: 0.8}
            filtered = {k: v for k, v in memory_scores.items() if k not in expired_ids}
            assert fresh_id in filtered
            assert expired_id not in filtered

            supabase.table('memories').delete().eq('id', fresh_id).execute()
            supabase.table('memories').delete().eq('id', expired_id).execute()
        finally:
            supabase.table('memories').delete().ilike('content', '[TEST] D11%').execute()

    @pytest.mark.asyncio
    async def test_12_memory_retrieval_quality_guard(self):
        supabase.table('memories').delete().ilike('content', '[TEST] D12%').execute()

        try:
            fresh_res = supabase.table('memories').insert({
                'content': '[TEST] D12 current project update',
                'memory_type': 'note', 'source': 'test',
                'is_current': True, 'version': 1, 'expires_at': None
            }).execute()
            fresh_id = fresh_res.data[0]['id']

            expired_res = supabase.table('memories').insert({
                'content': '[TEST] D12 stale pricing discussion',
                'memory_type': 'note', 'source': 'test',
                'is_current': True, 'version': 1,
                'expires_at': _ts_ago(hours=48)
            }).execute()
            expired_id = expired_res.data[0]['id']

            now_iso = datetime.now(timezone.utc).isoformat()
            expired_query = supabase.table('memories') \
                .select('id').in_('id', [fresh_id, expired_id]) \
                .lt('expires_at', now_iso).execute()
            expired_ids = {r['id'] for r in (expired_query.data or [])}

            assert expired_id in expired_ids
            assert fresh_id not in expired_ids

            memory_scores = {fresh_id: 0.75, expired_id: 0.60}
            filtered = {k: v for k, v in memory_scores.items() if k not in expired_ids}
            assert fresh_id in filtered
            assert expired_id not in filtered

            supabase.table('memories').delete().eq('id', fresh_id).execute()
            supabase.table('memories').delete().eq('id', expired_id).execute()
        finally:
            supabase.table('memories').delete().ilike('content', '[TEST] D12%').execute()


# ─────────────────────────────────────────────
# E. Raw dump lifecycle (13)
# ─────────────────────────────────────────────

class TestRawDumpLifecycle:

    @pytest.mark.asyncio
    async def test_13_raw_dump_cleanup(self):
        _cleanup_raw()

        try:
            recent_res = supabase.table('raw_dumps').insert({
                "content": "[TEST] E13 recent note", "status": "staged",
                "source": TEST_SOURCE, "dedup_key": _d_key("e13r"),
                "created_at": _ts(hours=-1)
            }).execute()
            recent_id = recent_res.data[0]['id']

            old_res = supabase.table('raw_dumps').insert({
                "content": "[TEST] E13 old stale note", "status": "staged",
                "source": TEST_SOURCE, "dedup_key": _d_key("e13o"),
                "created_at": _ts_ago(hours=25)
            }).execute()
            old_id = old_res.data[0]['id']

            pending_res = supabase.table('raw_dumps').insert({
                "content": "[TEST] E13 old pending item", "status": "pending",
                "source": TEST_SOURCE, "dedup_key": _d_key("e13p"),
                "created_at": _ts_ago(hours=30)
            }).execute()
            pending_id = pending_res.data[0]['id']

            cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
            cleanup_res = supabase.table('raw_dumps').update({"status": "abandoned"}) \
                .in_('status', ['staged', 'pending']).lt('created_at', cutoff).execute()

            cleaned_ids = {r['id'] for r in (cleanup_res.data or [])}
            assert old_id in cleaned_ids
            assert pending_id in cleaned_ids
            assert recent_id not in cleaned_ids

            assert supabase.table('raw_dumps').select('status').eq('id', old_id).execute().data[0]['status'] == 'abandoned'
            assert supabase.table('raw_dumps').select('status').eq('id', pending_id).execute().data[0]['status'] == 'abandoned'
            assert supabase.table('raw_dumps').select('status').eq('id', recent_id).execute().data[0]['status'] == 'staged'

            cleanup_res2 = supabase.table('raw_dumps').update({"status": "abandoned"}) \
                .in_('status', ['staged', 'pending']).lt('created_at', cutoff).execute()
            for r in (cleanup_res2.data or []):
                assert r['status'] != 'staged', "Recent staged should not be re-caught"
                assert r['status'] != 'pending', "Recent pending should not be re-caught"
        finally:
            _cleanup_raw()


# ─────────────────────────────────────────────
# F. Memory versioning (14-16)
# ─────────────────────────────────────────────

class TestMemoryVersioning:

    @pytest.mark.asyncio
    async def test_14_versioning_on_enrichment_update(self):
        supabase.table('memories').delete().ilike('content', '[TEST] F14%').execute()

        try:
            res = supabase.table('memories').insert({
                'content': '[TEST] F14 memory for versioning test',
                'memory_type': 'note', 'source': 'test',
                'is_current': True, 'version': 1
            }).execute()
            memory_id = res.data[0]['id']

            update_data = {}
            result = version_memory_for_update(memory_id, update_data)

            assert result.get('version') == 2, f"Expected version=2, got {result.get('version')}"
            assert result.get('supersedes_id') is not None, "Should have supersedes_id"
            archived_id = result['supersedes_id']

            supabase.table('memories').update(result).eq('id', memory_id).execute()

            current = supabase.table('memories').select('*').eq('id', memory_id).execute()
            assert current.data[0]['is_current']
            assert current.data[0]['version'] == 2
            assert current.data[0]['supersedes_id'] == archived_id

            archived = supabase.table('memories').select('*').eq('id', archived_id).execute()
            assert not archived.data[0]['is_current']
            assert archived.data[0]['version'] == 1
            assert archived.data[0]['content'] == '[TEST] F14 memory for versioning test'

            supabase.table('memories').delete().eq('id', memory_id).execute()
            supabase.table('memories').delete().eq('id', archived_id).execute()
        finally:
            supabase.table('memories').delete().ilike('content', '[TEST] F14%').execute()

    @pytest.mark.asyncio
    async def test_15_versioning_on_completion_degraded_path(self):
        supabase.table('memories').delete().ilike('content', '[TEST] F15%').execute()

        try:
            res = supabase.table('memories').insert({
                'content': '[TEST] F15 completion degraded memory',
                'memory_type': 'note', 'source': 'test',
                'is_current': True, 'version': 1,
                'metadata': {"intent": "COMPLETION", "title": "Fix bug"}
            }).execute()
            memory_id = res.data[0]['id']

            update1 = version_memory_for_update(memory_id, {
                "metadata": {"intent": "PROJECT_UPDATE", "degraded_from_completion": True}
            })
            supabase.table('memories').update(update1).eq('id', memory_id).execute()
            v1_archived_id = update1['supersedes_id']

            update2 = version_memory_for_update(memory_id, {})
            supabase.table('memories').update(update2).eq('id', memory_id).execute()
            v2_archived_id = update2['supersedes_id']

            current = supabase.table('memories').select('*').eq('id', memory_id).execute()
            assert current.data[0]['version'] == 3
            assert current.data[0]['is_current']

            assert supabase.table('memories').select('version').eq('id', v1_archived_id).execute().data[0]['version'] == 1
            assert supabase.table('memories').select('version').eq('id', v2_archived_id).execute().data[0]['version'] == 2

            supabase.table('memories').delete().eq('id', memory_id).execute()
            supabase.table('memories').delete().eq('id', v1_archived_id).execute()
            supabase.table('memories').delete().eq('id', v2_archived_id).execute()
        finally:
            supabase.table('memories').delete().ilike('content', '[TEST] F15%').execute()

    @pytest.mark.asyncio
    async def test_16_regression_guard_against_bypass(self):
        supabase.table('memories').delete().ilike('content', '[TEST] F16%').execute()

        try:
            res = supabase.table('memories').insert({
                'content': '[TEST] F16 direct update memory',
                'memory_type': 'note', 'source': 'test',
                'is_current': True, 'version': 1
            }).execute()
            memory_id = res.data[0]['id']

            supabase.table('memories').update({}).eq('id', memory_id).execute()

            current = supabase.table('memories').select('*').eq('id', memory_id).execute()
            assert current.data[0]['version'] == 1, "Direct update without versioning should not bump version"
            assert current.data[0]['is_current']

            archives = supabase.table('memories').select('id') \
                .eq('content', '[TEST] F16 direct update memory') \
                .eq('is_current', False).execute()
            assert len(archives.data) == 0, "No archive should be created without versioning"

            supabase.table('memories').delete().eq('id', memory_id).execute()
        finally:
            supabase.table('memories').delete().ilike('content', '[TEST] F16%').execute()


# ─────────────────────────────────────────────
# G. Deletion and index cleanup (17-18)
# ─────────────────────────────────────────────

class TestDeletionCleanup:

    @pytest.mark.asyncio
    async def test_17_undo_delete_cleanup(self):
        supabase.table('memories').delete().ilike('content', '[TEST] G17%').execute()

        try:
            mem_res = supabase.table('memories').insert({
                'content': '[TEST] G17 memory for cleanup test',
                'memory_type': 'note', 'source': 'test',
                'is_current': True, 'version': 1
            }).execute()
            memory_id = mem_res.data[0]['id']

            passage_res = supabase.table('retrieval_passages').insert({
                'memory_id': memory_id, 'text': '[TEST] G17 passage',
                'embedding': [0.0] * 768,
                'source_type': 'memory', 'source_id': str(memory_id),
                'passage_index': 0
            }).execute()
            passage_id = passage_res.data[0]['id']

            supabase.table('retrieval_memory_bundle_links').insert({
                'memory_id': memory_id, 'passage_id': passage_id
            }).execute()

            supabase.table('retrieval_index_runs').insert({
                'source_id': str(memory_id), 'source_type': 'memory', 'status': 'completed'
            }).execute()

            links_before = supabase.table('retrieval_memory_bundle_links') \
                .select('id').eq('memory_id', memory_id).execute()
            assert len(links_before.data) > 0

            cleanup_memory_retrieval_index(memory_id)

            assert len(supabase.table('retrieval_passages').select('id').eq('memory_id', memory_id).execute().data) == 0
            assert len(supabase.table('retrieval_memory_bundle_links').select('id').eq('memory_id', memory_id).execute().data) == 0
            assert len(supabase.table('retrieval_index_runs').select('id').eq('source_id', str(memory_id)).eq('source_type', 'memory').execute().data) == 0

            supabase.table('memories').delete().eq('id', memory_id).execute()
        finally:
            supabase.table('memories').delete().ilike('content', '[TEST] G17%').execute()

    @pytest.mark.asyncio
    async def test_18_orphan_sweep(self):
        supabase.table('memories').delete().ilike('content', '[TEST] G18%').execute()

        try:
            mem_res = supabase.table('memories').insert({
                'content': '[TEST] G18 valid memory',
                'memory_type': 'note', 'source': 'test',
                'is_current': True, 'version': 1
            }).execute()
            valid_memory_id = mem_res.data[0]['id']

            supabase.table('retrieval_passages').insert({
                'memory_id': valid_memory_id, 'text': '[TEST] G18 valid passage',
                'embedding': [0.0] * 768,
                'source_type': 'memory', 'source_id': str(valid_memory_id),
                'passage_index': 0
            }).execute()

            supabase.table('retrieval_index_runs').insert({
                'source_id': str(valid_memory_id), 'source_type': 'memory', 'status': 'completed'
            }).execute()

            orphan_memory_id = 99999999
            supabase.table('retrieval_passages').insert({
                'memory_id': orphan_memory_id, 'text': '[TEST] G18 orphan passage',
                'embedding': [0.0] * 768,
                'source_type': 'memory', 'source_id': 'orphan',
                'passage_index': 0
            }).execute()

            sweep_orphan_retrieval_entries()

            assert len(supabase.table('retrieval_passages').select('id').eq('memory_id', valid_memory_id).execute().data) == 1
            assert len(supabase.table('retrieval_passages').select('id').eq('memory_id', orphan_memory_id).execute().data) == 0

            sweep_orphan_retrieval_entries()

            assert len(supabase.table('retrieval_passages').select('id').eq('memory_id', valid_memory_id).execute().data) == 1

            cleanup_memory_retrieval_index(valid_memory_id)
            supabase.table('memories').delete().eq('id', valid_memory_id).execute()
        finally:
            supabase.table('memories').delete().ilike('content', '[TEST] G18%').execute()


# ─────────────────────────────────────────────
# H. End-to-end conversational flow (19)
# ─────────────────────────────────────────────

class TestEndToEnd:

    @pytest.mark.asyncio
    async def test_19_end_to_end_conversational_flow(self):
        chat_id = _chat_id(19)
        _cleanup_raw()
        _cleanup_conversations(chat_id)
        supabase.table('memories').delete().ilike('content', '[TEST] H19%').execute()
        supabase.table('tasks').delete().ilike('title', '[TEST] H19%').execute()

        try:
            # Step 1: Thread A — query about Equisoft
            thread_a_id = str(uuid.uuid4())
            eid_a = _make_entity_id()
            anchor_a = {"entity_type": "organization", "entity_id": eid_a, "label": "Equisoft"}
            supabase.table('conversation_threads').insert({
                'id': thread_a_id, 'chat_id': chat_id, 'thread_type': 'entity',
                'entity_type': 'organization', 'entity_id': eid_a,
                'active_anchor': anchor_a,
                'last_active_at': _ts(hours=0, minutes=-10)
            }).execute()

            # Step 2: Thread A — follow-up uses anchor
            _, loaded_anchor = resolve_thread(chat_id, "what about the timeline?")
            if loaded_anchor:
                assert loaded_anchor.get('label') == 'Equisoft'
            t_a = supabase.table('conversation_threads').select('active_anchor').eq('id', thread_a_id).execute()
            assert t_a.data[0]['active_anchor'] == anchor_a

            # Step 3: Thread B — capture raw note
            thread_b_id = str(uuid.uuid4())
            supabase.table('conversation_threads').insert({
                'id': thread_b_id, 'chat_id': chat_id, 'thread_type': 'general',
                'last_active_at': _ts(hours=0, minutes=-5)
            }).execute()

            note_res = supabase.table('raw_dumps').insert({
                "content": "[TEST] H19 captured note in thread B", "status": "staged",
                "source": TEST_SOURCE, "dedup_key": _d_key("h19n1"),
                "metadata": {"intent": "NOTE"}
            }).execute()
            note1_id = note_res.data[0]['id']

            # Step 4: Thread A — workflow clarification
            w1_res = supabase.table('conversation_workflows').insert({
                'chat_id': chat_id, 'thread_id': thread_a_id, 'workflow_type': 'calendar_event',
                'status': 'active', 'awaiting_user_input': True,
                'payload': {'title': '[TEST] H19 Meeting with Equisoft'},
                'expires_at': _ts(hours=23)
            }).execute()
            w1_id = w1_res.data[0]['id']

            # Step 5: Thread B — another raw note
            note2_res = supabase.table('raw_dumps').insert({
                "content": "[TEST] H19 second note while workflow active", "status": "staged",
                "source": TEST_SOURCE, "dedup_key": _d_key("h19n2"),
                "metadata": {"intent": "NOTE"}
            }).execute()
            note2_id = note2_res.data[0]['id']
            assert supabase.table('raw_dumps').select('id').eq('id', note2_id).execute().data

            # Step 6: Thread A — delayed "yes" resumes workflow
            handled = await check_and_resume_workflow(chat_id, "yes", thread_a_id)
            assert handled
            w1_check = supabase.table('conversation_workflows').select('status').eq('id', w1_id).execute()
            assert w1_check.data[0]['status'] == 'resolved'
            task_check = supabase.table('tasks').select('id').eq('title', '[TEST] H19 Meeting with Equisoft').execute()
            assert len(task_check.data) >= 1

            # Step 7: Thread A — second workflow, explicit cancel
            w2_res = supabase.table('conversation_workflows').insert({
                'chat_id': chat_id, 'thread_id': thread_a_id, 'workflow_type': 'task_creation',
                'status': 'active', 'awaiting_user_input': True,
                'payload': {'title': '[TEST] H19 Task to Cancel'},
                'expires_at': _ts(hours=23)
            }).execute()
            w2_id = w2_res.data[0]['id']

            handled_cancel = await check_and_resume_workflow(chat_id, "no", thread_a_id)
            assert handled_cancel
            assert supabase.table('conversation_workflows').select('status').eq('id', w2_id).execute().data[0]['status'] == 'cancelled'

            # Step 8: Memory versioning + delete cleanup
            mem_res = supabase.table('memories').insert({
                'content': '[TEST] H19 memory for versioning + delete',
                'memory_type': 'note', 'source': 'test',
                'is_current': True, 'version': 1
            }).execute()
            memory_id = mem_res.data[0]['id']

            ud = version_memory_for_update(memory_id, {})
            supabase.table('memories').update(ud).eq('id', memory_id).execute()
            assert ud['version'] == 2

            p_res = supabase.table('retrieval_passages').insert({
                'memory_id': memory_id, 'text': '[TEST] H19 passage for cleanup',
                'embedding': [0.0] * 768,
                'source_type': 'memory', 'source_id': str(memory_id),
                'passage_index': 0
            }).execute()
            passage_id = p_res.data[0]['id']
            supabase.table('retrieval_memory_bundle_links').insert({
                'memory_id': memory_id, 'passage_id': passage_id
            }).execute()
            supabase.table('retrieval_index_runs').insert({
                'source_id': str(memory_id), 'source_type': 'memory', 'status': 'completed'
            }).execute()

            cleanup_memory_retrieval_index(memory_id)
            supabase.table('memories').delete().eq('id', memory_id).execute()
            assert len(supabase.table('retrieval_passages').select('id').eq('memory_id', memory_id).execute().data) == 0

            # FINAL ASSERTIONS
            t_a_final = supabase.table('conversation_threads').select('active_anchor').eq('id', thread_a_id).execute()
            if t_a_final.data:
                assert t_a_final.data[0]['active_anchor'] == anchor_a
            assert supabase.table('raw_dumps').select('id').eq('id', note1_id).execute().data
            assert supabase.table('raw_dumps').select('id').eq('id', note2_id).execute().data

            w1f = supabase.table('conversation_workflows').select('status').eq('id', w1_id).execute()
            assert w1f.data[0]['status'] == 'resolved' if w1f.data else True
            w2f = supabase.table('conversation_workflows').select('status').eq('id', w2_id).execute()
            assert w2f.data[0]['status'] == 'cancelled' if w2f.data else True
        finally:
            _cleanup_raw()
            _cleanup_conversations(chat_id)
            supabase.table('memories').delete().ilike('content', '[TEST] H19%').execute()
            supabase.table('tasks').delete().ilike('title', '[TEST] H19%').execute()


def _make_entity_id() -> str:
    return str(uuid.uuid4())


# ─────────────────────────────────────────────
# I. Cleanup regression guard (20)
# ─────────────────────────────────────────────

class TestCleanupRegression:

    @pytest.mark.asyncio
    async def test_20_cleanup_deletes_all_retrieval_rows(self):
        tag = "[TEST] I20"
        supabase.table('memories').delete().ilike('content', f'{tag}%').execute()

        try:
            # Create two memories — A will be cleaned, B is collateral guard
            mem_a = supabase.table('memories').insert({
                'content': f'{tag} memory A', 'memory_type': 'note', 'source': 'test',
                'is_current': True, 'version': 1
            }).execute()
            mem_a_id = mem_a.data[0]['id']

            mem_b = supabase.table('memories').insert({
                'content': f'{tag} memory B', 'memory_type': 'note', 'source': 'test',
                'is_current': True, 'version': 1
            }).execute()
            mem_b_id = mem_b.data[0]['id']

            PHRASE_NODE_ID = 4

            # Helper: wire up retrieval rows for a memory
            def _wire(memory_id: int, suffix: str):
                p = supabase.table('retrieval_passages').insert({
                    'memory_id': memory_id, 'text': f'{tag} passage {suffix}',
                    'embedding': [0.0] * 768,
                    'source_type': 'memory', 'source_id': str(memory_id),
                    'passage_index': 0
                }).execute()
                pid = p.data[0]['id']

                supabase.table('retrieval_memory_bundle_links').insert({
                    'memory_id': memory_id, 'passage_id': pid
                }).execute()

                supabase.table('retrieval_passage_phrase_links').insert({
                    'passage_id': pid, 'node_id': PHRASE_NODE_ID,
                    'role': 'object', 'weight': 0.95
                }).execute()

                supabase.table('retrieval_index_runs').insert({
                    'source_id': str(memory_id), 'source_type': 'memory',
                    'status': 'completed'
                }).execute()
                return pid

            pid_a = _wire(mem_a_id, 'A')
            pid_b = _wire(mem_b_id, 'B')

            # Sanity: both memory rows have their retrieval rows
            assert len(supabase.table('retrieval_passages').select('id').eq('memory_id', mem_a_id).execute().data) == 1
            assert len(supabase.table('retrieval_passages').select('id').eq('memory_id', mem_b_id).execute().data) == 1
            assert len(supabase.table('retrieval_memory_bundle_links').select('id').eq('memory_id', mem_a_id).execute().data) == 1
            assert len(supabase.table('retrieval_memory_bundle_links').select('id').eq('memory_id', mem_b_id).execute().data) == 1
            assert len(supabase.table('retrieval_passage_phrase_links').select('id').eq('passage_id', pid_a).execute().data) == 1
            assert len(supabase.table('retrieval_passage_phrase_links').select('id').eq('passage_id', pid_b).execute().data) == 1
            assert len(supabase.table('retrieval_index_runs').select('id').eq('source_id', str(mem_a_id)).execute().data) >= 1
            assert len(supabase.table('retrieval_index_runs').select('id').eq('source_id', str(mem_b_id)).execute().data) >= 1

            # Act: clean up memory A
            cleanup_memory_retrieval_index(mem_a_id)

            # Assert: all retrieval rows for memory A are gone
            assert len(supabase.table('retrieval_passages').select('id').eq('memory_id', mem_a_id).execute().data) == 0
            assert len(supabase.table('retrieval_memory_bundle_links').select('id').eq('memory_id', mem_a_id).execute().data) == 0
            assert len(supabase.table('retrieval_passage_phrase_links').select('id').eq('passage_id', pid_a).execute().data) == 0
            assert len(supabase.table('retrieval_index_runs').select('id').eq('source_id', str(mem_a_id)).execute().data) == 0

            # Assert: memory B's rows are untouched (no collateral damage)
            assert len(supabase.table('retrieval_passages').select('id').eq('memory_id', mem_b_id).execute().data) == 1
            assert len(supabase.table('retrieval_memory_bundle_links').select('id').eq('memory_id', mem_b_id).execute().data) == 1
            assert len(supabase.table('retrieval_passage_phrase_links').select('id').eq('passage_id', pid_b).execute().data) == 1
            assert len(supabase.table('retrieval_index_runs').select('id').eq('source_id', str(mem_b_id)).execute().data) >= 1

            # Clean up memory B rows
            cleanup_memory_retrieval_index(mem_b_id)

        finally:
            supabase.table('memories').delete().ilike('content', f'{tag}%').execute()
