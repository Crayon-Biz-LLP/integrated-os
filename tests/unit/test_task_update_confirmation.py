"""Task-update confirmation rewrite (Sep 14 incident regression).

The Telegram-era confirmation asked "which task?" with inline buttons the
retired channel silently dropped, and its resolver only accepted button-tap
letters ('u'/'n'). Real incident (2026-09-14 10:33–10:45 UTC):
  1. "Move my rental agreement signing task to tomorrow" → gate fired on a
     SINGLE match → statement with dropped buttons, nothing executed.
  2. Same message re-sent → same statement again (loop).
  3. "Yes." → escaped to the classifier → COMPLETION (98%) → planner emitted
     close_task → the task was closed and its calendar event deleted.

Fixes under test:
  B1: single-match + explicit request executes directly; ≥2 matches still ask.
  B2: prompt is a self-contained plain-text question; resolver understands
      digits, names, acks, update phrases, re-sends, declines; consumed
      questions are marked resolved so they cannot double-fire.
  B3: a bare ack can never drive close_task/delete_event while a pending
      task_update confirmation exists — it resolves the question instead.
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import core.webhook.dispatch as disp

pytestmark = pytest.mark.webhook


def _clar_payload(matched_ids=(5966,),
                  original="Move my rental agreement signing task to tomorrow. It is not happening today"):
    return {
        "confirmation": "task_update",
        "matched_tasks": [{"id": tid, "title": "sign the rental agreement with the new landlord"}
                          for tid in matched_ids],
        "original": original,
        "candidate_request": original,
        "classification": {"title": original, "intent": "TASK", "confidence": 0.95},
    }


# ── B2: bare-ack detector ──


def test_is_bare_ack_true():
    assert disp._is_bare_ack("Yes.")
    assert disp._is_bare_ack("yes")
    assert disp._is_bare_ack("Ok!")
    assert disp._is_bare_ack("sure")
    assert disp._is_bare_ack("done")
    assert disp._is_bare_ack("no")
    assert disp._is_bare_ack("cancel")


def test_is_bare_ack_false_for_content():
    assert not disp._is_bare_ack("move it to 5pm")
    assert not disp._is_bare_ack("sign the rental agreement")
    assert not disp._is_bare_ack("update the rental task to tomorrow")
    assert not disp._is_bare_ack("")


# ── B2: prompt is a self-contained plain-text question ──


@pytest.mark.asyncio
async def test_ask_confirmation_prompt_is_self_contained():
    with patch.object(disp, "log_exchange"), \
         patch.object(disp, "send_telegram", new=AsyncMock()) as st:
        await disp.ask_task_update_confirmation(
            "move my task", {"intent": "TASK"}, chat_id=1, session_id="s1",
            matched_tasks=_clar_payload((5966, 6001))["matched_tasks"])

        text = st.await_args.args[1]
        # Echoes both candidates and states how to answer — no buttons needed.
        assert "1." in text and "2." in text
        assert "sign the rental agreement with the new landlord" in text
        assert "Reply with the number" in text
        assert "'cancel'" in text
        # Telegram-era buttons are gone: no inline_keyboard payload passed.
        assert st.await_args.kwargs.get("inline_keyboard") is None
        # The logged row is HUMAN-READABLE (the app renders it verbatim);
        # the machine payload — incl. candidate_request for re-send matching —
        # lives in metadata (Sep 14 round 2).
        logged = disp.log_exchange.call_args
        assert "{" not in logged.args[3]
        assert logged.kwargs["metadata"]["candidate_request"] == "move my task"
        assert logged.kwargs["metadata"]["matched_tasks"][0]["id"] == 5966


# ── B2: resolver vocabulary ──


@pytest.mark.asyncio
async def test_resolve_single_candidate_yes_targets_task():
    with patch.object(disp, "mark_task_update_confirmation_resolved", new=AsyncMock()), \
         patch.object(disp, "route_by_intent", new=AsyncMock()) as rb:
        ok = await disp.resolve_task_update_confirmation("Yes.", 1, "s1", _clar_payload((5966,)))
        assert ok is True
        assert rb.await_args.kwargs["task_update_id"] == 5966
        assert rb.await_args.args[0] == "TASK"
        assert rb.await_args.args[1] == _clar_payload()["original"]  # original text re-routed


@pytest.mark.asyncio
async def test_resolve_multi_candidate_bare_yes_is_not_consumed():
    # With 2+ candidates, a bare "yes" is ambiguous — must NOT consume/guess.
    with patch.object(disp, "route_by_intent", new=AsyncMock()) as rb:
        ok = await disp.resolve_task_update_confirmation("yes", 1, "s1",
                                                         _clar_payload((5966, 6001)))
        assert ok is False
        rb.assert_not_awaited()


@pytest.mark.asyncio
async def test_resolve_digit_selection():
    with patch.object(disp, "mark_task_update_confirmation_resolved", new=AsyncMock()), \
         patch.object(disp, "route_by_intent", new=AsyncMock()) as rb:
        ok = await disp.resolve_task_update_confirmation("2", 1, "s1",
                                                         _clar_payload((5966, 6001)))
        assert ok is True
        assert rb.await_args.kwargs["task_update_id"] == 6001


@pytest.mark.asyncio
async def test_resolve_task_name_answer():
    with patch.object(disp, "mark_task_update_confirmation_resolved", new=AsyncMock()), \
         patch.object(disp, "route_by_intent", new=AsyncMock()) as rb:
        ok = await disp.resolve_task_update_confirmation(
            "sign the rental agreement with the new landlord", 1, "s1",
            _clar_payload((5966, 6001)))
        assert ok is True
        assert rb.await_args.kwargs["task_update_id"] == 5966


@pytest.mark.asyncio
async def test_resolve_resend_of_original_request():
    payload = _clar_payload((5966, 6001))
    with patch.object(disp, "mark_task_update_confirmation_resolved", new=AsyncMock()), \
         patch.object(disp, "route_by_intent", new=AsyncMock()) as rb:
        ok = await disp.resolve_task_update_confirmation(
            "Move my rental agreement signing task to tomorrow. It is not happening today",
            1, "s1", payload)
        assert ok is True
        assert rb.await_args.kwargs["task_update_id"] == 5966


@pytest.mark.asyncio
async def test_resolve_decline_cancels_without_action():
    with patch.object(disp, "mark_task_update_confirmation_resolved", new=AsyncMock()), \
         patch.object(disp, "route_by_intent", new=AsyncMock()) as rb, \
         patch.object(disp, "send_telegram", new=AsyncMock()) as st:
        ok = await disp.resolve_task_update_confirmation("cancel", 1, "s1", _clar_payload((5966,)))
        assert ok is True
        rb.assert_not_awaited()          # never guesses an action
        st.assert_awaited_once()          # confirms the cancellation instead


@pytest.mark.asyncio
async def test_resolve_unrelated_content_falls_through():
    with patch.object(disp, "route_by_intent", new=AsyncMock()) as rb:
        ok = await disp.resolve_task_update_confirmation(
            "who won the match last night", 1, "s1", _clar_payload((5966,)))
        assert ok is False
        rb.assert_not_awaited()


# ── B2: consumption is remembered (no double-fire) ──


def test_parse_exchange_distinguishes_resolved_marker():
    assert disp._task_update_confirmation_from_exchange(
        json.dumps({"confirmation": "task_update", "resolved": True})) == {"resolved": True}
    assert disp._task_update_confirmation_from_exchange(
        json.dumps(_clar_payload()))["matched_tasks"][0]["id"] == 5966
    assert disp._task_update_confirmation_from_exchange("not json") is None


@pytest.mark.asyncio
async def test_fetch_stops_scan_at_resolved_marker(monkeypatch):
    # Newest row first (desc order): a resolved marker NEWER than the question
    # stops the scan — that's how consumption is recorded.
    rows = [{"content": json.dumps({"confirmation": "task_update", "resolved": True})},
            {"content": json.dumps(_clar_payload())}]
    fake = MagicMock()
    fake.table.return_value.select.return_value.eq.return_value.eq.return_value. \
        eq.return_value.order.return_value.limit.return_value.execute.return_value.data = rows
    monkeypatch.setattr(disp, "supabase", fake)
    assert await disp.fetch_pending_task_update_confirmation("s1") is None


# ── B3: bare ack + pending confirmation in _route_by_intent ──


@pytest.mark.asyncio
async def test_route_by_intent_bare_ack_resolves_pending_question():
    payload = _clar_payload((5966,))
    with patch.object(disp, "fetch_pending_task_update_confirmation",
                      new=AsyncMock(return_value=payload)), \
         patch.object(disp, "resolve_task_update_confirmation",
                      new=AsyncMock(return_value=True)) as resolve, \
         patch.object(disp, "log_decision", new=AsyncMock()):
        await disp._route_by_intent("COMPLETION", "Yes.", chat_id=1, session_id="s1",
                                    classification={"intent": "COMPLETION", "confidence": 0.98})
        resolve.assert_awaited_once_with("Yes.", 1, "s1", payload)


@pytest.mark.asyncio
async def test_route_by_intent_bare_ack_never_reaches_action_planning():
    """The exact Sep 14 failure: pending question + 'Yes.' must not plan actions."""
    payload = _clar_payload((5966,))
    with patch.object(disp, "fetch_pending_task_update_confirmation",
                      new=AsyncMock(return_value=payload)), \
         patch.object(disp, "resolve_task_update_confirmation",
                      new=AsyncMock(return_value=False)), \
         patch.object(disp, "send_telegram", new=AsyncMock()) as st, \
         patch.object(disp, "log_decision", new=AsyncMock()):
        # Even a TASK-leaning classification must be intercepted.
        await disp._route_by_intent("TASK", "Yes.", chat_id=1, session_id="s1",
                                    classification={"intent": "TASK", "confidence": 0.95})
        st.assert_awaited_once()  # re-asked, not executed
        assert "which task" in st.await_args.args[1].lower()


@pytest.mark.asyncio
async def test_route_by_intent_bare_ack_without_pending_passes_through():
    with patch.object(disp, "fetch_pending_task_update_confirmation",
                      new=AsyncMock(return_value=None)), \
         patch.object(disp, "log_decision", new=AsyncMock()), \
         patch("core.lib.entity_context.extract_context_from_source",
               new=AsyncMock(return_value=MagicMock(detected_entities=[]))), \
         patch("core.lib.suggestion_extractor.extract_suggestions",
               new=AsyncMock(return_value=([], None))), \
         patch("core.actions.executor.execute_actions_harden", new=AsyncMock()) as ex:
        await disp._route_by_intent("COMPLETION", "Yes.", chat_id=1, session_id="s1",
                                    classification={"intent": "COMPLETION", "confidence": 0.98})
        ex.assert_awaited_once()  # normal pipeline, empty actions → no-op


# ── Sep 14 round 2: no JSON in user-visible clarification rows ──


@pytest.mark.asyncio
async def test_resolved_marker_is_human_readable_with_metadata_payload():
    """The app renders bot CLARIFICATION content verbatim (WAITING ON YOU card),
    so the marker must be human text; the machine payload lives in metadata."""
    with patch.object(disp, "log_exchange") as le:
        await disp.mark_task_update_confirmation_resolved("s1", chat_id=1)
        args, kwargs = le.call_args
        content, metadata = args[3], kwargs["metadata"]
        assert "{" not in content                      # no raw JSON in the visible text
        assert metadata["confirmation"] == "task_update"
        assert metadata["resolved"] is True
        # …and the parser reads the metadata payload
        assert disp._task_update_confirmation_from_exchange(content, metadata) == {"resolved": True}


def test_parser_still_reads_legacy_json_content():
    legacy = json.dumps({"confirmation": "task_update", "resolved": True})
    assert disp._task_update_confirmation_from_exchange(legacy, None) == {"resolved": True}
    assert disp._task_update_confirmation_from_exchange(
        json.dumps(_clar_payload()), None)["matched_tasks"][0]["id"] == 5966


@pytest.mark.asyncio
async def test_fetch_reads_metadata_payload(monkeypatch):
    """New-format rows: human content + payload in metadata."""
    rows = [{"content": "\U0001f9d0 You mentioned updating a task…",
             "metadata": {"confirmation": "task_update",
                          "matched_tasks": [{"id": 5966, "title": "sign the rental agreement"}],
                          "original": "move it", "candidate_request": "move it"}}]
    fake = MagicMock()
    fake.table.return_value.select.return_value.eq.return_value.eq.return_value. \
        eq.return_value.order.return_value.limit.return_value.execute.return_value.data = rows
    monkeypatch.setattr(disp, "supabase", fake)
    payload = await disp.fetch_pending_task_update_confirmation("s1")
    assert payload and payload["matched_tasks"][0]["id"] == 5966


# ── Sep 14 round 2: date-only reschedule preserves the task's clock time ──


from core.actions.models import inject_deterministic_preserve_time  # noqa: E402


def test_preserve_time_moves_date_keeps_clock():
    action = {"operation": "reschedule", "target_id": 5966,
              "params": {"deadline": "2026-09-15"}}
    out = inject_deterministic_preserve_time(
        action, current_reminder_at="2026-09-14T11:30:00+00:00")
    assert out["params"]["new_reminder_at"].startswith("2026-09-15T11:30:00")
    assert "deadline" not in out["params"]      # deadline folded into the datetime


def test_preserve_time_never_invents_or_overrides():
    # Explicit time already present → untouched
    explicit = {"operation": "reschedule", "target_id": 1,
                "params": {"new_reminder_at": "2026-09-15T09:00:00+05:30"}}
    assert inject_deterministic_preserve_time(explicit, "2026-09-14T11:30:00+00:00") is explicit
    # No deadline at all → untouched (legit NeedsClarification path)
    no_date = {"operation": "reschedule", "target_id": 1, "params": {}}
    assert inject_deterministic_preserve_time(no_date, "2026-09-14T11:30:00+00:00") is no_date
    # Task has no reminder to preserve → untouched (fail closed, never guess)
    no_current = {"operation": "reschedule", "target_id": 1, "params": {"deadline": "2026-09-15"}}
    assert inject_deterministic_preserve_time(no_current, None) is no_current


# ── B1: gate precision lives in handler.check_task_overlap_for_update ──


def test_single_match_returns_one_item():
    # DB-backed helper: lives in core/webhook/classify.py — patch its supabase.
    import core.webhook.classify as cls
    rows = [{"id": 1, "title": "buy groceries and cook dinner"},
            {"id": 2, "title": "plan the offsite dinner party"}]
    fake = MagicMock()
    # NOTE: the real chain is .eq(...).not_.in_(...) — attribute access on
    # not_, so in_ hangs off eq.return_value.not_ directly.
    fake.table.return_value.select.return_value.eq.return_value.not_. \
        in_.return_value.execute.return_value.data = rows
    with patch.object(cls, "supabase", fake):
        matched = cls.check_task_overlap_for_update("move the dinner party planning task")
        ids = [t["id"] for t in matched]
        assert ids == [2]  # only the overlapping task matches — not everything
