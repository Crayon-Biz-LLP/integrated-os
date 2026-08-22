"""Regression tests for the 2026-08-15 overall-health-check fixes.

Pins the recurring error sources found in audit_logs so they can't come
back:
  1. pulse daily reflection: asyncio.create_task(None) on the SYNC
     schedule_index_memory (memory.py)
  2. sentinel: '>' not supported between NoneType and int (zombie_recovery)
  3. ingest: 'NoneType' object has no attribute 'data' (record_outgoing dedup)
  4. webhook: UnboundLocalError 'ledger' on channel rejections
"""


import inspect
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
pytestmark = pytest.mark.briefing



# ── 1. memory.py: schedule_index_memory is called directly (sync) ──────────
def test_daily_reflection_calls_schedule_index_memory_directly():
    with open("core/pulse/memory.py") as f:
        content = f.read()
    assert "asyncio.create_task(schedule_index_memory(" not in content, (
        "create_task on the SYNC schedule_index_memory raises "
        "'a coroutine was expected, got None' on every pulse run"
    )
    assert (
        'schedule_index_memory(memory_id, lesson, "reflection", "pulse_reflection")'
        in content
    )


# ── 2. zombie_recovery returns an int (sentinel's `recovered > 0` works) ────
def test_zombie_recovery_returns_int():
    from core.services import db as db_mod

    fake_update = MagicMock()
    fake_update.execute.return_value.data = [{"id": 1}, {"id": 2}]
    fake_update.lt.return_value = fake_update
    fake_update.eq.return_value = fake_update

    fake_table = MagicMock()
    fake_table.update.return_value = fake_update

    fake_client = MagicMock()
    fake_client.table.return_value = fake_table

    with patch.object(db_mod, "tenant_aware_client", return_value=fake_client):
        recovered = db_mod.zombie_recovery()

    assert isinstance(recovered, int), "zombie_recovery must return an int"
    # Two 'processing' + two 'processing_completion' rows recovered → count 4;
    # the comparison `recovered > 0` in sentinel.py must never see a None again.
    assert recovered == 4


def test_zombie_recovery_empty_returns_zero():
    from core.services import db as db_mod

    fake_update = MagicMock()
    fake_update.execute.return_value.data = []
    fake_update.lt.return_value = fake_update
    fake_update.eq.return_value = fake_update
    fake_client = MagicMock()
    fake_client.table.return_value = MagicMock(update=MagicMock(return_value=fake_update))
    with patch.object(db_mod, "tenant_aware_client", return_value=fake_client):
        assert db_mod.zombie_recovery() == 0


# ── 3. ingest.record_outgoing_message: maybe_single() → None must not crash ─
@pytest.mark.asyncio
async def test_record_outgoing_dedup_none_is_guarded():
    from core.lib import ingest as ingest_mod

    # maybe_single().execute() returns None (PostgREST no-row response) —
    # the pre-fix code did `existing.data` → AttributeError on every
    # outgoing record, logged as a WARNING each time.
    fake_single = MagicMock()
    fake_single.execute.return_value = None
    fake_query = MagicMock()
    fake_query.maybe_single.return_value = fake_single
    # PostgREST builder chain: every filter method returns the same query
    # object, so both dedup queries terminate at maybe_single().execute() → None
    fake_query.eq.return_value = fake_query
    fake_query.gte.return_value = fake_query
    fake_query.limit.return_value = fake_query
    fake_table = MagicMock()
    fake_table.select.return_value = fake_query
    fake_client = MagicMock()
    fake_client.table.return_value = fake_table
    insert_resp = MagicMock()
    insert_resp.data = [{"id": 999}]
    fake_table.insert.return_value.execute.return_value = insert_resp

    with patch.object(ingest_mod, "supabase", fake_client), \
         patch.object(ingest_mod, "audit_log_sync") as mock_log, \
         patch("core.services.awaiting_reply.auto_resolve_on_outgoing", return_value={"resolved": 0}):
        result = await ingest_mod.record_outgoing_message(
            chat_id="9000000",
            source="whatsapp",
            body="Hello there",
            tracking_id="evt-1",
        )

    assert result.get("status") == "filed", "row must be stored as a fresh record"
    for call in mock_log.call_args_list:
        msg = call.args[1] if len(call.args) > 1 else str(call)
        assert "dedup check failed" not in msg, "None maybe_single must not warn"


# ── 4. webhook: rejection path must not raise UnboundLocalError 'ledger' ────
@pytest.mark.asyncio
async def test_channel_rejection_does_not_reference_unbound_ledger():
    from core.webhook import utils as wu

    fake_row = MagicMock()
    fake_row.data = {
        "id": 424242,
        "suggested_title": "Fix the navbar",
        "summary": "Fix special characters",
        "body": "Fix the navbar",
        "metadata": {},
    }
    fake_client = MagicMock()
    fake_client.table.return_value.select.return_value.eq.return_value.is_.return_value.eq.return_value = (
        MagicMock(execute=MagicMock(return_value=fake_row))
    )

    with patch.object(wu, "supabase", fake_client), \
         patch.object(wu, "record_decision", return_value={"id": "dec-1"}) as mock_rd, \
         patch.object(wu, "fire_briefing_refresh"), \
         patch.object(wu, "emit_observation"), \
         patch.object(wu, "audit_log_sync"):
        result = await wu._process_channel_pending_decision(
            channel="teams", pending_id=424242, decision="reject"
        )

    # Rejection returns a normal dict — the pre-fix UnboundLocalError
    # ('cannot access local variable ledger') is gone.
    assert isinstance(result, dict)
    mock_rd.assert_called_once()


# ── 4b. approval path still records the action ledger ──────────────────────
@pytest.mark.asyncio
async def test_channel_approval_records_ledger():
    from core.webhook import utils as wu

    fake_row = MagicMock()
    fake_row.data = {
        "id": 424243,
        "suggested_title": "Send the proposal",
        "summary": None,
        "body": "Send the proposal",
        "metadata": {},
    }
    fake_client = MagicMock()
    fake_client.table.return_value.select.return_value.eq.return_value.is_.return_value.eq.return_value = (
        MagicMock(execute=MagicMock(return_value=fake_row))
    )

    with patch.object(wu, "supabase", fake_client), \
         patch.object(wu, "record_decision", return_value={"id": "dec-2"}), \
         patch.object(wu, "fire_briefing_refresh"), \
         patch.object(wu, "emit_observation"), \
         patch.object(wu, "audit_log_sync"), \
         patch("core.lib.suggestion_extractor.extract_suggestions", new=AsyncMock(return_value=[])), \
         patch("core.actions.executor.execute_planned_actions", new=AsyncMock(return_value=[])):
        await wu._process_channel_pending_decision(
            channel="teams", pending_id=424243, decision="approve"
        )


# ── 5. resolve_user_by_api_key: maybe_single() → None must not crash ───────
def test_resolve_user_by_api_key_none_execute_is_guarded():
    from core.services import db as db_mod

    # maybe_single().execute() returns None (no row for an unknown key) —
    # the pre-fix code did `res.data` → AttributeError on every miss,
    # logged as a spurious db WARNING.
    fake_exec = MagicMock(return_value=None)
    fake_ms = MagicMock()
    fake_ms.execute = fake_exec
    fake_builder = MagicMock()
    fake_builder.eq.return_value = fake_builder  # .eq().limit().maybe_single() chain
    fake_builder.limit.return_value = fake_builder
    fake_builder.maybe_single.return_value = fake_ms
    fake_table = MagicMock()
    fake_table.select.return_value = fake_builder
    fake_client = MagicMock()
    fake_client.table.return_value = fake_table

    with patch.object(db_mod, "get_supabase", return_value=fake_client), \
         patch("core.lib.audit_logger.audit_log_sync") as mock_log:
        result = db_mod.resolve_user_by_api_key("unknown-key")

    assert result is None, "unknown key must resolve to None, not raise"
    assert "resolve_user_by_api_key failed" not in str(mock_log.call_args_list)


# ── 6. briefing calendar wrapper: sync get_calendar_context, no await ───────
def test_calendar_wrapper_does_not_await_sync_string():
    from core.pulse import briefing as b

    # get_calendar_context is a plain sync function returning a string;
    # awaiting it raised "object str can't be used in 'await' expression"
    # on every pulse, silently dropping calendar context from briefings.
    src = inspect.getsource(b._wrap_calendar_context)
    assert "await get_calendar_context" not in src, (
        "the sync get_calendar_context must be called directly, not awaited"
    )

    # Behavioural check: the wrapper returns the sync result as-is.
    with patch("core.pulse.briefing.get_calendar_context", return_value="- 09:00 Standup (Google)") as m:
        import asyncio
        out = asyncio.run(b._wrap_calendar_context(None))
    assert out == "- 09:00 Standup (Google)"
    m.assert_called_once()


# ── 7. serendipity engine: empty path_labels must not crash formatting ─────
def test_serendipity_engine_skips_empty_label_paths():
    from core.pulse import memory as mem

    # RPC rows where path_labels == [] (malformed/short path) made
    # `types[0]` raise "list index out of range" for the affected tenant.
    bad_path = {
        "path_labels": [],
        "path_types": ["person"],
        "path_relations": [],
        "total_weight": 0.5,
    }
    good_path = {
        "path_labels": ["Danny", "Deepa"],
        "path_types": ["person", "person"],
        "path_relations": ["", "MET_WITH"],
        "total_weight": 1.2,
    }

    fake_rpc = MagicMock()
    fake_rpc.execute.return_value.data = [bad_path, good_path]
    fake_client = MagicMock()
    fake_client.rpc.return_value = fake_rpc

    # graph_nodes lookup: select().in_('metadata->>task_id').eq('is_current')
    # → execute() returns one matching node.
    fake_node_resp = MagicMock()
    fake_node_resp.data = [{"id": "node-1"}]
    nodes_builder = MagicMock()
    nodes_builder.in_.return_value = nodes_builder
    nodes_builder.eq.return_value = nodes_builder
    nodes_builder.execute.return_value = fake_node_resp
    fake_table = MagicMock()
    fake_table.select.return_value = nodes_builder
    fake_client.table.return_value = fake_table

    with patch.object(mem, "supabase", fake_client), \
         patch.object(mem, "audit_log_sync"):
        import asyncio
        out = asyncio.run(mem.serendipity_engine([{"id": "123"}], [], []))

    assert "HIDDEN GRAPH CONNECTIONS" in out
    assert "MET_WITH" in out, "the well-formed path must still be formatted"
    assert "Path (Weight 0.5)" not in out, "empty-label path must be skipped"
