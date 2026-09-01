from datetime import datetime, timezone
from typing import Tuple, Optional
from core.services.db import tenant_aware_client
from core.lib.audit_logger import audit_log_sync
from core.llm.fallback import generate_content_with_fallback
from core.llm.config import WorkloadProfile
from core.webhook.classify import CLASSIFICATION_MODEL
from core.webhook.telegram import send_telegram
from core.lib.conversation import log_exchange, _check_topic_overlap
from core.pulse.tools import create_task_direct
from core.pulse.graph import process_graph_pending_decision
import re

# ── Deterministic bypass for simple workflow replies ──
# Resolves "yes" / "no" / "sure" / "stop" in <1ms without an LLM call.
# Falls through to LLM only for ambiguous replies.

CONFIRM_PHRASES = frozenset({
    "yes", "y", "yeah", "yep", "sure", "ok", "okay", "k", "go ahead",
    "do it", "confirm", "proceed",
})

DECLINE_PHRASES = frozenset({
    "no", "n", "nope", "nah", "not now", "stop", "cancel", "never mind",
    "forget it", "dismiss", "reject", "decline", "skip", "ignore",
})

NEGATION_WORDS = frozenset({
    "not", "no", "never", "nothing", "none",
    "don\'t", "doesn\'t", "won\'t", "can\'t",
})

# ── Phase 4: action_clarification workflows ──
# A pending action that failed schema validation (NeedsClarification) is parked
# here. The user's reply is the ANSWER (a date / delta), not a yes/no — this
# workflow type resumes by re-planning the original text with the answer.

_TIME_REPLY_RE = re.compile(
    r'\b(\d{1,2}(?:st|nd|rd|th)?|monday|tuesday|wednesday|thursday|friday|saturday|'
    r'sunday|tomorrow|tonight|today|next\s+week|in\s+a\s+week|week|day|pm|am|'
    r'noon|midnight|later|now)\b',
    re.I,
)


def _looks_like_time_reply(text: str) -> bool:
    """True if the reply plausibly answers a 'when?' clarification."""
    return bool(_TIME_REPLY_RE.search(text or ""))


async def park_action_clarification(
    chat_id: int,
    thread_id: str,
    original_text: str,
    intent: str = None,
    title: str = "",
    entity: str = None,
    operation: str = None,
    target_id=None,
    missing_fields: list = None,
):
    """Park a pending action clarification (Phase 4, invariant #5).

    Creates an `action_clarification` workflow in `conversation_workflows` so
    the user's reply resumes the pending action instead of being re-classified
    from scratch. DB-backed + restart-safe (Layer 5 pattern), superseding any
    prior active workflow for the thread.
    """
    supabase = tenant_aware_client()
    from datetime import timedelta

    payload = {
        "original_text": original_text,
        "intent": intent,
        "title": title,
        "entity": entity,
        "operation": operation,
        "target_id": target_id,
        "missing_fields": missing_fields or [],
    }
    try:
        supabase.table('conversation_workflows').update({'status': 'cancelled'}) \
            .eq('thread_id', thread_id).eq('status', 'active').execute()
    except Exception as e:
        audit_log_sync("workflow", "WARNING", f"Failed to supersede workflows for {thread_id}: {e}")
    try:
        supabase.table('conversation_workflows').insert({
            'chat_id': chat_id,
            'thread_id': thread_id,
            'workflow_type': 'action_clarification',
            'status': 'active',
            'awaiting_user_input': True,
            'payload': payload,
            'expires_at': (datetime.now(timezone.utc) + timedelta(days=7)).isoformat(),
        }).execute()
        audit_log_sync("workflow", "INFO",
                        f"Parked action_clarification for {thread_id} (op={operation}, missing={missing_fields})")
    except Exception as e:
        audit_log_sync("workflow", "WARNING", f"Failed to park action clarification: {e}")


async def _emit_clarification_observation(workflow: dict, thread_id: str, outcome: str) -> None:
    """Persist a clarification exchange into the learning loop (vision #4).

    Every resolution of an action_clarification workflow becomes an observation
    in `subsystem_telemetry` + its pattern counters (the codebase's learning
    mechanism) — so Rhodey accumulates how often each operation needs
    clarification and how the user resolves it. Fail-open: telemetry never
    breaks the clarification loop.
    """
    payload = workflow.get("payload") or {}
    features = {
        "operation": payload.get("operation") or "unknown",
        "missing_fields": payload.get("missing_fields") or [],
        "intent": payload.get("intent") or "unknown",
    }
    try:
        from core.lib.telemetry import emit_observation
        await emit_observation(
            subsystem="action_planner",
            event_type="clarification",
            features=features,
            predicted=None,
            actual=None,
            outcome=outcome,
            session_id=thread_id,
            source="workflow",
        )
    except Exception as e:
        audit_log_sync("workflow", "WARNING", f"Clarification learning emit failed (non-critical): {e}")


async def _resume_action_clarification(chat_id: int, text: str, thread_id: str, workflow: dict) -> Tuple[bool, Optional[str]]:
    """Resume an action_clarification workflow: the reply is the answer.

    Re-plans the pending original text + the user's answer anchored to the
    pending action context. Decline replies cancel the pending action.
    Unrelated replies fall through to normal routing (workflow stays active).
    """
    supabase = tenant_aware_client()
    w_id = workflow['id']
    payload = workflow.get('payload') or {}
    original_text = payload.get('original_text', '')
    intent = payload.get('intent')
    title = payload.get('title', '')
    entity = payload.get('entity')

    now_iso = datetime.now(timezone.utc).isoformat()

    # Decline replies abort the pending action
    if get_deterministic_decision(text) == 'decline':
        try:
            supabase.table('conversation_workflows').update({
                'status': 'cancelled', 'resolved_at': now_iso, 'updated_at': now_iso,
            }).eq('id', w_id).eq('status', 'active').execute()
        except Exception as e:
            audit_log_sync("workflow", "WARNING", f"Failed to cancel workflow {w_id}: {e}")
        await send_telegram(chat_id, "Got it — I won't change that.")
        log_exchange(thread_id, 'user', 'WORKFLOW_REPLY', text, chat_id)
        await _emit_clarification_observation(workflow, thread_id, "rejected")
        return True, None

    # Only consume the message if it plausibly ANSWERS the clarification
    # (a date/delta) or declines it. Anything else (e.g. "what's the weather")
    # falls through to normal routing — the workflow stays active, bounded by
    # its 7-day expiry and supersede-on-repark. The generic topic-overlap guard
    # can't discriminate here: it returns True for entity-less filler text.
    if get_deterministic_decision(text) != 'decline' and not _looks_like_time_reply(text):
        return False, None

    # The answer completes the original request — re-plan with both.
    combined = f"{original_text}\n[User clarification:] {text}"
    from core.lib.suggestion_extractor import extract_suggestions
    from core.lib.entity_context import extract_context_from_source
    from core.actions.executor import execute_actions_harden
    from core.actions.models import NeedsClarification
    
    try:
        actions, _ = await extract_suggestions(combined, title=title, entity=entity, intent=intent)
    except NeedsClarification as nc:
        # Still unclear — re-ask and keep the workflow active
        await send_telegram(chat_id, nc.to_question())
        log_exchange(thread_id, 'user', 'WORKFLOW_REPLY', text, chat_id)
        return True, None

    if not actions:
        # Couldn't resolve an action from the reply — close the loop honestly.
        try:
            supabase.table('conversation_workflows').update({
                'status': 'cancelled', 'resolved_at': now_iso, 'updated_at': now_iso,
            }).eq('id', w_id).eq('status', 'active').execute()
        except Exception:
            pass
        await send_telegram(chat_id, "I couldn't work that out from what you said — the task is unchanged. Try again whenever.")
        log_exchange(thread_id, 'user', 'WORKFLOW_REPLY', text, chat_id)
        await _emit_clarification_observation(workflow, thread_id, "failed")
        return True, None

    ctx = await extract_context_from_source(combined, timing="sync")
    await execute_actions_harden(
        actions, chat_id, text=combined, entity=entity,
        source="telegram", sender="user", session_id=thread_id, intent=intent,
        entity_context=ctx,
    )

    # Resolve the workflow (atomic — only if still active)
    try:
        supabase.table('conversation_workflows').update({
            'status': 'resolved', 'resolved_at': now_iso, 'updated_at': now_iso,
        }).eq('id', w_id).eq('status', 'active').execute()
    except Exception as e:
        audit_log_sync("workflow", "WARNING", f"Failed to resolve workflow {w_id}: {e}")
    audit_log_sync("workflow", "INFO",
                   f"action_clarification resolved (w_id={w_id}, reply={text[:60]!r})")
    log_exchange(thread_id, 'user', 'WORKFLOW_REPLY', text, chat_id)
    await _emit_clarification_observation(workflow, thread_id, "confirmed")
    return True, None


def get_deterministic_decision(text: str) -> str | None:
    """Resolve simple confirm/decline replies without an LLM call.

    Returns 'confirm', 'decline', or None if the reply is ambiguous.
    Uses exact phrase match + short-phrase heuristics.
    """
    cleaned = re.sub(r'[^\w\s]', '', text.lower()).strip()
    if not cleaned:
        return None

    if cleaned in CONFIRM_PHRASES:
        return 'confirm'
    if cleaned in DECLINE_PHRASES:
        return 'decline'

    # Short phrases (≤4 words): check for confirmation/negation patterns
    words = cleaned.split()
    if len(words) <= 4:
        has_negation = any(w in NEGATION_WORDS for w in words)
        has_confirm = any(w in CONFIRM_PHRASES for w in words)
        has_decline = any(w in DECLINE_PHRASES for w in words)

        # "yes do it" or "sure go ahead"
        if has_confirm and not has_negation and not has_decline:
            return 'confirm'
        # "no thanks" or "not now"
        if has_decline or has_negation:
            return 'decline'

    return None


def _has_decline_language(text: str) -> bool:
    """True if the reply carries explicit decline/negation intent.

    Gates LLM-produced 'decline' decisions: an unrelated note (e.g. "By the
    way, I need milk") must never cancel an active workflow just because the
    LLM read it as a rejection. Single-word declines ('no', 'stop', 'cancel',
    ...), negation words ('not', 'never', "don't", ...) and multi-word
    decline phrases ('not now', 'never mind', ...) count; anything else
    doesn't.
    """
    if not text:
        return False
    lowered = text.lower()
    for phrase in DECLINE_PHRASES:
        if " " in phrase and phrase in lowered:
            return True
    tokens = {tok.strip(".,!?;:'\"").lower() for tok in lowered.split()}
    return bool(tokens & DECLINE_PHRASES) or bool(tokens & NEGATION_WORDS)


async def check_and_resume_workflow(chat_id: int, text: str, thread_id: str) -> Tuple[bool, Optional[str]]:
    """
    Checks if there's an active workflow for this chat.
    If so, evaluates the user's reply to see if it confirms, declines, or ignores the workflow.
    Returns (True, None) if the message was consumed, (True, ancillary_text) if the workflow
    handled the offer but there's a separate instruction to re-process, (False, None) if normal routing.
    """
    supabase = tenant_aware_client()
    
    # Prune expired workflows first
    try:
        now_iso = datetime.now(timezone.utc).isoformat()
        supabase.table('conversation_workflows').update({
            'status': 'expired',
            'resolved_at': now_iso,
            'updated_at': now_iso
        }).eq('chat_id', chat_id).eq('status', 'active').lt('expires_at', now_iso).execute()
    except Exception:
        pass
    
    # Always query DB directly (cache removed — DB lookup is fast and restart-safe)
    try:
        res = supabase.table('conversation_workflows') \
            .select('*') \
            .eq('chat_id', chat_id) \
            .eq('status', 'active') \
            .eq('awaiting_user_input', True) \
            .execute()
    except Exception as e:
        audit_log_sync("workflow", "ERROR", f"DB lookup failure falling open to general: {e}")
        return False, None
        
    if not res.data:
        return False, None
        
    if len(res.data) > 1:
        # Mark older ones as superseded
        sorted_ws = sorted(res.data, key=lambda x: x['created_at'])
        superseded_ids = [w['id'] for w in sorted_ws[:-1]]
        now_iso = datetime.now(timezone.utc).isoformat()
        supabase.table('conversation_workflows').update({
            'status': 'cancelled', 
            'resolved_at': now_iso,
            'updated_at': now_iso
        }).in_('id', superseded_ids).execute()
        
        audit_log_sync("workflow", "WARNING", f"Multiple active workflows for chat {chat_id}. Superseded older ones, falling open.")
        return False, None
        
    workflow = res.data[0]
    w_id = workflow['id']
    w_type = workflow['workflow_type']
    payload = workflow.get('payload') or {}

    # Phase 4 (invariant #5): action_clarification — the reply is the ANSWER
    # (a date / delta), not a yes/no. Handled before the confirm/decline
    # analysis; unrelated replies fall through to normal routing.
    if w_type == "action_clarification":
        return await _resume_action_clarification(chat_id, text, thread_id, workflow)

    # Batch-resume fields — MUST be initialized before the deterministic
    # bypass below: a simple "yes/sure" reply to a batch workflow skips the
    # LLM block (step 2) that normally assigns these, and the confirm path
    # reads them at step 3. UnboundLocalError otherwise.
    signal_decisions = []
    has_other_content = False
    other_content_text = ""
    
    # 0. Deterministic topical relevance guard (before any LLM call)
    if text and not _check_topic_overlap(text, payload):
        audit_log_sync("workflow", "INFO",
            f"Workflow {w_id} bypassed: message entities don't match workflow payload — falling through")
        return False, None

    # 1: Deterministic bypass — resolve simple yes/no/sure/stop without an LLM call
    decision = get_deterministic_decision(text)

    # 2: LLM fallback — only for ambiguous or complex replies
    if not decision:
        from core.prompts.workflow import build_workflow_resume_prompt
        prompt = build_workflow_resume_prompt(w_type, payload, text)
        try:
            analysis_res = await generate_content_with_fallback(
                prompt=prompt,
                workload=WorkloadProfile.INTERACTIVE,
                primary_model=CLASSIFICATION_MODEL,
                config={'response_mime_type': 'application/json'}
            )
            raw = analysis_res.parse_json()
        except Exception as e:
            audit_log_sync("workflow", "ERROR", f"LLM eval failed falling open: {e}")
            return False, None
        
        if w_type == "batch":
            signal_decisions = raw.get("decisions", [])
            has_other_content = raw.get("has_other_content", False)
            other_content_text = raw.get("other_content_text", "")
            confirmed = any(sd.get("decision") == "confirm" for sd in signal_decisions)
            declined = any(sd.get("decision") == "decline" for sd in signal_decisions)
            if confirmed:
                decision = "confirm"
            elif declined and _has_decline_language(text):
                # 'decline' is only authoritative when the reply actually says
                # no — an LLM misread of an unrelated message must not cancel
                # the workflow.
                decision = "decline"
            else:
                # The reply confirms no signal and doesn't explicitly decline
                # (e.g. an unrelated note) — fall through to normal routing so
                # the message is captured normally and the workflow stays
                # active, awaiting the user's real answer.
                audit_log_sync("workflow", "INFO",
                    f"Workflow {w_id} bypassed: batch reply unrelated to all signals — falling through")
                return False, None
        else:
            decision = raw.get("decision", "unrelated")
            
    # 3. Handle Decision
    if decision == "unrelated":
        audit_log_sync("workflow", "INFO", f"Workflow {w_id} bypassed due to unrelated reply. Remains active.")
        return False, None
        
    # ATOMIC UPDATE FOR IDEMPOTENCY
    try:
        now_iso = datetime.now(timezone.utc).isoformat()
        update_res = supabase.table('conversation_workflows').update({
            'status': 'resolved' if decision == 'confirm' else 'cancelled',
            'resolved_at': now_iso,
            'updated_at': now_iso
        }).eq('id', w_id).eq('status', 'active').execute()
        
        if not update_res.data:
            audit_log_sync("workflow", "WARNING", f"Workflow {w_id} already resolved concurrently. Skipping.")
            return True, other_content_text if decision == "decline" and other_content_text else None
    except Exception as e:
        audit_log_sync("workflow", "ERROR", f"Failed atomic update for {w_id}: {e}")
        return False, None
    
    if decision == "decline":
        has_other = has_other_content and bool(other_content_text.strip())
        reply_text = "Cancelled the pending items." if has_other else "Cancelled."
        await send_telegram(chat_id, reply_text)
        log_exchange(thread_id, 'user', 'WORKFLOW_REPLY', text, chat_id)
        log_exchange(thread_id, 'bot', 'WORKFLOW_RESOLUTION', reply_text, chat_id)
        if has_other:
            return True, other_content_text.strip()
        return True, None
        
    elif decision == "confirm":
        reply_text = "Done."

        if w_type == "batch":
            # ── Auto-approve new orgs before creating tasks ──
            # This eliminates the timing window: org exists BEFORE task is created,
            # so create_task_direct can resolve organisation_id at creation time.
            payload_new_orgs = payload.get("new_orgs", [])
            approved_orgs = {}  # {label: org_db_record_id}
            for org_info in payload_new_orgs:
                pending_id = org_info.get("pending_id")
                if not pending_id:
                    continue
                try:
                    result = await process_graph_pending_decision(
                        pending_id=pending_id, decision='approve'
                    )
                    if result.get('success'):
                        label = org_info.get('label', 'Unknown')
                        reply_text += f"\n🏢 Organization approved: {label}"
                        approved_orgs[label] = True
                        audit_log_sync("workflow", "INFO",
                            f"Batch auto-approved org '{label}' (pending_id={pending_id})")
                except Exception as e:
                    audit_log_sync("workflow", "WARNING",
                        f"Failed to auto-approve org (pending_id={pending_id}): {e}")

            signals_list = payload.get("signals", [])
            
            # If confirmed via deterministic bypass, approve all signals
            if not signal_decisions and decision == "confirm":
                signal_decisions = [{"index": i, "decision": "confirm"} for i in range(len(signals_list))]

            # Deserialize EntityContext from payload (if stored at creation time)
            entity_ctx_dict = payload.get("entity_context")
            entity_ctx = None
            if entity_ctx_dict:
                try:
                    from core.lib.entity_context import EntityContext
                    entity_ctx = EntityContext.from_dict(entity_ctx_dict)
                except Exception as ec_e:
                    audit_log_sync("workflow", "WARNING", f"Failed to deserialize EntityContext: {ec_e}")

            # Cache active tasks once for task_closure matching
            active_tasks = []
            for sd in signal_decisions:
                if sd.get("decision") != "confirm":
                    continue
                idx = sd.get("index")
                if idx is None or idx < 0 or idx >= len(signals_list):
                    continue
                sig = signals_list[idx]
                sig_type = sig.get("type")
                title = sig.get("task_title") or sig.get("proposed_title") or sig.get("title") or sig.get("target_task_description", "") or "New Task"
                reminder_at = sig.get("reminder_at")

                # Determine org name: workflow signal > approved org (from original text)
                organization_name = sig.get("organization_name")
                if not organization_name and approved_orgs:
                    # Use the first approved org label as the org context
                    organization_name = list(approved_orgs.keys())[0]

                if sig_type in ("deadline", "calendar_event"):
                    res = await create_task_direct(title=title, reminder_at=reminder_at, organization_name=organization_name, entity_context=entity_ctx)
                    if res.get("action") == "created":
                        reply_text += f"\n✅ Task created: {title}"
                elif sig_type == "task_imperative":
                    res = await create_task_direct(title=title, reminder_at=reminder_at, organization_name=organization_name, entity_context=entity_ctx)
                    if res.get("action") == "created":
                        reply_text += f"\n✅ Task created: {title}" 
                elif sig_type == "task_closure":
                    if not active_tasks:
                        tasks_res = supabase.table("tasks") \
                            .select("id, title") \
                            .eq("is_current", True) \
                            .not_.in_("status", ["done", "cancelled"]) \
                            .execute()
                        active_tasks = tasks_res.data or []
                    target = sig.get("target_task_description", "") or title
                    target_lower = target.lower()
                    matching = [
                        t for t in active_tasks
                        if any(word in t["title"].lower() for word in target_lower.split() if len(word) > 3)
                    ]
                    if matching:
                        from core.pulse.tools import update_task_status
                        for t in matching:
                            update_task_status(task_id=t["id"], status="done")

        await send_telegram(chat_id, reply_text)
        log_exchange(thread_id, 'user', 'WORKFLOW_REPLY', text, chat_id)
        log_exchange(thread_id, 'bot', 'WORKFLOW_RESOLUTION', reply_text, chat_id)
        has_other = has_other_content and bool(other_content_text.strip())
        if has_other:
            return True, other_content_text.strip()
        return True, None
        
    return False, None
