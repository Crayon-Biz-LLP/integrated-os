import uuid
import hashlib
from typing import List, Optional, Dict
from core.actions.models import Action
from core.services.db import get_supabase
from core.lib.audit_logger import audit_log_sync
from core.lib.state_machines import guard_require_valid_transition
from core.webhook.telegram import send_telegram


# ── Real-time org detection for NOTE and TASK paths ──

async def _detect_new_orgs_and_create_pending(text: str, chat_id: int, cached_entities: List = None) -> List[Dict]:
    """Run lightweight entity detection for new orgs.
    
    If Pattern D detects new orgs (is_new=True, type='organization'),
    creates pending_nodes and returns info for workflow inclusion.
    
    Accepts optional cached_entities from Guard B to avoid duplicate
    detect_entities() calls. When cached_entities is provided, skips
    the DB roundtrip (saves ~200-800ms on Vercel).
    
    Returns: [{label, pending_id}] for each new or previously-pending org.
    """
    supabase = get_supabase()
    
    if cached_entities is not None:
        entities = cached_entities
    else:
        from core.lib.entity_detector import detect_entities
        entities = detect_entities(text)
    new_orgs = [e for e in entities if e.type == 'organization' and e.is_new]
    if not new_orgs:
        return []
    
    created = []
    for org in new_orgs:
        # Check if already pending
        existing = supabase.table('pending_nodes') \
            .select('id') \
            .ilike('label', org.label) \
            .limit(1) \
            .execute()
        if existing and existing.data:
            created.append({'label': org.label, 'pending_id': existing.data[0]['id']})
            continue

        # Check if already an approved graph node
        existing_gn = supabase.table('graph_nodes') \
            .select('id') \
            .ilike('label', org.label) \
            .eq('type', 'organization') \
            .eq('is_current', True) \
            .limit(1) \
            .execute()
        if existing_gn and existing_gn.data:
            audit_log_sync("executor", "INFO", f"Skipped pending_node for '{org.label}' — already exists as graph node")
            continue

        # (Consolidation: the graph-node check above is the single source of
        # truth — the organizations mirror table is no longer consulted.)

        # Create new pending_node
        res = supabase.table('pending_nodes').insert({
            'label': org.label,
            'type': 'organization',
            'source_text': text[:200],
            'status': 'pending',
            'confidence': 0.8,
        }).execute()
        if res.data:
            created.append({'label': org.label, 'pending_id': res.data[0]['id']})
            audit_log_sync("executor", "INFO",
                f"Real-time org detection: created pending_node {res.data[0]['id']} for '{org.label}'")
    
    return created


# ── Guard 3: Executor data-loss prevention ──

async def _save_fallback_note(text: str, chat_id: int, entity: str = None, source: str = "telegram"):
    """Deterministic data-loss prevention: save text as a memory before reporting failure.

    Called when the executor has zero valid actions — guarantees the user's
    message is NEVER silently dropped. Always saves to memories so the data
    is retrievable even if the pipeline failed to extract actions.

    This is Guard 3 of 3 (see also: classify pre-filter in classify.py,
    planner context injection in planner.py + planner prompt).
    """
    import asyncio
    try:
        from core.llm import get_embedding
        from core.retrieval.pipeline import schedule_index_memory
        from core.lib.time_utils import compute_expires_at
        from datetime import datetime, timezone
        from core.pulse.entity_extractor import extract_and_link_entities

        supabase = get_supabase()
        embedding = (await get_embedding(text)).vector
        embed_valid = bool(embedding and any(embedding))
        mem_res = supabase.table("memories").insert({
            "content": text,
            "memory_type": "note",
            "embedding": embedding if embed_valid else None,
            "embedding_status": "success" if embed_valid else "failed",
            "source": source or "executor_fallback",
            "metadata": {"intent": "NOTE", "entity": entity or "INBOX"},
            "expires_at": compute_expires_at(text, datetime.now(timezone.utc).isoformat())
        }).execute()
        memory_id = mem_res.data[0]['id'] if mem_res.data else None
        if memory_id:
            schedule_index_memory(memory_id, text, "note", "executor_fallback")
            # Fire enrichment in background — don't block the response
            asyncio.ensure_future(extract_and_link_entities(text, str(memory_id), 'memory'))
        audit_log_sync("executor", "INFO",
                       f"Guard 3: Saved fallback note (memory_id={memory_id}) for unprocessable message")
        return True
    except Exception as e:
        audit_log_sync("executor", "WARNING", f"Guard 3: Fallback note save failed: {e}")
        return False



# ── #3: Pre-execution validation ──

def validate_operation(action: Action) -> Optional[str]:
    """Validate that an action can be executed before attempting.

    Returns None if valid, or an error message string if invalid.
    Catches: missing target, nonexistent task, unparseable dates.
    """
    supabase = get_supabase()

    # Operations that require an existing task
    if action.operation in ["close_task", "suppress_instance", "cancel_recurring",
                            "modify_recurring", "reschedule", "update_metadata"]:
        tid = action.target_id
        if not tid:
            return f"{action.operation}: missing target_id"
        try:
            int(str(tid))
        except (ValueError, TypeError):
            return f"{action.operation}: invalid target_id '{tid}'"

        # Check the task actually exists
        try:
            task_res = supabase.table("tasks").select("id, status").eq("id", int(tid)).limit(1).execute()
            if not task_res.data:
                return f"{action.operation}: task {tid} not found"
        except Exception as e:
            return f"{action.operation}: DB check failed for task {tid}: {e}"

    # Operations that require a valid event ID
    if action.operation == "delete_event":
        if not action.target_id:
            return "delete_event: missing target_id"

    # Operations that require a title
    if action.operation in ["create_task", "create_event"]:
        title = action.params.get("title") or action.human_label or ""
        if not title or not title.strip():
            return f"{action.operation}: missing title"

    # Operations that require content
    if action.operation == "create_note":
        content = action.params.get("content") or action.human_label or ""
        if not content or not content.strip():
            return "create_note: missing content"

    return None


# ── #4: Compensation / rollback ──

async def compensate_action(action: Action, supabase):
    """Reverse a completed action.

    Idempotent — safe to call even if the action wasn't actually applied.
    Called during rollback when an action in a batch fails.
    """
    try:
        if action.operation == "close_task":
            # Re-open: only if the task was closed by this operation
            # (safe even if already open — guard prevents invalid transition)
            from core.pulse.tools import update_task_status as _uts
            tid = int(str(action.target_id))
            current = supabase.table("tasks").select("status").eq("id", tid).limit(1).execute()
            if current.data and current.data[0]["status"] == "done":
                _uts(task_id=tid, status="todo")
                audit_log_sync("executor", "INFO", f"Rolled back close_task {tid}")

        elif action.operation == "cancel_recurring":
            # Un-cancel: re-open as todo with original recurrence
            from core.pulse.tools import update_task_status as _uts
            tid = int(str(action.target_id))
            current = supabase.table("tasks").select("status, recurrence").eq("id", tid).limit(1).execute()
            if current.data and current.data[0]["status"] == "cancelled":
                original_rec = current.data[0].get("recurrence")
                _uts(task_id=tid, status="todo", recurrence=original_rec)
                audit_log_sync("executor", "INFO", f"Rolled back cancel_recurring {tid}")

        elif action.operation == "suppress_instance":
            # Can't easily re-create a deleted instance. Audit log is sufficient.
            audit_log_sync("executor", "INFO",
                           f"Cannot undo suppress_instance {action.target_id} — instance already deleted")

        elif action.operation in ("modify_recurring", "reschedule", "update_metadata"):
            # These are inherently idempotent or hard to reverse precisely.
            # Audit log + user notification is the correct approach.
            audit_log_sync("executor", "INFO",
                           f"Rollback for {action.operation} {action.target_id}: logged for manual review")

        elif action.operation == "delete_event":
            # Can't restore a deleted event. Logged.
            audit_log_sync("executor", "INFO",
                           f"Cannot undo delete_event {action.target_id} — event deleted from calendar")

        elif action.operation == "create_task":
            # Delete the created task (soft delete via is_current=False)
            tid = action.params.get("_created_task_id")
            if tid:
                try:
                    supabase.table("tasks").update({"is_current": False}).eq("id", int(tid)).execute()
                    audit_log_sync("executor", "INFO", f"Rolled back create_task {tid}")
                except Exception as e:
                    audit_log_sync("executor", "WARNING", f"Rollback create_task {tid} failed: {e}")

        elif action.operation == "create_note":
            nid = action.params.get("_created_note_id")
            if nid:
                try:
                    supabase.table("memories").update({"is_current": False}).eq("id", int(nid)).execute()
                    audit_log_sync("executor", "INFO", f"Rolled back create_note {nid}")
                except Exception as e:
                    audit_log_sync("executor", "WARNING", f"Rollback create_note {nid} failed: {e}")

        elif action.operation == "create_event":
            eid = action.params.get("_created_event_id")
            if eid:
                try:
                    supabase.table("tasks").update({"is_current": False}).eq("id", int(eid)).execute()
                    audit_log_sync("executor", "INFO", f"Rolled back create_event {eid}")
                except Exception as e:
                    audit_log_sync("executor", "WARNING", f"Rollback create_event {eid} failed: {e}")

    except Exception as e:
        audit_log_sync("executor", "WARNING", f"Compensation failed for {action.operation}: {e}")


# ── Enrichment (fire-and-forget after create operations) ──

def _resolve_entity_from_anchor(entity: str, active_anchor: dict = None) -> str | None:
    """Guard 2c: Resolve entity name from active_anchor, falling back to classifier entity.

    If the thread has an active_anchor with a resolved entity name (e.g., "FC Madras"),
    prefer it over the classifier's routing tag (e.g., "SOLVSTRAT").
    This prevents entity context loss when a note is created in an entity-anchored thread.
    """
    if active_anchor:
        anchor_name = active_anchor.get('name', '')
        if anchor_name and anchor_name.lower() != 'inbox':
            return anchor_name
    return entity


async def execute_planned_actions(
    actions: List[Action], 
    chat_id: int, 
    text: str = "", 
    entity: str = None, 
    source: str = "telegram", 
    sender: str = "user", 
    session_id: str = None,
    intent: str = None,
    suppress_telegram: bool = False,
    active_anchor: dict = None,
):
    """Executes a list of planned actions directly — NO legacy dispatch, NO process_single_dump.

    Features:
      - #3: validate_operation() pre-checks every action before execution
      - #4: compensate_action() rolls back completed actions if a later one fails
      - Creates tasks/notes/events via direct DB inserts (create_task_direct/create_note_direct).
      - Handles closures via existing update_task_status.
      - suppress_telegram: skip Telegram notifications.
      - Guard 2c: active_anchor resolves entity name for correct org routing.
    """
    # ── Guard 3: Zero valid actions → save as note before reporting failure ──
    if not actions:
        resolved_entity = _resolve_entity_from_anchor(entity, active_anchor)
        saved = await _save_fallback_note(text, chat_id, resolved_entity, source)
        if not suppress_telegram:
            if saved:
                await send_telegram(chat_id, "📝 Logged as a note — no specific actions identified.")
            else:
                await send_telegram(chat_id, "I processed the input but couldn't identify any clear actions or notes to extract.")
        return
        
    supabase = get_supabase()

    # Guard 2c: Resolve entity from active_anchor for correct org routing
    resolved_entity = _resolve_entity_from_anchor(entity, active_anchor)
    
    # ── Stage 0: Pre-validate all actions ──
    valid_actions = []
    pre_failures = []
    for action in actions:
        if action.operation == "no_op":
            continue
        err = validate_operation(action)
        if err:
            pre_failures.append(err)
            audit_log_sync("executor", "WARNING", f"Pre-validation blocked: {err}")
        else:
            valid_actions.append(action)
    
    # ── Guard 3 (continued): All actions failed validation → save as note ──
    if not valid_actions:
        saved = await _save_fallback_note(text, chat_id, resolved_entity, source)
        if not suppress_telegram:
            if pre_failures:
                details = "\\n".join(pre_failures)
                await send_telegram(chat_id, f"⚠️ All actions blocked by validation:\\n{details}")
            else:
                if saved:
                    await send_telegram(chat_id, "📝 Logged as a note — no specific actions identified.")
                else:
                    await send_telegram(chat_id, "I processed the input but couldn't identify any clear actions or notes to extract.")
        return
    
    # ── Stage 1: Save dump and memory for closures (zero data loss) ──
    has_closures = any(a.operation in ["close_task", "suppress_instance", "cancel_recurring", "modify_recurring", "reschedule", "update_metadata", "delete_event"] for a in valid_actions)
    if has_closures and text:
        try:
            from core.llm import get_embedding
            from core.retrieval.pipeline import schedule_index_memory
            from core.lib.time_utils import compute_expires_at
            from datetime import datetime, timezone
            from core.pulse.entity_extractor import extract_and_link_entities

            embedding = (await get_embedding(text)).vector
            embed_valid = bool(embedding and any(embedding))
            mem_res = supabase.table("memories").insert({
                "content": text,
                "memory_type": "note",
                "embedding": embedding if embed_valid else None,
                "embedding_status": "success" if embed_valid else "failed",
                "source": "webhook_completion",
                "metadata": {"intent": "COMPLETION", "entity": resolved_entity},
                "expires_at": compute_expires_at(text, datetime.now(timezone.utc).isoformat())
            }).execute()
            memory_id = mem_res.data[0]['id'] if mem_res.data else None
            if memory_id:
                schedule_index_memory(memory_id, text, "note", "webhook_completion")
                await extract_and_link_entities(text, str(memory_id), 'memory')
        except Exception as e:
            audit_log_sync("executor", "WARNING", f"Failed to save completion history: {e}")

    # ── Guard B (Gap B fix): Preserve original message as memory for TASK intents with informational weight ──
    # When the classifier returns TASK for a mixed message (informational + actionable),
    # the task title only captures the action. The rich relationship context
    # (people, orgs, dependencies) is lost. This guard saves the original text
    # as a memory whenever the entity detector finds ≥2 context-bearing entities
    # (person, organization, project) — indicating informational density worth preserving.
    #
    # Also caches the entities result for _detect_new_orgs_and_create_pending to reuse,
    # avoiding a duplicate ~200-800ms detect_entities() call later in the same request.
    _cached_entities = None
    if intent == "TASK" and text and not has_closures:
        try:
            from core.lib.entity_detector import detect_entities
            from core.llm import get_embedding
            from core.retrieval.pipeline import schedule_index_memory
            from core.lib.time_utils import compute_expires_at
            from datetime import datetime, timezone
            from core.pulse.entity_extractor import extract_and_link_entities

            # Gate: count context-bearing entity types only (person, org, project)
            # Emotional states and other types are too noisy for this guard.
            entities = detect_entities(text)
            _cached_entities = entities  # Cache for reuse downstream
            entity_count = sum(1 for e in entities
                              if e.type in ('person', 'organization'))

            if entity_count >= 2:
                embedding = (await get_embedding(text)).vector
                embed_valid = bool(embedding and any(embedding))
                mem_res = supabase.table("memories").insert({
                    "content": text,
                    "memory_type": "note",
                    "embedding": embedding if embed_valid else None,
                    "embedding_status": "success" if embed_valid else "failed",
                    "source": source or "executor",
                    "metadata": {"intent": "TASK_CONTEXT", "entity": resolved_entity},
                    "expires_at": compute_expires_at(text, datetime.now(timezone.utc).isoformat()),
                }).execute()
                memory_id = mem_res.data[0]['id'] if mem_res.data else None
                if memory_id:
                    schedule_index_memory(memory_id, text, "note", "executor")
                    await extract_and_link_entities(text, str(memory_id), 'memory')
                audit_log_sync("executor", "INFO",
                    f"Guard B: Saved original TASK message as memory (memory_id={memory_id}, "
                    f"entities={entity_count}) for {text[:60]}...")
        except Exception as e:
            audit_log_sync("executor", "WARNING", f"Guard B: Failed to save TASK context note: {e}")

    from core.services.google_service import delete_calendar_event
    
    sync_failed = False
    failed_tasks = []
    closed_ids = []
    created_labels = []
    completed_actions = []  # Track for rollback
    
    execute_actions = []
    intercepted_tasks = []
    
    # Intercept tasks extracted from NOTE intents for user approval.
    # TASK intents are NOT intercepted — clear commands like "Remind me to..."
    # execute immediately without asking for confirmation.
    # NOTE intents still get intercepted because they might contain extracted
    # actions the user didn't explicitly intend.
    if intent == "NOTE":
        for action in valid_actions:
            if action.operation in ("create_task", "create_event"):
                intercepted_tasks.append(action)
            else:
                execute_actions.append(action)
    else:
        execute_actions = valid_actions
        
    if intercepted_tasks and session_id:
        signals = []
        for act in intercepted_tasks:
            sig = {
                "type": "deadline" if act.operation == "create_event" else "task_imperative",
                "title": act.params.get("title") or act.human_label or "New Task",
                "reminder_at": act.params.get("time") or act.params.get("reminder_at")
            }
            if act.params.get("organization_name"):
                sig["organization_name"] = act.params["organization_name"]
            signals.append(sig)
            
        # ── Detect new orgs from the full text and add to workflow ──
        new_orgs = []
        if text:
            try:
                new_orgs = await _detect_new_orgs_and_create_pending(text, chat_id, cached_entities=_cached_entities)
            except Exception as e:
                audit_log_sync("executor", "WARNING", f"TASK real-time org detection failed (non-critical): {e}")
        
        w_id = str(uuid.uuid4())
        
        try:
            # Build payload with org info so check_and_resume_workflow can auto-approve them
            payload = {'signals': signals}
            if new_orgs:
                payload['new_orgs'] = new_orgs
                payload['original_text'] = text
            
            # Clear old active workflows for this thread to prevent duplicates
            supabase.table('conversation_workflows').update({'status': 'cancelled'}).eq('thread_id', session_id).eq('status', 'active').execute()
            
            supabase.table('conversation_workflows').insert({
                'id': w_id,
                'chat_id': chat_id,
                'thread_id': session_id,
                'workflow_type': 'batch',
                'status': 'active',
                'awaiting_user_input': True,
                'payload': payload,
            }).execute()
            
            msg_lines = ["📋 I found these items:"]
            item_num = 1
            if new_orgs:
                for org_info in new_orgs:
                    msg_lines.append(f"  {item_num}. 🏢 New client: {org_info['label']}")
                    item_num += 1
            for sig in signals:
                icon = "📅" if sig["type"] == "deadline" else "📝"
                title = sig["title"]
                msg_lines.append(f"  {item_num}. {icon} {title}")
                item_num += 1
            msg_lines.append("\nWant me to handle them?")
            
            if not suppress_telegram:
                await send_telegram(chat_id, "\n".join(msg_lines))
        except Exception as e:
            audit_log_sync("executor", "WARNING", f"Failed to create batch workflow: {e}")
            # Fallback: if we fail to create the workflow, we just execute them
            execute_actions.extend(intercepted_tasks)

    for action in execute_actions:
        if action.operation == "no_op":
            continue
            
        # 1. Handle closures / modifications (require valid target_id)
        if action.operation in ["close_task", "suppress_instance", "cancel_recurring", "modify_recurring", "reschedule", "update_metadata", "delete_event"]:
            if action.operation == "delete_event":
                try:
                    delete_calendar_event(str(action.target_id))
                    closed_ids.append(action.target_id)
                except Exception as e:
                    sync_failed = True
                    failed_tasks.append(f"Event {action.target_id}: {e}")
                continue
                
            if action.operation == "update_metadata":
                try:
                    upd = {}
                    if "new_priority" in action.params:
                        upd["priority"] = action.params["new_priority"]
                    if "new_deadline" in action.params:
                        upd["deadline"] = action.params["new_deadline"]
                    if upd:
                        supabase.table('tasks').update(upd).eq('id', int(action.target_id)).execute()
                        closed_ids.append(action.target_id)
                        # Sync metadata changes to Google Tasks/Calendar
                        try:
                            from core.services.google_service import sync_to_google, get_tasks_service
                            task_meta = supabase.table('tasks').select('title, google_task_id, google_event_id').eq('id', int(action.target_id)).limit(1).execute()
                            if task_meta.data:
                                td = task_meta.data[0]
                                g_id = td.get('google_task_id')
                                if g_id:
                                    sync_to_google(get_tasks_service(), title=td['title'], task_id=g_id,
                                                    priority=upd.get('priority'), due_at=upd.get('deadline'))
                                e_id = td.get('google_event_id')
                                if e_id and upd.get('deadline'):
                                    from core.services.google_service import sync_to_calendar
                                    sync_to_calendar(td['title'], upd['deadline'], event_id=e_id,
                                                      priority='important')
                        except Exception as sync_e:
                            audit_log_sync("executor", "WARNING", f"Google sync for metadata update failed: {sync_e}")
                except Exception as e:
                    sync_failed = True
                    failed_tasks.append(f"Task {action.target_id} metadata: {e}")
                continue

            if action.operation == "modify_recurring":
                # Dedicated modify_recurring handler — does NOT go through update_task_status
                # because update_task_status treats None reminder_at as "delete calendar event."
                # Modifying a recurring task's schedule should update the event, not delete it.
                new_rrule = action.params.get("new_rrule")
                new_reminder = action.params.get("new_reminder_at")
                try:
                    from core.services.google_service import sync_to_calendar
                    task_ref = supabase.table('tasks').select('*').eq('id', int(action.target_id)).limit(1).execute()
                    if task_ref.data:
                        td = task_ref.data[0]
                        e_id = td.get('google_event_id')
                        upd = {"recurrence": new_rrule}
                        if new_reminder:
                            from core.services.google_service import format_rfc3339
                            upd["reminder_at"] = format_rfc3339(new_reminder)
                        supabase.table('tasks').update(upd).eq('id', int(action.target_id)).execute()
                        # Sync to calendar — update existing event, don't delete
                        e_id = sync_to_calendar(td['title'], upd.get('reminder_at') or td.get('reminder_at'),
                                                  event_id=e_id, duration_mins=td.get('duration_mins', 15),
                                                  recurrence=new_rrule)
                        if e_id:
                            supabase.table('tasks').update({'google_event_id': e_id}).eq('id', int(action.target_id)).execute()
                        closed_ids.append(action.target_id)
                    else:
                        sync_failed = True
                        failed_tasks.append(f"Task {action.target_id}: modify_recurring — task not found")
                except Exception as e:
                    sync_failed = True
                    failed_tasks.append(f"Task {action.target_id} modify_recurring: {e}")
                continue
            elif action.operation == "reschedule":
                # Dedicated reschedule handler — bypasses the state machine guard for
                # metadata-only updates. Rescheduling (changing reminder_at) is not a
                # status change — the task stays in its current state (todo, blocked, etc.).
                # The state machine was only designed for status transitions, not metadata.
                new_reminder = action.params.get("new_reminder_at")
                try:
                    if new_reminder:
                        from core.services.google_service import format_rfc3339, sync_to_calendar
                        formatted = format_rfc3339(new_reminder)
                        task_ref = supabase.table('tasks').select('*').eq('id', int(action.target_id)).limit(1).execute()
                        if task_ref.data:
                            td = task_ref.data[0]
                            supabase.table('tasks').update({'reminder_at': formatted}).eq('id', int(action.target_id)).execute()
                            e_id = td.get('google_event_id')
                            if e_id:
                                sync_to_calendar(td['title'], formatted, event_id=e_id,
                                                  duration_mins=td.get('duration_mins', 15))
                            closed_ids.append(action.target_id)
                        else:
                            sync_failed = True
                            failed_tasks.append(f"Task {action.target_id}: reschedule — task not found")
                    else:
                        # No new time provided — just acknowledge
                        closed_ids.append(action.target_id)
                except Exception as e:
                    sync_failed = True
                    failed_tasks.append(f"Task {action.target_id} reschedule: {e}")
                continue
            elif action.operation == "cancel_recurring":
                status_to_set = "cancelled"
                reminder_at = None
                recurrence = None
            else:  # close_task or suppress_instance
                status_to_set = "done"
                reminder_at = None
                recurrence = None

            # State machine guard for task status transitions
            from core.pulse.tools import update_task_status as _uts
            try:
                task_current = supabase.table('tasks').select('status').eq('id', int(action.target_id)).limit(1).execute()
                if task_current.data:
                    if not guard_require_valid_transition("tasks", task_current.data[0]['status'], status_to_set, record_id=int(action.target_id), context="executor_update_status"):
                        sync_failed = True
                        failed_tasks.append(f"Task {action.target_id}: invalid transition '{task_current.data[0]['status']}' → '{status_to_set}'")
                        continue
            except Exception as e:
                audit_log_sync("state_machine", "WARNING", f"Guard fetch failed for task {action.target_id}: {e}")
                
            try:
                result_msg = _uts(
                    task_id=int(action.target_id), 
                    status=status_to_set,
                    reminder_at=reminder_at,
                    recurrence=recurrence
                )
                if "FAIL:" in result_msg:
                    sync_failed = True
                    failed_tasks.append(f"Task {action.target_id}: {result_msg}")
                else:
                    # "INFO:" means already in target state — no-op, don't track
                    if "INFO:" not in result_msg:
                        closed_ids.append(action.target_id)
            except Exception as e:
                sync_failed = True
                failed_tasks.append(f"Task {action.target_id}: {e}")
                
        # 2. Handle creations via direct DB insert — NO process_single_dump
        elif action.operation == "create_task":
            title = action.params.get("title") or action.human_label or text or "New Task"
            reminder_at = action.params.get("reminder_at")
            priority = action.params.get("priority", "important")
            duration = action.params.get("duration_mins", 15)
            recurrence = action.params.get("recurrence")
            direction = action.params.get("direction", "inbound")
            committed_to = action.params.get("committed_to")
            deadline = action.params.get("deadline")

            try:
                from core.pulse.tools import create_task_direct
                # Compute dedup_key from title + org scope to prevent duplicate webhook submissions
                # Case-insensitive + scoped by organization to avoid cross-org collisions
                dedup_org_id = action.params.get("organization_id") or action.organization_id or ""
                dedup_raw = f"{title.lower().strip()}:{dedup_org_id}"
                dedup_key = hashlib.md5(dedup_raw.encode()).hexdigest()[:16] if title else None
                result = await create_task_direct(
                        title=title,
                        dedup_key=dedup_key,
                        organization_id=action.params.get("organization_id") or action.organization_id,
                        organization_name=action.params.get("organization_name"),
                        reminder_at=reminder_at,
                        priority=priority,
                        duration_mins=duration,
                        recurrence=recurrence,
                        deadline=deadline,
                        direction=direction,
                        committed_to=committed_to,
                        # Original message text rides along as the task note —
                        # the app shows it as the "chief of staff" context on
                        # the focal card.
                        notes=text[:500] if text else None,
                    )
                if result.get("action") == "created":
                    created_labels.append(action.human_label or title)
                    # Track for rollback
                    if result.get("task_id"):
                        action.params["_created_task_id"] = result["task_id"]
                        completed_actions.append(action)
                        # Enrichment handled internally by create_task_direct
                elif result.get("action") == "error":
                    sync_failed = True
                    failed_tasks.append(f"Create task '{title}': {result.get('reason', 'unknown')}")
                # "skipped" is silent (dedup)
            except Exception as e:
                sync_failed = True
                failed_tasks.append(f"Create task '{title}': {e}")
                
        elif action.operation == "create_note":
            # Use the original full text for document extractions (PyMuPDF verbatim)
            # rather than the LLM's summarized params.content.
            # Flash Lite (CLASSIFICATION_MODEL) used by the planner for NOTE intents
            # consistently summarizes long content — which destroys meeting notes.
            # When the original text is longer than the planner's output, prefer it.
            planner_content = action.params.get("content") or action.human_label or ""
            content = text if text and len(text) > len(planner_content) else planner_content

            try:
                from core.pulse.tools import create_note_direct
                # Guard 2c: Fall back to resolved_entity if planner didn't provide organization_name
                note_org_name = action.params.get("organization_name") or resolved_entity
                result = await create_note_direct(
                        content=content,
                        source=source,
                        organization_id=action.params.get("organization_id") or action.organization_id,
                        organization_name=note_org_name,
                        session_id=session_id,
                        active_anchor=active_anchor,
                    )
                if result.get("action") == "filed":
                    created_labels.append(action.human_label or "Note created")
                    if result.get("memory_id"):
                        action.params["_created_note_id"] = result["memory_id"]
                        completed_actions.append(action)
                        # Enrichment handled internally by create_note_direct
                elif result.get("action") == "error":
                    sync_failed = True
                    failed_tasks.append(f"Create note: {result.get('reason', 'unknown')}")
            except Exception as e:
                sync_failed = True
                failed_tasks.append(f"Create note: {e}")
                
        elif action.operation == "create_event":
            title = action.params.get("title") or action.human_label or text or "New Event"
            event_time = action.params.get("time") or action.params.get("reminder_at") or ""
            # Compute dedup_key scoped by org to prevent duplicate event creation
            dedup_org_id = action.params.get("organization_id") or action.organization_id or ""
            event_dedup_raw = f"{title.lower().strip()}:{dedup_org_id}:event"
            event_dedup_key = hashlib.md5(event_dedup_raw.encode()).hexdigest()[:16] if title else None
            duration = action.params.get("duration_mins", 30)

            try:
                from core.pulse.tools import create_task_direct
                result = await create_task_direct(
                        title=title,
                        dedup_key=event_dedup_key,
                        reminder_at=event_time,
                        duration_mins=duration,
                        priority="important",
                        organization_name=action.params.get("organization_name"),
                        notes=text[:500] if text else None,
                    )
                if result.get("action") == "created":
                    created_labels.append(action.human_label or title)
                    if result.get("task_id"):
                        action.params["_created_event_id"] = result["task_id"]
                        completed_actions.append(action)
                        # Enrichment handled internally by create_task_direct
                elif result.get("action") == "error":
                    sync_failed = True
                    failed_tasks.append(f"Create event '{title}': {result.get('reason', 'unknown')}")
            except Exception as e:
                sync_failed = True
                failed_tasks.append(f"Create event '{title}': {e}")
                
        elif action.operation == "query_info":
            # query_info is informational only — the original text was already
            # processed through interrogate_brain before planning
            pass
        
        # Track non-create closures for rollback
        if action.operation in ["close_task", "cancel_recurring", "suppress_instance",
                                "modify_recurring", "reschedule", "update_metadata", "delete_event"]:
            completed_actions.append(action)
                
    # ── Rollback: if any action failed, reverse completed actions in reverse order ──
    if sync_failed and completed_actions:
        audit_log_sync("executor", "WARNING",
                       f"{len(completed_actions)} completed actions to roll back after {len(failed_tasks)} failures")
        rolled_back_ids = set()
        for completed in reversed(completed_actions):
            await compensate_action(completed, supabase)
            # Track the human-visible label for the rollback message
            if completed.operation in ("close_task", "cancel_recurring", "suppress_instance"):
                rolled_back_ids.add(str(completed.target_id))
            elif completed.operation in ("create_task", "create_event"):
                created_labels = [lb for lb in created_labels if completed.human_label not in lb]
            elif completed.operation == "create_note":
                created_labels = [lb for lb in created_labels if completed.human_label not in lb]

        # Remove rolled-back IDs from closed_ids so the success message doesn't claim them
        closed_ids = [cid for cid in closed_ids if str(cid) not in rolled_back_ids]

        if not suppress_telegram:
            rollback_msg = f"↩️ Rolled back {len(completed_actions)} previously completed actions." if completed_actions else ""
            error_details = "\\n".join(failed_tasks)
            await send_telegram(chat_id, f"⚠️ **Partial Sync Failure**\\nSome actions failed. {rollback_msg}\
\\nDetails: {error_details}")
    
    # ── Gap 1: After NOTE creation, check for new orgs in real-time ──
    if intent == "NOTE" and created_labels and text:
        try:
            new_orgs = await _detect_new_orgs_and_create_pending(text, chat_id, cached_entities=_cached_entities)
            if new_orgs and not suppress_telegram:
                org_lines = ["🏢 *New organization detected:*"]
                for org_info in new_orgs:
                    org_lines.append(
                        f"  • {org_info['label']} — reply `g{org_info['pending_id']} yes` to approve"
                    )
                await send_telegram(chat_id, "\n".join(org_lines))
        except Exception as e:
            audit_log_sync("executor", "WARNING", f"NOTE real-time org detection failed (non-critical): {e}")
    
    # Send success messages for creations
    if created_labels and not suppress_telegram:
        titles = ", ".join(created_labels)
        await send_telegram(chat_id, f"✅ Logged: {titles}")

    # Send success messages for closures
    if closed_ids and not suppress_telegram:
        active_tasks = []
        try:
            tasks_res = supabase.table("tasks").select("id, title").in_("id", closed_ids).execute()
            active_tasks = tasks_res.data or []
        except Exception:
            pass
        
        labels = [act.human_label for act in actions if act.target_id in closed_ids and act.human_label]
        if labels:
            closed_titles = ", ".join(labels)
        else:
            closed_titles = ", ".join(t["title"] for t in active_tasks if str(t["id"]) in [str(cid) for cid in closed_ids])
            if not closed_titles:
                closed_titles = f"{len(closed_ids)} items"
        await send_telegram(chat_id, f"✅ Closed: {closed_titles}")
        
