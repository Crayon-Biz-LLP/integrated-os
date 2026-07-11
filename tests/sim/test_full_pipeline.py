"""Full pipeline simulation test.

Exercises the refactored process_single_dump (ProcessInput) contract
through pre-decided and full end-to-end paths with real Gemini calls.

Run: LIVE_DB=true python -m pytest tests/sim/test_full_pipeline.py -v
"""

import os
import pytest
from datetime import datetime, timezone

from core.lib.process_input import ProcessInput
from core.services.db import get_supabase

skip_unless_live_db = pytest.mark.skipif(
    os.getenv("LIVE_DB") != "true",
    reason="Requires LIVE_DB=true (real Supabase)"
)

supabase = get_supabase()


def _now_ts():
    return datetime.now(timezone.utc).isoformat()


def _recent_rows(table, since_ts, id_col="id", limit=5):
    return supabase.table(table).select(id_col).gte("created_at", since_ts).limit(limit).execute().data or []


# ═══════════════════════════════════════════════════════════════════════
# Category A — Pre-decided path (ProcessInput, skips classify)
# ═══════════════════════════════════════════════════════════════════════

class TestPreDecidedPath:

    @skip_unless_live_db
    @pytest.mark.asyncio
    async def test_p1_basic_note(self, seed_full_test_data, mock_telegram):
        from core.agents.quick_process import process_single_dump
        pi = ProcessInput(category="NOTE", text="[SIM_TEST] P1: Met Shifrah about Qhord UI", source="sim_test")
        result = await process_single_dump(text=pi.text, metadata={}, input=pi)
        assert result["action"] == "filed"
        assert result["type"] == "note"
        assert result["memory_id"] is not None
        seed_full_test_data["_created_memories"].append(result["memory_id"])
        row = supabase.table("memories").select("content, memory_type") \
            .eq("id", result["memory_id"]).single().execute()
        assert row.data["content"] == "[SIM_TEST] P1: Met Shifrah about Qhord UI"
        assert row.data["memory_type"] == "note"

    @skip_unless_live_db
    @pytest.mark.asyncio
    async def test_p2_basic_task(self, seed_full_test_data, mock_telegram, mock_google):
        from core.agents.quick_process import process_single_dump
        pi = ProcessInput(category="TASK", text="[SIM_TEST] P2: Prepare Qhord demo", title="Qhord demo prep", source="sim_test")
        result = await process_single_dump(text=pi.text, metadata={}, input=pi)
        assert result["action"] == "created"
        assert result["task_id"] is not None
        seed_full_test_data["_created_tasks"].append(result["task_id"])
        row = supabase.table("tasks").select("title, priority, dedup_key, status") \
            .eq("id", result["task_id"]).single().execute()
        assert row.data["title"] == "Qhord demo prep"
        assert row.data["priority"] == "important"
        assert row.data["dedup_key"] is not None
        assert row.data["status"] == "todo"

    @skip_unless_live_db
    @pytest.mark.asyncio
    async def test_p3_task_with_project(self, seed_full_test_data, mock_telegram, mock_google):
        from core.agents.quick_process import process_single_dump
        pi = ProcessInput(category="TASK", text="[SIM_TEST] P3: Qhord invoice prep", title="Invoice prep",
                          project_name="[SIM_TEST] Qhord", source="sim_test")
        result = await process_single_dump(text=pi.text, metadata={}, input=pi)
        assert result["action"] == "created"
        seed_full_test_data["_created_tasks"].append(result["task_id"])
        row = supabase.table("tasks").select("project_id, organization_id") \
            .eq("id", result["task_id"]).single().execute()
        expected_pid = seed_full_test_data["projects"]["[SIM_TEST] Qhord"]
        assert row.data["project_id"] == expected_pid
        assert row.data["organization_id"] is None

    @skip_unless_live_db
    @pytest.mark.asyncio
    async def test_p4_task_with_org_fallback(self, seed_full_test_data, mock_telegram, mock_google):
        from core.agents.quick_process import process_single_dump
        pi = ProcessInput(category="TASK", text="[SIM_TEST] P4: Equisoft compliance review", title="Equisoft review",
                          project_name="[SIM_TEST] Equisoft", source="sim_test")
        result = await process_single_dump(text=pi.text, metadata={}, input=pi)
        assert result["action"] == "created"
        seed_full_test_data["_created_tasks"].append(result["task_id"])
        row = supabase.table("tasks").select("project_id, organization_id") \
            .eq("id", result["task_id"]).single().execute()
        expected_oid = seed_full_test_data["orgs"]["[SIM_TEST] Equisoft"]
        assert row.data["organization_id"] == expected_oid
        assert row.data["project_id"] is None

    @skip_unless_live_db
    @pytest.mark.asyncio
    async def test_p5_task_unknown_org(self, seed_full_test_data, mock_telegram, mock_google):
        from core.agents.quick_process import process_single_dump
        pi = ProcessInput(category="TASK", text="[SIM_TEST] P5: Unknown project planning", title="Unknown project task",
                          project_name="[SIM_TEST] NonExistentOrg", source="sim_test")
        result = await process_single_dump(text=pi.text, metadata={}, input=pi)
        assert result["action"] == "created"
        seed_full_test_data["_created_tasks"].append(result["task_id"])
        row = supabase.table("tasks").select("project_id, organization_id") \
            .eq("id", result["task_id"]).single().execute()
        assert row.data["project_id"] is None
        assert row.data["organization_id"] is None
        signals = supabase.table("project_creation_signals") \
            .select("*").ilike("project_name", "[SIM_TEST] NonExistentOrg%") \
            .execute()
        assert len(signals.data) >= 1

    @skip_unless_live_db
    @pytest.mark.asyncio
    async def test_p6_task_timed_reminder(self, seed_full_test_data, mock_telegram, mock_google):
        from core.agents.quick_process import process_single_dump
        pi = ProcessInput(category="TASK", text="[SIM_TEST] P6: Meeting with time", title="Timed meeting",
                          reminder_at="2026-07-15T10:00:00+05:30", source="sim_test")
        result = await process_single_dump(text=pi.text, metadata={}, input=pi)
        assert result["action"] == "created"
        seed_full_test_data["_created_tasks"].append(result["task_id"])
        row = supabase.table("tasks").select("reminder_at, google_event_id") \
            .eq("id", result["task_id"]).single().execute()
        assert row.data["reminder_at"] is not None
        if row.data.get("google_event_id"):
            assert row.data["google_event_id"] == "mock_event_id"

    @skip_unless_live_db
    @pytest.mark.asyncio
    async def test_p7_task_date_only_reminder(self, seed_full_test_data, mock_telegram, mock_google):
        from core.agents.quick_process import process_single_dump
        pi = ProcessInput(category="TASK", text="[SIM_TEST] P7: Date task", title="Date-only task",
                          reminder_at="2026-07-15", source="sim_test")
        result = await process_single_dump(text=pi.text, metadata={}, input=pi)
        assert result["action"] == "created"
        seed_full_test_data["_created_tasks"].append(result["task_id"])
        assert not mock_google["sync_to_calendar"].called

    @skip_unless_live_db
    @pytest.mark.asyncio
    async def test_p8_task_dedup(self, seed_full_test_data, mock_telegram, mock_google):
        from core.agents.quick_process import process_single_dump
        pi = ProcessInput(category="TASK", text="[SIM_TEST] P8: Buy groceries", title="Buy groceries", source="sim_test")
        r1 = await process_single_dump(text=pi.text, metadata={}, input=pi)
        assert r1["action"] == "created"
        seed_full_test_data["_created_tasks"].append(r1["task_id"])
        r2 = await process_single_dump(text=pi.text, metadata={}, input=pi)
        assert r2["action"] == "skipped"
        assert r2["reason"] == "duplicate"

    @skip_unless_live_db
    @pytest.mark.asyncio
    async def test_p9_note_with_url(self, seed_full_test_data, test_chat_id, mock_telegram):
        from core.webhook.dispatch import handle_confident_note
        url = f"https://sim-test-{os.getpid()}.example.com/p9-doc"
        text = f"[SIM_TEST] P9: Read {url}"
        receipt = await handle_confident_note(
            text=text, chat_id=test_chat_id,
            receipt="Note vaulted.", source="sim_test",
        )
        assert receipt is not None
        rows = supabase.table("resources").select("*").eq("url", url).limit(5).execute()
        assert len(rows.data) >= 1
        assert rows.data[0]["url"] == url

    @skip_unless_live_db
    @pytest.mark.asyncio
    async def test_p10_resource_category(self, seed_full_test_data, mock_telegram):
        from core.agents.quick_process import process_single_dump
        pi = ProcessInput(category="RESOURCE", text="[SIM_TEST] P10: Design doc", url="https://figma.com/sim-test-file", source="sim_test")
        result = await process_single_dump(text=pi.text, metadata={}, input=pi)
        assert result["action"] == "filed"
        assert result["type"] == "resource"
        seed_full_test_data["_created_memories"].append(result.get("memory_id"))
        rows = supabase.table("resources").select("*").ilike("url", "%sim-test-file%").limit(5).execute()
        assert len(rows.data) >= 1

    @skip_unless_live_db
    @pytest.mark.asyncio
    async def test_p11_project_update(self, seed_full_test_data, mock_telegram):
        from core.agents.quick_process import process_single_dump
        pi = ProcessInput(category="NOTE", text="[SIM_TEST] P11: Qhord UI is complete", memory_type="project_update", source="sim_test")
        result = await process_single_dump(text=pi.text, metadata={}, input=pi)
        assert result["action"] == "filed"
        seed_full_test_data["_created_memories"].append(result.get("memory_id"))
        row = supabase.table("memories").select("memory_type, content") \
            .eq("id", result["memory_id"]).single().execute()
        assert row.data["memory_type"] == "project_update"
        assert "[SIM_TEST] P11:" in row.data["content"]


# ═══════════════════════════════════════════════════════════════════════
# Category B — Full end-to-end (real classify → handler → persist)
# ═══════════════════════════════════════════════════════════════════════

@pytest.fixture
def test_chat_id():
    return 999999001


class TestEndToEnd:

    @skip_unless_live_db
    @pytest.mark.asyncio
    async def test_p12_schedule_task(self, seed_full_test_data, test_chat_id, mock_telegram, mock_google):
        from core.webhook.classify import classify_intent
        from core.webhook.dispatch import handle_confident_task
        text = "[SIM_TEST] P12: Schedule Qhord review tomorrow at 2pm"
        classification = await classify_intent(text, context=[])
        assert classification["intent"] == "TASK", f"Expected TASK, got {classification['intent']}"
        ts_before = _now_ts()
        receipt = await handle_confident_task(
            text=text,
            title=classification.get("title", text),
            time_context=classification.get("time_context", ""),
            chat_id=test_chat_id,
            receipt=classification.get("receipt"),
            source="sim_test",
        )
        assert receipt is not None
        tasks = supabase.table("tasks").select("id").gte("created_at", ts_before).limit(5).execute()
        assert len(tasks.data) >= 1
        for t in tasks.data:
            seed_full_test_data["_created_tasks"].append(t["id"])
        tc = classification.get("time_context") or ""
        if 'T' in tc:
            assert mock_google["sync_to_calendar"].called

    @skip_unless_live_db
    @pytest.mark.asyncio
    async def test_p13_note_about_person(self, seed_full_test_data, test_chat_id, mock_telegram):
        from core.webhook.classify import classify_intent
        text = "[SIM_TEST] P13: Met Shifrah about Ashraya budget"
        classification = await classify_intent(text, context=[])
        receipt = None
        intent = classification["intent"]
        ts_before = _now_ts()
        if intent == "NOTE":
            from core.webhook.dispatch import handle_confident_note
            receipt = await handle_confident_note(
                text=text, chat_id=test_chat_id,
                receipt=classification.get("receipt", "Note vaulted."),
                source="sim_test",
            )
        else:
            from core.webhook.dispatch import handle_confident_task
            receipt = await handle_confident_task(
                text=text, title=classification.get("title", text),
                time_context=classification.get("time_context", ""),
                chat_id=test_chat_id, receipt=classification.get("receipt"),
                source="sim_test",
            )
        assert receipt is not None
        tbl = "memories" if intent == "NOTE" else "tasks"
        rows = supabase.table(tbl).select("id").gte("created_at", ts_before).limit(5).execute()
        assert len(rows.data) >= 1
        for r in rows.data:
            if intent == "NOTE":
                seed_full_test_data["_created_memories"].append(r["id"])
            else:
                seed_full_test_data["_created_tasks"].append(r["id"])

    @skip_unless_live_db
    @pytest.mark.asyncio
    async def test_p14_url_note(self, seed_full_test_data, test_chat_id, mock_telegram):
        from core.webhook.classify import classify_intent
        text = "[SIM_TEST] P14: Read this https://blog.sim-test.tech/pipeline-test"
        classification = await classify_intent(text, context=[])
        receipt = None
        intent = classification["intent"]
        if intent == "NOTE":
            from core.webhook.dispatch import handle_confident_note
            receipt = await handle_confident_note(
                text=text, chat_id=test_chat_id,
                receipt=classification.get("receipt", "Note vaulted."),
                source="sim_test",
            )
        else:
            from core.webhook.dispatch import handle_confident_task
            receipt = await handle_confident_task(
                text=text, title=classification.get("title", text),
                time_context=classification.get("time_context", ""),
                chat_id=test_chat_id, receipt=classification.get("receipt"),
                source="sim_test",
            )
        assert receipt is not None
        if intent == "NOTE":
            resources = supabase.table("resources").select("*").ilike("url", "%sim-test.tech%").limit(5).execute()
            assert len(resources.data) >= 1

    @skip_unless_live_db
    @pytest.mark.asyncio
    async def test_p15_dedup_via_classify(self, seed_full_test_data, test_chat_id, mock_telegram, mock_google):
        from core.webhook.classify import classify_intent
        from core.webhook.dispatch import handle_confident_task
        text = "[SIM_TEST] P15: Buy groceries"
        classification = await classify_intent(text, context=[])
        assert classification["intent"] == "TASK", f"Expected TASK, got {classification['intent']}"
        ts_before = _now_ts()
        r1 = await handle_confident_task(
            text=text,
            title=classification.get("title", text),
            time_context=classification.get("time_context", ""),
            chat_id=test_chat_id,
            receipt=classification.get("receipt"),
            source="sim_test",
        )
        assert r1 is not None
        r2 = await handle_confident_task(
            text=text,
            title=classification.get("title", text),
            time_context=classification.get("time_context", ""),
            chat_id=test_chat_id,
            receipt=classification.get("receipt"),
            source="sim_test",
        )
        assert r2 is not None
        tasks = supabase.table("tasks").select("id").gte("created_at", ts_before).limit(10).execute()
        assert len(tasks.data) == 1
        for t in tasks.data:
            seed_full_test_data["_created_tasks"].append(t["id"])


# ═══════════════════════════════════════════════════════════════════════
# Category C — Entity extraction + enrichment (real Gemini calls)
# ═══════════════════════════════════════════════════════════════════════

class TestEntityExtraction:

    @skip_unless_live_db
    @pytest.mark.asyncio
    async def test_p16_note_with_entities(self, seed_full_test_data, mock_telegram):
        from core.agents.quick_process import process_single_dump
        pi = ProcessInput(category="NOTE", text="[SIM_TEST] P16: Marcus from Equisoft is joining Qhord project", source="sim_test")
        result = await process_single_dump(text=pi.text, metadata={}, input=pi)
        assert result["action"] == "filed"
        seed_full_test_data["_created_memories"].append(result.get("memory_id"))
        memory = supabase.table("memories").select("id").eq("id", result["memory_id"]).single().execute()
        assert memory.data is not None

    @skip_unless_live_db
    @pytest.mark.asyncio
    async def test_p17_memory_enrichment(self, seed_full_test_data, test_chat_id, mock_telegram):
        from core.webhook.classify import classify_intent
        from core.webhook.dispatch import handle_confident_note
        text = "[SIM_TEST] P17: Marcus from Equisoft is working on Qhord"
        classification = await classify_intent(text, context=[])
        receipt = None
        intent = classification["intent"]
        ts_before = _now_ts()
        if intent in ("NOTE", "PROJECT_UPDATE"):
            from core.webhook.dispatch import handle_confident_note
            receipt = await handle_confident_note(
                text=text, chat_id=test_chat_id,
                receipt=classification.get("receipt", "Note vaulted."),
                source="sim_test",
            )
        else:
            from core.webhook.dispatch import handle_confident_task
            receipt = await handle_confident_task(
                text=text, title=classification.get("title", text),
                time_context=classification.get("time_context", ""),
                chat_id=test_chat_id, receipt=classification.get("receipt"),
                source="sim_test",
            )
        assert receipt is not None
        if intent in ("NOTE", "PROJECT_UPDATE"):
            rows = supabase.table("memories").select("id").gte("created_at", ts_before).limit(5).execute()
        else:
            rows = supabase.table("tasks").select("id").gte("created_at", ts_before).limit(5).execute()
        assert len(rows.data) >= 1
        for r in rows.data:
            if intent in ("NOTE", "PROJECT_UPDATE"):
                seed_full_test_data["_created_memories"].append(r["id"])
            else:
                seed_full_test_data["_created_tasks"].append(r["id"])


# ═══════════════════════════════════════════════════════════════════════
# Category D — Completion flow
# ═══════════════════════════════════════════════════════════════════════

class TestCompletion:

    @skip_unless_live_db
    @pytest.mark.asyncio
    async def test_p18_completion_flow(self, seed_full_test_data, test_chat_id, mock_telegram, mock_google):
        from core.webhook.classify import classify_intent
        from core.webhook.completion_handler import handle_confident_completion

        task_title = "[SIM_TEST] P18: Complete this test task"
        task_res = supabase.table("tasks").insert({
            "title": task_title,
            "status": "todo",
            "priority": "important",
            "is_current": True,
            "direction": "inbound",
        }).execute()
        assert task_res.data
        existing_task_id = task_res.data[0]["id"]
        seed_full_test_data["_created_tasks"].append(existing_task_id)

        text = "[SIM_TEST] P18: Complete this test task is done"
        classification = await classify_intent(text, context=[])
        assert classification["intent"] == "COMPLETION", \
            f"Expected COMPLETION, got {classification['intent']}"
        await handle_confident_completion(
            text=text,
            title=classification.get("title", text),
            chat_id=test_chat_id,
            receipt=classification.get("receipt"),
            source="sim_test",
        )
        refreshed = supabase.table("tasks").select("status").eq("id", existing_task_id).single().execute()
        assert refreshed.data["status"] in ("done", "completed"), \
            f"Expected task status done/completed, got {refreshed.data['status']}"
