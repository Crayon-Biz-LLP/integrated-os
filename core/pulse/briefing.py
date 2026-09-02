"""Pulse Engine — scheduled AI briefing generation.

Extracted and refactored from core/pulse/engine.py. Key improvements:
- No agent loop: single LLM call with structured PulseOutput JSON
- Write-behind pattern: all DB writes happen AFTER briefing generation
- Clean module boundary: only process_pulse() is the public API
"""
import os
import json
import re
import asyncio
from datetime import datetime, timedelta, timezone

from core.llm.constants import SYNTHESIS_MODEL
from core.llm.fallback import generate_content_with_fallback
from core.llm.config import WorkloadProfile
from core.webhook.telegram import send_telegram
from core.services.push_notification import send_push_notification
from core.services.push_notification import send_silent_push
from core.services.push_notification import push_data_content
from core.services.google_service import get_tasks_service
from core.lib.audit_logger import info, warning, error, audit_log_sync
from core.lib.temporal_lineage import detect_drift
from core.lib.redis_cache import acquire_lock, release_lock
from core.decisions import record_decision
from core.pulse.models import PulseOutput
from core.pulse.llm import supabase
from core.services.db import active_user_ids, get_tenant, resolve_telegram_chat_id, tenant_scope
from core.pulse.utils import format_error
from core.pulse.memory import (
    write_outcome_memory,
    detect_temporal_patterns, serendipity_engine, adaptive_briefing_learner,
    retrieve_hindsight_memories, generate_after_action_report,
)
from core.pulse.context import context_provider
from core.pulse.graph import (
    check_task_dependencies,
    analyze_communication_patterns, fetch_graph_task_context,
    get_graph_centrality_context,
)
from core.pulse.pipeline import update_heartbeat, check_pipeline_health
from core.pulse.calendar import sync_completed_tasks_from_google
from core.pulse.practices import (
    detect_practices, build_practice_edges, build_practice_correlations,
    sync_practice_canonical_pages, build_rhythms_section,
)
from core.pulse.resources import batch_enrich_resources
from core.pulse.cluster_discovery import discover_new_clusters
from core.prompts.briefing import build_pulse_briefing_prompt, build_pulse_system_instruction
from core.pulse.calendar import get_calendar_context
from core.lib.pattern_extractor import build_transparency_report

# ──────────────────────────────────────────
# CONSTANTS
# ──────────────────────────────────────────
_BRIEFING_HISTORY_HOURS = 48
_BRIEFING_HISTORY_LIMIT = 3


# ──────────────────────────────────────────
# HELPER FUNCTIONS
# ──────────────────────────────────────────

def _extract_insight(text: str) -> str:
    """Extract the first meaningful narrative paragraph from briefing text.

    Skips the first line (which is usually the briefing mode like
    "Wrap-up.") and returns the next non-empty paragraph.
    """
    if not text:
        return ""
    lines = text.split('\n')
    # Skip first line (briefing mode), find first non-empty narrative paragraph
    for line in lines[1:]:
        stripped = line.strip()
        if stripped and not stripped.startswith(('🚀', '🏠', '⛪', '💡', '🛡️', '-', '•', '🔴', '🟡', '✅', '📅', '⏰', '⚠️', '📝', '⚪', '+', '🔄', '📍', '🆕')):
            # Return first ~80 chars of the narrative paragraph
            return stripped[:120]
    # Fallback: return first line
    return lines[0][:120] if lines else ''


_SECTION_ICONS = '🏡⛪🚀💡✅📅🛡️🔴🟡⚪⏳'
_BARE_SECTION_NAMES = {
    'home', 'work', 'church', 'ideas', 'schedule', 'done',
    'stale', 'stale loops', 'backlog', 'weekend recon', 'urgent', 'important',
}


def _is_section_line(line: str) -> bool:
    """True if a line is a briefing section header or a task bullet.

    An emoji-led line only counts as a section when the word after the icon is
    a known section name (e.g. "🏠 Home") — a narrative opening like "✅ Done 3
    tasks today — …" is NOT treated as a section, so we never double-inject.
    """
    s = line.strip()
    if not s:
        return False
    if s.startswith(('-', '•')):
        return True
    if s[0] in _SECTION_ICONS:
        s = s[1:].strip()
    bare = s.split(':', 1)[0].strip().lower().rstrip('.')
    return bare in _BARE_SECTION_NAMES


def _ensure_briefing_opening(briefing_text: str, briefing_mode: str, opening_line: str) -> str:
    """Guarantee the briefing opens with the headline + a Rhodey opening line.

    The prompt mandates an opening, but a weak LLM run can still drop it —
    this enforces it server-side so a pulse never renders as a bare section list.
    """
    text = briefing_text.strip()
    if not text:
        return text
    lines = [ln.strip() for ln in text.split('\n')]
    # 1) Headline first — prepend if the LLM skipped or mangled it. Compare on
    #    the punctuation-normalised token so a near-match ("Morning check:" vs
    #    "Morning check.") never produces a doubled headline.
    first = lines[0].rstrip('.:') if lines else ''
    first_lower = first.lower()
    mode_key = briefing_mode.strip().rstrip('.:')
    mode_key_lower = mode_key.lower()
    if not first or mode_key_lower not in first_lower:
        lines = [briefing_mode, ''] + lines
    elif first_lower != mode_key_lower:
        idx = lines[0].lower().find(mode_key_lower)
        if idx != -1:
            end_idx = idx + len(mode_key)
            remainder = lines[0][end_idx:].strip('.:- \t*')
            if remainder:
                lines = [briefing_mode, '', remainder] + lines[1:]
    # 2) The line right after the headline must be narrative, not a section.
    anchor = 1
    while anchor < len(lines) and not lines[anchor]:
        anchor += 1
    if anchor < len(lines) and not _is_section_line(lines[anchor]):
        return '\n'.join(lines)
    # 3) No opening — inject one between the headline and the board.
    opening = opening_line.strip() or "Here's where things stand."
    tail = lines[1:]
    while tail and not tail[0]:
        tail.pop(0)
    return '\n'.join([lines[0], '', opening, ''] + tail)


def _resolve_time_intelligence(now: datetime, user_name: str = "") -> dict:
    """Pure briefing-mode selection (extracted for the boundary-clock matrix).

    `now` MUST be timezone-aware in the tenant's zone (Asia/Kolkata default).
    Returns {is_weekend, is_pre_monday, is_monday_morning, briefing_mode,
    system_persona} — the exact values the pulse engine derives from the
    clock, so the whole weekday/weekend/Monday-morning branch is testable
    without running the pipeline (plans/75 #6 boundary-clock matrix).

    Current documented edges (pinned by tests): hour < 12 INCLUDES midnight
    (00:00–11:59 → "Morning check.") — so a 00:30 pulse reads as a morning
    briefing; Friday ≥19:00 and Saturday/Sunday count as weekend; Sunday
    ≥19:00 takes Pre-Monday precedence over the weekend mode.
    """
    day = now.isoweekday()
    hour = now.hour

    is_weekend = (day == 6 or day == 7) or (day == 5 and hour >= 19)
    is_pre_monday = (day == 7 and hour >= 19)
    is_monday_morning = (day == 1 and hour < 11)

    if is_weekend and not is_pre_monday:
        briefing_mode = "Weekend: Chores and Ideas."
        system_persona = "Focus ONLY on Home, Family, and Chores. Explicitly hide Work tasks. Be relaxed."
    elif is_pre_monday:
        briefing_mode = "Pre-Monday: Loading the Week."
        system_persona = "Pre-load Monday. Show Work tasks that start tomorrow. Keep Home visible but deprioritized. Be direct."
    else:
        if hour < 12:
            briefing_mode = "Morning check."
            system_persona = f"Give {user_name} the plain picture of the board — what's on top, what's new, what needs doing. No coaching."
        elif hour < 15 or (hour == 15 and now.minute < 30):
            briefing_mode = "Afternoon check."
            system_persona = f"Keep {user_name} moving on today's priorities. Be direct."
        elif hour < 19:
            if day == 5:
                briefing_mode = "Friday wrap-up."
                system_persona = f"Help {user_name} close the work week: what's done, what can wait. Be dry."
            else:
                briefing_mode = "Wrap-up."
                system_persona = f"Help {user_name} close the day: what's done, what's still open. Be dry."
        else:
            briefing_mode = "Night wind-down."
            system_persona = "Close out the day: what got done, what's still open, what's next. Be calm and brief."

    return {
        "is_weekend": is_weekend,
        "is_pre_monday": is_pre_monday,
        "is_monday_morning": is_monday_morning,
        "briefing_mode": briefing_mode,
        "system_persona": system_persona,
    }


def _map_pulse_mode(briefing_mode: str) -> str:
    """Map a briefing_mode string → the clean pulse_mode the app renders.

    Pure (extracted so every branch is pinned by tests). Unknown modes fall
    back to 'check_in'.
    """
    mode_lower = (briefing_mode or "").lower()
    if 'morning' in mode_lower:
        return 'morning'
    if 'afternoon' in mode_lower:
        return 'afternoon'
    if 'wrap' in mode_lower or 'closing' in mode_lower or 'sign off' in mode_lower:
        return 'closing_loop'
    if 'weekend' in mode_lower or 'chores' in mode_lower:
        return 'weekend'
    if 'pre-monday' in mode_lower or 'loading' in mode_lower:
        return 'pre_monday'
    if 'wind' in mode_lower or 'intel' in mode_lower or 'vaulted' in mode_lower:
        return 'intel'
    return 'check_in'


def _store_briefing_to_history(briefing_text: str):
    """Store a condensed summary of this briefing in memories for future context."""
    if not briefing_text:
        return
    try:
        summary = briefing_text.strip()[:200].replace('\n', ' ').strip()
        supabase.table('memories').insert({
            'content': f"[BRIEFING] {summary}",
            'memory_type': 'pulse_briefing',
            'source': 'pulse_engine',
            'expires_at': (datetime.now(timezone.utc) + timedelta(hours=_BRIEFING_HISTORY_HOURS * 2)).isoformat()
        }).execute()
    except Exception as e:
        audit_log_sync("pulse", "WARNING", f"Failed to store briefing history: {e}")


def _get_recent_briefings_context() -> str:
    """Return string of what was already briefed, so the AI avoids repetition."""
    try:
        rows = supabase.table('memories') \
            .select('content, created_at') \
            .eq('memory_type', 'pulse_briefing') \
            .eq('is_current', True) \
            .gte('created_at', (datetime.now(timezone.utc) - timedelta(hours=_BRIEFING_HISTORY_HOURS)).isoformat()) \
            .order('created_at', desc=True) \
            .limit(_BRIEFING_HISTORY_LIMIT) \
            .execute()
        if not rows.data:
            return ""
        parts = []
        for r in rows.data:
            content = r.get('content', '')
            created = r.get('created_at', '')[:16] if r.get('created_at') else ''
            if content:
                cleaned = content.replace('[BRIEFING]', '').strip()
                parts.append(f"- [{created}] {cleaned}")
        if not parts:
            return ""
        return f"PREVIOUSLY BRIEFED (last {len(parts)} briefings — do NOT repeat this content verbatim):\n" + "\n".join(parts)
    except Exception:
        return ""


async def _wrap_calendar_context(target_date: datetime) -> str:
    """Safe wrapper around get_calendar_context() for parallel gather.

    `target_date` is the tenant's resolved `now` — get_calendar_context
    requires it to scope the day's events (missing it raised a TypeError on
    every pulse, silently dropping calendar context).
    """
    try:
        # get_calendar_context is SYNC (returns a formatted string) —
        # awaiting it raised "object str can't be used in 'await' expression"
        # on every pulse, silently dropping calendar context from briefings.
        return get_calendar_context(target_date)
    except Exception as e:
        audit_log_sync("pulse", "WARNING", f"Calendar context fetch failed: {e}")
        return ""


def _auto_expire_recurring_tasks():
    """Parse RRULE UNTIL/COUNT on active recurring tasks. If recurrence has ended,
    mark the task as auto-expired so it stops appearing in briefings."""
    try:
        rows = supabase.table('tasks') \
            .select('id, title, recurrence, reminder_at, created_at') \
            .eq('status', 'todo') \
            .eq('is_current', True) \
            .not_.is_('recurrence', None) \
            .execute()
        if not rows.data:
            return

        now = datetime.now(timezone.utc)
        expired = []
        for task in rows.data:
            rrule = (task.get('recurrence') or '').strip()

            # Attempt 1: UNTIL=YYYYMMDDTHHMMSSZ (UTC datetime)
            m = re.search(r'UNTIL=(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})Z', rrule)
            if m:
                until_dt = datetime(
                    int(m.group(1)), int(m.group(2)), int(m.group(3)),
                    int(m.group(4)), int(m.group(5)), int(m.group(6)),
                    tzinfo=timezone.utc
                )
                if now >= until_dt:
                    expired.append(task['id'])
                continue

            # Attempt 2: UNTIL=YYYYMMDD (date only)
            m = re.search(r'UNTIL=(\d{4})(\d{2})(\d{2})(?!T)', rrule)
            if m:
                until_dt = datetime(
                    int(m.group(1)), int(m.group(2)), int(m.group(3)),
                    23, 59, 59, tzinfo=timezone.utc
                )
                if now >= until_dt:
                    expired.append(task['id'])
                continue

            # Attempt 3: COUNT-based recurrence
            m_count = re.search(r'COUNT=(\d+)', rrule)
            m_freq = re.search(r'FREQ=(\w+)', rrule)
            if m_count and m_freq:
                count = int(m_count.group(1))
                freq = m_freq.group(1).upper()
                start_str = task.get('reminder_at') or task.get('created_at')
                if not start_str:
                    continue
                try:
                    start_dt = datetime.fromisoformat(start_str.replace('Z', '+00:00'))
                except Exception:
                    continue
                if freq == 'DAILY':
                    last_dt = start_dt + timedelta(days=count - 1)
                elif freq == 'WEEKLY':
                    last_dt = start_dt + timedelta(weeks=count - 1)
                elif freq == 'MONTHLY':
                    last_dt = start_dt + timedelta(days=30 * (count - 1))
                elif freq == 'YEARLY':
                    last_dt = start_dt + timedelta(days=365 * (count - 1))
                else:
                    continue
                if now >= last_dt:
                    expired.append(task['id'])
                continue

        if expired:
            from core.lib.state_machines import guard_require_valid_transition
            for tid in expired:
                if not guard_require_valid_transition("tasks", "todo", "cancelled", record_id=tid, context="auto_expire_recurring_tasks"):
                    continue
                supabase.table('tasks').update({
                    'status': 'cancelled',
                    'completed_at': now.isoformat()
                }).eq('id', tid).execute()
                try:
                    record_decision(
                        decision_type="task_auto_expiry",
                        title=f"Auto-expired recurring task #{tid}",
                        context="Recurrence (RRULE) has ended — no future instances remain.",
                        entity_type="task",
                        entity_id=str(tid),
                        confidence=1.0,
                        source="pulse_engine",
                        auto_decided=True,
                    )
                except Exception as dec_err:
                    audit_log_sync("pulse", "WARNING", f"Failed to record auto-expiry decision: {dec_err}")
            # Invalidate tasks cache so expired tasks disappear from next briefing
            try:
                context_provider.caches['tasks'].invalidate()
                context_provider.caches['recent_tasks'].invalidate()
            except Exception:
                pass
            audit_log_sync("pulse", "INFO", f"⏰ Auto-expired {len(expired)} recurring tasks (recurrence ended).")
    except Exception as e:
        audit_log_sync("pulse", "WARNING", f"Auto-expiry check failed: {e}")


# ──────────────────────────────────────────
# MAIN PULSE ENGINE
# ──────────────────────────────────────────

def _user_briefing_due(uid: str) -> bool:
    """M9.7 gate: is THIS heartbeat one of this tenant's briefing slots?

    Runs INSIDE the tenant scope. Resolves their `briefing_schedule`
    (core_config row → balanced default) + timezone and checks the pure
    `briefing_due_now()`. Fail-closed: any error or malformed schedule means
    NO briefing — never a crash, never an off-slot send, never another
    tenant's row.
    """
    try:
        from core.services.briefing_schedule import (
            resolve_briefing_schedule,
            briefing_due_now,
        )
        from core.lib.time_utils import get_user_timezone
        schedule = resolve_briefing_schedule(uid)
        now = datetime.now(get_user_timezone(uid))
        return briefing_due_now(schedule, now)
    except Exception as e:
        audit_log_sync("briefing", "WARNING", f"Briefing gate failed for {uid} (fail-closed, skipped): {e}")
        return False


def _recently_briefed(uid: str, within_minutes: int = 25) -> bool:
    """True if a completed main pulse run exists for this tenant recently.

    Guards a slot against double-firing: the 30-min heartbeat with a 15-min
    window would double-hit a slot edited to an off-grid time (e.g. 07:15 is
    inside the window of both the 07:00 and 07:30 heartbeats), and a manual
    dispatch right before a scheduled slot would otherwise run twice. Any
    slot ≥30 min apart is never suppressed (25 < 30). Owner-scoped via the
    tenant facade — never another tenant's run.
    """
    try:
        cutoff = (datetime.now(timezone.utc) - timedelta(minutes=within_minutes)).isoformat()
        res = (
            supabase.table("pulse_runs")
            .select("id")
            .eq("pulse_type", "main")
            .eq("status", "completed")
            .gte("completed_at", cutoff)
            .limit(1)
            .execute()
        )
        return bool(res.data)
    except Exception:
        return False


async def process_pulse(auth_secret: str = None, request_id: str = None, trigger: str = "api"):
    """(M6 fan-out + M9.7 gate) Iterate all active users, one tenant-scoped
    briefing each. Cron traffic carries no API key; each active user gets
    their own briefing under tenant_scope(). A per-tenant failure is
    isolated and reported without aborting the other tenants.

    M9.7: the GHA heartbeat fires every 30 minutes for EVERY tenant. Only
    scheduled runs (trigger="cron") are gated by the per-tenant
    briefing_schedule — a tenant whose slot is not due is skipped cheaply
    (no LLM, no push). Manual triggers (workflow_dispatch → "manual",
    /api/pulse → "api") bypass the gate and force a briefing for everyone.

    Legacy (pre-db/78, or no active users): runs once unscoped, exactly as
    the pre-M4 briefing did. The top-level "briefing" key keeps the legacy
    /api/pulse response shape; per-tenant details are in "results".
    """
    uids = active_user_ids()
    if not uids:
        return await _process_pulse_impl(auth_secret, request_id, trigger)
    gated = trigger == "cron"
    results = []
    any_briefed = False
    for uid in uids:
        try:
            r = await process_pulse_for_tenant(uid, auth_secret, request_id, trigger)
            if r.get("briefing"):
                any_briefed = True
            results.append(r)
        except Exception as e:
            audit_log_sync("briefing", "ERROR", f"Briefing failed for tenant {uid}: {e}")
            results.append({"tenant": uid, "error": str(e)})
    # Heartbeat freshness: when the heartbeat wakes and NO tenant is due, the
    # engine still ran — keep the channel tenant's pulse_last_success fresh so
    # the health check reports a healthy pipeline instead of a stale one.
    if gated and not any_briefed:
        try:
            from core.services.db import channel_tenant_scope
            with channel_tenant_scope():
                await update_heartbeat()
        except Exception:
            pass
    return {
        "success": True,
        "briefing": next((r.get("briefing") for r in results if r.get("briefing")), None),
        "tenants": len(uids),
        "briefed": any_briefed,
        "results": results,
    }


def due_tenant_ids(trigger: str = "cron") -> list[str]:
    """Active users whose briefing is due RIGHT NOW (cheap gate, no LLM).

    Used by the /api/pulse-cron fan-out (Option B) to decide which tenants
    get a dedicated brief_tenant Modal worker. Mirrors the per-tenant gate
    inside process_pulse's loop so behavior is identical whether a tenant is
    briefed inline or in its own container. Fail-closed: any gate error
    means NOT due — never a surprise briefing, never another tenant's row.
    """
    due = []
    for uid in active_user_ids():
        try:
            with tenant_scope(uid):
                if trigger != "cron":
                    due.append(uid)
                    continue
                if _user_briefing_due(uid) and not _recently_briefed(uid):
                    due.append(uid)
        except Exception as e:
            audit_log_sync("briefing", "WARNING", f"Due-check failed for tenant {uid}: {e}")
    return due


async def process_pulse_for_tenant(
    uid: str, auth_secret: str = None, request_id: str = None, trigger: str = "cron"
) -> dict:
    """Brief ONE tenant (the Option B worker body, and the inline-loop unit).

    Owner-scoped. Applies the same schedule gate as the loop: a tenant whose
    slot is not due is skipped cheaply (no LLM, no push). Reaps stale
    'running' runs before starting, so timeout-killed rows can never block
    or mislead. Returns the impl's dict (success/briefing or error).
    """
    with tenant_scope(uid):
        gated = trigger == "cron"
        if gated and (not _user_briefing_due(uid) or _recently_briefed(uid)):
            return {"tenant": uid, "skipped": "not_briefing_time"}
        # ── Reap stale 'running' runs (timeout-killed rows never complete) ──
        try:
            from core.pulse.run_logger import reap_stuck_pulse_runs
            reap_stuck_pulse_runs(supabase)
        except Exception as e:
            warning("pulse", f"Stuck-run reap skipped: {e}", format_error(e))
        return await _process_pulse_impl(auth_secret, request_id, trigger)


async def _process_pulse_impl(auth_secret: str = None, request_id: str = None, trigger: str = "api"):
    """Generate and send an AI briefing.

    Single pipeline: read state → build context → single LLM call (no agent loop)
    → parse structured output → send to Telegram → store history → run maintenance.
    """
    from core.lib.audit_logger import set_trace_id
    set_trace_id(request_id)

    from core.pulse.run_logger import create_pulse_run, complete_pulse_run

    pulse_secret = os.getenv("PULSE_SECRET")
    if pulse_secret and auth_secret != pulse_secret:
        return {"error": "Unauthorized.", "status": 401}

    # Per-tenant lock (Option B): parallel brief_tenant workers must not
    # serialize on a global key — each tenant gets its own lock so a slow
    # tenant can't block the others. TTL 1200s comfortably covers the 900s
    # per-tenant Modal timeout (lock must outlive the run it guards).
    # Legacy unscoped path (no active users) keeps the global key.
    _lock_uid = get_tenant()
    lock_key = f"pulse_concurrency_lock:{_lock_uid}" if _lock_uid else "pulse_concurrency_lock"
    if not acquire_lock(lock_key, ttl=1200):
        return {"success": False, "message": "Pulse or Decision Pulse already running. Concurrency lock active."}

    run_id = None
    try:
        # ── Idempotency Check ──
        if request_id:
            existing = supabase.table('raw_dumps') \
                .select('id, status') \
                .eq('metadata->>request_id', request_id) \
                .limit(1) \
                .execute()
            if existing.data:
                info("pulse", f"Idempotency: request_id {request_id} already processed")
                release_lock(lock_key)
                return {"success": True, "idempotent": True, "message": "Already processed"}

        # ── Zombie recovery ──
        try:
            ten_mins_ago = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
            supabase.table('raw_dumps') \
                .update({"status": "staged"}) \
                .eq('status', 'processing') \
                .lt('created_at', ten_mins_ago) \
                .execute()
        except Exception as e:
            error("pulse", f"Zombie Recovery skipped: {e}", format_error(e))

        # ── Auth (re-check) ──
        pulse_secret = os.getenv("PULSE_SECRET")
        if pulse_secret and auth_secret != pulse_secret:
            release_lock(lock_key)
            return {"error": "Unauthorized manual trigger.", "status": 401}
        if not pulse_secret:
            warning("pulse", "PULSE_SECRET not set. Auth check bypassed.")

        # ── Pulse run logging ──
        run_id = await create_pulse_run(supabase, "main", trigger)

        # ── Google→Supabase sync ──
        tasks_service = get_tasks_service()
        try:
            completed_from_google = await asyncio.to_thread(sync_completed_tasks_from_google, supabase, tasks_service)
            for title, proj_name in (completed_from_google or []):
                await write_outcome_memory(title)
        except Exception as e:
            error("pulse", f"Google tasks sync failed, continuing pulse: {e}", format_error(e))

        # ── Heartbeat & health ──
        try:
            await update_heartbeat()
            health_report = await check_pipeline_health()
            audit_log_sync("pulse", "INFO", str(health_report))
        except Exception as e:
            warning("pulse", f"Heartbeat/Health check failed: {e}", format_error(e))

        # ── Batch enrichment ──
        try:
            batch_enrich_results = await batch_enrich_resources()
        except Exception as e:
            error("pulse", f"Batch enrichment failed, continuing pulse: {e}", format_error(e))
            batch_enrich_results = []

        # ── Cluster discovery ──
        try:
            await discover_new_clusters()
        except Exception as e:
            error("pulse", f"Cluster discovery failed, continuing pulse: {e}", format_error(e))

        # ── Fetch tasks (exclude snoozed — focal "Not now" deferral) ──
        active_tasks_q = supabase.table('tasks').select(
            'id, title, status, organization_id, priority, created_at, reminder_at, google_event_id, direction, committed_to'
        ).eq('is_current', True).not_.in_('status', ['done', 'cancelled'])
        try:
            active_tasks_q = active_tasks_q.or_('snoozed_until.is.null,snoozed_until.lt.now')
        except Exception:
            pass  # Column not yet migrated — fall back to unfiltered
        active_tasks_res = active_tasks_q.execute()
        active_tasks = active_tasks_res.data or []

        # ── Silence if no tasks ──
        if not active_tasks:
            await complete_pulse_run(supabase, run_id, status="completed",
                dumps_processed=0, tasks_created=0,
                metadata={"reason": "nothing_to_process"})
            release_lock(lock_key)
            return {"success": True, "message": "Nothing to process, nothing to nag about. Silence is golden."}

        audit_log_sync("pulse", "INFO", f"🚀 PULSE START: Processing {len(active_tasks)} active tasks.")

        # ═══════════════════════════════════════
        # CONTEXT BUILDING (identical to original)
        # ═══════════════════════════════════════

        # Fetch core_config
        core_res = supabase.table('core_config').select('key, content').execute()
        core = core_res.data or []

        # ── Time & Day Intelligence (CPU-only, no IO — compute before parallel phase 1) ──
        from core.lib.time_utils import get_user_timezone
        from core.services.user_settings import resolve_user_name
        ist_offset = get_user_timezone()  # M2: per-tenant timezone (settings → env → IST)
        now = datetime.now(ist_offset)
        day = now.isoweekday()
        hour = now.hour
        user_name = resolve_user_name()  # M2: per-tenant display name

        # Pure branch — extracted so the boundary-clock matrix pins every
        # mode without running the whole pipeline (plans/75 #6).
        ti = _resolve_time_intelligence(now, user_name)
        is_weekend = ti["is_weekend"]
        is_pre_monday = ti["is_pre_monday"]
        is_monday_morning = ti["is_monday_morning"]
        briefing_mode = ti["briefing_mode"]
        system_persona = ti["system_persona"]

        # M18c: the persona card is L3 KNOWLEDGE — it is fetched through the
        # ContextProvider in the parallel assembly below (same pipeline as
        # memories/tasks/people), never appended as a presentation-layer
        # string. Fail-closed: no card => empty block => byte-identical to
        # pre-M18. The card's prose was verified by the G1-G4 grounding
        # gates at write time, so it never injects invented facts.
        is_overloaded = len(active_tasks) > 15

        # ═══════════════════════════════════════
        # CONTEXT ASSEMBLY — PARALLEL PHASE 1
        # Independent DB/LLM queries that can run simultaneously
        # ═══════════════════════════════════════
        (
            people,
            orgs_list,
            dependency_context,
            temporal_context,
            centrality_context,
            calendar_context,
            compressed_tasks_final,
            persona_context,
        ) = await asyncio.gather(
            context_provider.get_people(),
            context_provider.get_organizations(),
            check_task_dependencies(active_tasks),
            detect_temporal_patterns(),
            get_graph_centrality_context(),
            _wrap_calendar_context(now),
            context_provider.hydrate_tasks_context(f"Briefing for {briefing_mode}"),
            context_provider.hydrate_persona_context(),
        )
        if persona_context:
            system_persona += persona_context
        org_map = {o['id']: o['name'] for o in orgs_list}
        # O(1) set for work/life split — built from graph node's is_personal flag
        personal_org_ids = {o['id'] for o in orgs_list if o.get('is_personal', False)}
        audit_log_sync("pulse", "INFO", f"📦 Phase 1 context fetched in parallel ({len(people)} people, {len(orgs_list)} orgs)")

        # ── Priority decay ──
        try:
            seven_days_ago_iso = (now - timedelta(days=7)).isoformat()
            stale_urgent = supabase.table('tasks') \
                .select('id, title, created_at') \
                .eq('is_current', True) \
                .eq('status', 'todo') \
                .eq('priority', 'urgent') \
                .lt('created_at', seven_days_ago_iso) \
                .execute()
            for st in (stale_urgent.data or []):
                supabase.table('tasks').update({'priority': 'high'}).eq('id', st['id']).execute()
                try:
                    record_decision(
                        decision_type="priority_decay",
                        title=f"Priority decay: '{st['title']}' urgent → high",
                        context="Task was 'urgent' for more than 7 days without progress. Auto-downgraded.",
                        entity_type="task",
                        entity_id=str(st['id']),
                        confidence=1.0,
                        source="pulse_engine",
                        auto_decided=True,
                    )
                except Exception as dec_err:
                    audit_log_sync("pulse", "WARNING", f"Failed to record priority decay decision: {dec_err}")
        except Exception as dec_err:
            audit_log_sync("pulse", "WARNING", f"Priority decay check failed: {dec_err}")

        # ── Strategic task filtering ──
        filtered_tasks = []
        horizon_cutoff = now + timedelta(days=2)

        for t in active_tasks:
            raw_reminder = t.get('reminder_at') or t.get('deadline')
            if raw_reminder:
                try:
                    clean_reminder = str(raw_reminder).replace(' ', 'T').replace('Z', '+00:00')
                    if len(clean_reminder) == 10:
                        clean_reminder += "T00:00:00+00:00"
                    task_date = datetime.fromisoformat(clean_reminder)
                    if task_date.tzinfo is None:
                        task_date = task_date.replace(tzinfo=ist_offset)
                    if task_date > horizon_cutoff:
                        continue
                except Exception as e:
                    audit_log_sync("pulse", "WARNING", f"Horizon guard date parse failed for '{t.get('title')}': {e}")

            o_id = t.get('organization_id')
            o_name = org_map.get(o_id, 'INBOX')

            # Work/life split: O(1) set lookup on graph node's is_personal flag.
            # Falls back to name-substring match for legacy nodes without metadata.
            is_personal = o_id in personal_org_ids if o_id else False
            if not is_personal:
                # Fallback: check name against personal_org_names from user_settings
                from core.services.user_settings import resolve_personal_orgs
                personal_orgs = resolve_personal_orgs()
                is_personal = any(po.lower() in o_name.lower() for po in personal_orgs)

            if is_weekend:
                # Weekend mode: only personal tasks pass through
                if not is_personal:
                    continue
            elif hour < 19:
                if not is_personal or o_name == 'INBOX':
                    pass
                else:
                    continue
            else:
                if is_personal:
                    pass
                else:
                    continue

            # Urgent priority tasks still MUST pass the org filter above
            # If they fail the org check, they don't get included — even if urgent
            filtered_tasks.append(t)

        # ── Pending channel suggestions (email/whatsapp/teams/call) ──
        # Actionable, undecided messages the Inbox is holding. The briefing may
        # promote ONE as the focal card (a "quick task suggestion" with an
        # Approve action) when it genuinely beats the active tasks — judgment
        # over volume: the full queue stays in Quick Confirmation.
        pending_suggestions = []
        try:
            from core.services.inbox_feed import fetch_pending_channel_messages
            pending_suggestions = await asyncio.to_thread(
                fetch_pending_channel_messages, supabase, 8
            )
        except Exception as _sug_err:
            audit_log_sync("pulse", "WARNING",
                           f"Failed to fetch pending channel suggestions: {_sug_err}")

        # ── Build universal task map (ID matching only — built from filtered tasks) ──
        # Suggestion lines are truncated (~80 chars) so a long message body
        # can't eat the 4000-char map budget and push real task IDs out.
        _suggestion_lines = []
        for _s in pending_suggestions:
            _sug_text = " ".join((_s.get('content') or 'Untitled').split())[:80]
            _suggestion_lines.append(
                f"[ID:{_s['id']}] {_sug_text} "
                f"(PENDING {str(_s.get('source') or 'channel').upper()} SUGGESTION — "
                "approve to create task)"
            )
        def _task_line(t: dict) -> str:
            # Tag committed ("I'll do it") tasks so the prompt can keep them
            # in briefing sections but never re-promote them as the focal
            # item — the user already took them on.
            if t.get('status') == 'in_progress':
                return f"[ID:{t['id']}] [IN PROGRESS — user committed to it] {t['title']}"
            return f"[ID:{t['id']}] {t['title']}"

        universal_task_map = (
            " | ".join(_task_line(t) for t in filtered_tasks)
            + (" || " + " | ".join(_suggestion_lines) if _suggestion_lines else "")
        )[:4000]

        # ── Context compression ──
        compressed_tasks_list = []
        for t in filtered_tasks:
            o_id = t.get('organization_id')
            o_name = org_map.get(o_id, 'INBOX')
            loc = o_name
            dir_str = ""
            if t.get('direction') == 'waiting_on':
                dir_str = f" [WAITING ON: {t.get('committed_to', 'someone')}]"
            elif t.get('direction') == 'outbound':
                dir_str = f" [OWED TO: {t.get('committed_to', 'someone')}]"
            compressed_tasks_list.append(f"[{loc}] {t.get('title')} ({t.get('priority')}){dir_str} [ID:{t.get('id')}]")

        # ── Season expiry ──
        season_row = next((c for c in core if c.get('key') == 'current_season'), None)
        season_config = season_row.get('content') if season_row else ''
        expiry_match = re.search(r'\[EXPIRY:\s*(\d{4}-\d{2}-\d{2})\]', season_config)
        system_context = "OPERATIONAL"
        if expiry_match:
            expiry_date = datetime.strptime(expiry_match.group(1), "%Y-%m-%d").replace(tzinfo=timezone.utc)
            if now > expiry_date:
                system_context = "CRITICAL: Season Context EXPIRED."

        # ── Nag logic ──
        overdue_tasks = []
        for t in filtered_tasks:
            try:
                raw_created = t.get('created_at')
                if raw_created:
                    created_date = datetime.fromisoformat(raw_created.replace("Z", "+00:00"))
                    hours_old = (now - created_date).total_seconds() / 3600
                    if t.get('priority') == 'urgent' and hours_old > 48:
                        overdue_tasks.append(t.get('title'))
            except Exception as e:
                audit_log_sync("pulse", "WARNING", f"Nag logic date parse failed for '{t.get('title')}': {e}")

        sevendays_ago = (now - timedelta(days=7)).isoformat()
        stale_tasks = [t for t in filtered_tasks if t.get('status') == 'todo' and t.get('created_at', '') < sevendays_ago and t.get('title') not in overdue_tasks]
        stale_tasks = sorted(stale_tasks, key=lambda t: t.get('created_at', ''))[:5]

        stale_context = None
        if stale_tasks:
            stale_lines = []
            for t in stale_tasks:
                try:
                    created = datetime.fromisoformat(t.get('created_at', '').replace('Z', '+00:00'))
                    days_old = (now - created).days
                    stale_lines.append(f"- {t.get('title', '')} (stale {days_old}d)")
                except Exception as e:
                    audit_log_sync("pulse", "WARNING", f"Stale task age calc failed for '{t.get('title')}': {e}")
            stale_context = "\n".join(stale_lines)

        drift_alerts = []
        for org in (orgs_list or []):
            org_name = org.get('name', '')
            if not org_name:
                continue
            try:
                drift = detect_drift(org_name, hours_window=48)
                if drift and drift.get('update_count', 0) >= 3:
                    drift_alerts.append(f"⚠️ DRIFT ALERT: '{org_name}' changed {drift['update_count']} times in 48h. Bottleneck?")
            except Exception as e:
                audit_log_sync("pulse", "WARNING", f"Drift detection failed for {org_name}: {e}")

        drift_context = "\n".join(drift_alerts) if drift_alerts else "None"

        # ── Hindsight ──
        thirty_days_ago = (now - timedelta(days=30)).isoformat()
        hindsight_context = "None"
        task_inputs = [t.get('title', '') for t in filtered_tasks if t.get('title')]
        all_entity_terms = [p['name'] for p in people]

        # ── Resource & memory queries (parallel phase 2) ──
        recent_lib_res = supabase.table('resources') \
            .select('id, url, category, title, summary, strategic_note, created_at') \
            .gt('created_at', thirty_days_ago) \
            .eq('is_current', True) \
            .order('created_at', desc=True) \
            .limit(50) \
            .execute()
        recent_lib_data = recent_lib_res.data or []

        mem_query = " | ".join([t.get('title', '') for t in filtered_tasks[:5]])

        # ── Weekly patterns (no IO — reads from already-fetched core_config) ──
        weekly_patterns_str = ""
        try:
            wp_row = next((c for c in core if c.get('key') == 'weekly_patterns'), None)
            if wp_row and wp_row.get('content'):
                weekly_patterns_str = wp_row['content']
        except Exception:
            pass

        # ═══════════════════════════════════════
        # CONTEXT ASSEMBLY — PARALLEL PHASE 2
        # Depends on Phase 1's people + projects
        # All independent of each other
        # ═══════════════════════════════════════
        phase2_tasks = []
        
        # Graph task context
        if people and filtered_tasks:
            phase2_tasks.append(fetch_graph_task_context(people, filtered_tasks))
        else:
            phase2_tasks.append(asyncio.sleep(0, result=""))
        
        # Hindsight memories
        phase2_tasks.append(retrieve_hindsight_memories(task_inputs, filtered_tasks, entity_terms=all_entity_terms))
        
        # Cross-referenced memories
        phase2_tasks.append(context_provider.get_cross_referenced_context(mem_query, task_inputs, people, orgs=orgs_list, match_count=5))
        
        # Social graph / communication patterns
        phase2_tasks.append(analyze_communication_patterns(people))
        
        # Serendipity engine
        phase2_tasks.append(serendipity_engine(filtered_tasks, people, recent_lib_data, pattern_context=weekly_patterns_str or None))
        
        # Master page / canonical synthesis
        relevant_org_names = list(set([
            o.get('name', '').strip() for o in orgs_list
            if o.get('name', '').strip()
        ]))
        phase2_tasks.append(context_provider.get_master_page_context(
            entity_names=relevant_org_names[:5],
            match_count=3
        ))
        
        # Adaptive briefing learner (only on Sundays)
        if now.weekday() == 6:
            phase2_tasks.append(adaptive_briefing_learner())
        else:
            phase2_tasks.append(asyncio.sleep(0, result="None"))

        phase2_results = await asyncio.gather(*phase2_tasks, return_exceptions=True)

        # Unpack phase 2 results with error handling
        _graph_task_ctx_result = phase2_results[0]
        if isinstance(_graph_task_ctx_result, Exception):
            audit_log_sync("pulse", "WARNING", f"Graph task context failed: {_graph_task_ctx_result}")
            graph_task_context = ""
        else:
            graph_task_context = _graph_task_ctx_result

        _hindsight_result = phase2_results[1]
        if isinstance(_hindsight_result, Exception):
            audit_log_sync("pulse", "WARNING", f"Hindsight retrieval failed: {_hindsight_result}")
            hindsight_memories, hindsight_timestamp = [], None
        else:
            hindsight_memories, hindsight_timestamp = _hindsight_result

        _cross_ref_result = phase2_results[2]
        if isinstance(_cross_ref_result, Exception):
            audit_log_sync("pulse", "WARNING", f"Cross-referenced context failed: {_cross_ref_result}")
            recent_memories_context = ""
        else:
            recent_memories_context = _cross_ref_result

        _social_result = phase2_results[3]
        if isinstance(_social_result, Exception):
            audit_log_sync("pulse", "WARNING", f"Social graph analysis failed: {_social_result}")
            social_graph_context = ""
        else:
            social_graph_context = _social_result

        _serendipity_result = phase2_results[4]
        if isinstance(_serendipity_result, Exception):
            audit_log_sync("pulse", "WARNING", f"Serendipity engine failed: {_serendipity_result}")
            serendipity_context = ""
        else:
            serendipity_context = _serendipity_result

        _master_page_result = phase2_results[5]
        if isinstance(_master_page_result, Exception):
            audit_log_sync("pulse", "WARNING", f"Master page fetch failed: {_master_page_result}")
            master_page_context = ""
        else:
            master_page_context = _master_page_result

        _adaptive_result = phase2_results[6]
        if isinstance(_adaptive_result, Exception):
            audit_log_sync("pulse", "WARNING", f"Adaptive learner failed: {_adaptive_result}")
            adaptive_context = "None"
        else:
            adaptive_context = _adaptive_result

        audit_log_sync("pulse", "INFO", "📦 Phase 2 context fetched in parallel")

        # ── Process hindsight results ──
        memory_lines = []
        memory_lines.extend(hindsight_memories)
        hindsight_block = "\n".join(memory_lines)
        if hindsight_memories:
            hindsight_context = hindsight_block

        is_hindsight_stale = False
        hindsight_empty = False
        if hindsight_timestamp:
            last_seen = datetime.fromisoformat(hindsight_timestamp.replace('Z', '+00:00'))
            if (now - last_seen).total_seconds() > (72 * 3600):
                is_hindsight_stale = True
        else:
            hindsight_empty = True

        # ── Resource patterns (from recent_lib_data already fetched) ──
        # Weekend rest mode: strip Ideas/resources data so the LLM literally cannot surface them
        if is_weekend and not is_pre_monday:
            pattern_context = "None"
            newly_enriched_context = "None"
            recent_urls_context = "None"
        else:
            if recent_lib_data:
                enriched_items = []
                for r in recent_lib_data:
                    note = r.get('strategic_note') or ""
                    enriched_items.append(f"[ID:{r['id']}] [{r['category']}] {r['title']} | {note}".strip())
                pattern_context = " | ".join(enriched_items)
            else:
                pattern_context = "None"

            newly_enriched_context = "None"
            if batch_enrich_results:
                newly_enriched_lines = [f"[ID:{r.get('id', '?')}] [{r.get('category', 'LINK')}] {r.get('title', 'Unknown')} | {r.get('strategic_note', '')}" for r in batch_enrich_results]
                newly_enriched_context = " | ".join(newly_enriched_lines)

            recent_urls_context = "None"
            if recent_lib_data:
                url_lines = []
                for r in recent_lib_data[:30]:
                    label = r.get('title') or r.get('url', 'Unknown')
                    cat = r.get('category') or 'RAW'
                    note = r.get('strategic_note') or ''
                    url_lines.append(f"[ID:{r['id']}] [{cat}] {label} | {note}".strip().rstrip('| '))
                recent_urls_context = "\n".join(url_lines)

        active_clusters_res = supabase.table('clusters').select('title, description').eq('status', 'active').execute()
        active_clusters_context = "\n".join([f"- {c['title']}: {c.get('description', '')}" for c in active_clusters_res.data]) if active_clusters_res.data else "None"

        try:
            last_pulse_row = next((c for c in core if c.get('key') == 'last_pulse_summary'), None)
            session_memory = last_pulse_row['content'] if last_pulse_row else "None"
        except Exception:
            session_memory = "None"

        # ── Delta briefing ──
        delta_context = "None"
        try:
            hist_row = next((c for c in core if c.get('key') == 'briefing_history'), None)
            history = json.loads(hist_row['content']) if hist_row and hist_row.get('content') else []
            curr_task_ids = set(str(t.get('id')) for t in filtered_tasks)

            if history:
                prev = history[0]
                prev_task_ids = set(prev.get('task_ids', []))
                prev_completed_ids = set(prev.get('completed_ids', []))
                new_ids = curr_task_ids - prev_task_ids
                dropped_ids = prev_task_ids - curr_task_ids - prev_completed_ids
                delta_lines = []
                if new_ids:
                    new_titles = [t.get('title', '') for t in filtered_tasks if str(t.get('id')) in new_ids]
                    delta_lines.append(f"🆕 NEW since last briefing: {', '.join(new_titles[:5])}")
                if dropped_ids:
                    delta_lines.append(f"📍 {len(dropped_ids)} task(s) moved off the active board")
                if delta_lines:
                    delta_context = "\n".join(delta_lines)
                else:
                    delta_context = "No significant changes since last briefing."

                if len(history) >= 3:
                    recurring_drops = set()
                    for prev_snap in history[:5]:
                        prev_ids = set(prev_snap.get('task_ids', []))
                        recurring_drops.update(prev_ids - curr_task_ids)
                    if len(recurring_drops) >= 2:
                        delta_lines.append(f"🔄 {len(recurring_drops)} task(s) appeared and dropped across multiple briefings — review?")
                        delta_context = "\n".join(delta_lines)
            else:
                delta_context = "First briefing — no history to compare."

            curr_snapshot = {'task_ids': list(curr_task_ids), 'completed_ids': [], 'timestamp': now.isoformat()}
            history.insert(0, curr_snapshot)
            history = history[:5]
            chk = supabase.table('core_config').select('id').eq('key', 'briefing_history').execute()
            if chk.data:
                supabase.table('core_config').update({"content": json.dumps(history)}).eq('key', 'briefing_history').execute()
            else:
                supabase.table('core_config').insert({"key": "briefing_history", "content": json.dumps(history)}).execute()
        except Exception as e:
            audit_log_sync("pulse", "WARNING", f"Delta briefing history error: {e}")

        # ── New inputs (recent inbound messages) ──
        new_inputs_text = "None"
        new_input_summary = "None"
        try:
            inputs_cutoff = (now - timedelta(hours=24)).isoformat()
            dumps_res = supabase.table('raw_dumps') \
                .select('id, content, message_type, created_at') \
                .eq('direction', 'incoming') \
                .gte('created_at', inputs_cutoff) \
                .order('created_at', desc=True) \
                .limit(20) \
                .execute()
            new_dumps = dumps_res.data or []
            if new_dumps:
                new_lines = []
                for d in new_dumps:
                    content = (d.get('content') or '').strip()
                    msg_type = d.get('message_type', 'note')
                    if content:
                        preview = content[:100].replace('\n', ' ')
                        new_lines.append(f"- [{msg_type}] {preview}")
                if new_lines:
                    new_inputs_text = "\n".join(new_lines)
                    new_input_summary = f"{len(new_dumps)} new {'messages' if len(new_dumps) != 1 else 'message'} since last briefing"
        except Exception as e:
            audit_log_sync("pulse", "WARNING", f"New inputs fetch failed: {e}")

        current_time_str = now.strftime("%A, %B %d, %Y at %I:%M %p %Z")

        # ── Practices context ──
        practices_context = ""
        if is_pre_monday or (day == 1 and hour < 11):
            try:
                practice_results = await detect_practices()
                if practice_results:
                    audit_log_sync("pulse", "INFO", f"📊 Detected {len(practice_results)} practice(s)")
                    edge_count = await build_practice_edges(practice_results)
                    audit_log_sync("pulse", "INFO", f"📊 Built {edge_count} practice edges")
                    correlations = await build_practice_correlations(practice_results)
                    audit_log_sync("pulse", "INFO", f"📊 Built {len(correlations)} practice correlations")
                    sync_count = await sync_practice_canonical_pages(practice_results)
                    audit_log_sync("pulse", "INFO", f"📊 Synced {sync_count} practice canonical pages")
                    practices_context = await build_rhythms_section(practice_results)
            except Exception as e:
                audit_log_sync("pulse", "WARNING", f"Practice detection failed: {e}")

        # ── Morning narrative (reserved for future context registry integration) ──
        morning_pulse_narrative = ""

        # ── People names ──
        people_names = "None"
        if people:
            people_names = "; ".join([
                f"{p.get('name', '?')} (org: {org_map.get(p.get('organization_id'), 'INBOX')})"
                for p in people[:20]
            ])

        # ── Urgency lists ──
        urgent_tasks = [t for t in filtered_tasks if t.get('priority') == 'urgent']
        high_tasks = [t for t in filtered_tasks if t.get('priority') == 'high']
        normal_tasks = [t for t in filtered_tasks if t.get('priority') not in ('urgent', 'high')]
        urgency_lists = ""
        if urgent_tasks:
            urgency_lists += "🔴 URGENT:\n" + "\n".join(f"- {t['title']}" for t in urgent_tasks) + "\n"
        if high_tasks:
            urgency_lists += "🟡 IMPORTANT:\n" + "\n".join(f"- {t['title']}" for t in high_tasks) + "\n"
        if normal_tasks:
            urgency_lists += "⚪ BACKLOG:\n" + "\n".join(f"- {t['title']}" for t in normal_tasks[:5])

        overdue_tasks_json = json.dumps(overdue_tasks) if overdue_tasks else "None"

        cluster_task_list = "\n".join(compressed_tasks_list) if compressed_tasks_list else "No tasks."

        # ── Project routing logic ──
        project_routing_logic = ""
        try:
            from core.pulse.utils import build_routing_context
            project_routing_logic = build_routing_context()
        except Exception as e:
            audit_log_sync("pulse", "WARNING", f"Project routing context failed: {e}")

        # ── Session memory context ──
        session_memory_context = f"PREVIOUS SESSION: {session_memory}" if session_memory and session_memory != "None" else ""

        # ── Briefing history ──
        briefing_history_context = _get_recent_briefings_context()

        # ═══════════════════════════════════════
        # LLM BRIEFING GENERATION (Single call — no agent loop)
        # ═══════════════════════════════════════

        from core.pulse.models import BriefingContext
        
        sample_task = filtered_tasks[0] if filtered_tasks else {}
        st_id = str(sample_task.get("id", "123"))
        st_title = str(sample_task.get("title", "Review the new project brief"))
        
        # ── Focal selection learning: fetch patterns so the LLM knows
        # which item types have been historically rejected ──
        focal_learning_context = "No pattern data yet."
        try:
            from core.lib.telemetry import get_pattern_summary
            focal_patterns = await get_pattern_summary(
                "focal_selection", min_observations=3, max_patterns=10
            )
            if focal_patterns:
                lines = []
                for p in focal_patterns:
                    feat = p.get("features", {})
                    item_type = feat.get("item_type", "unknown")
                    rec = p.get("recommendation", "review")
                    rule = p.get("rule", "")
                    lines.append(f"- {item_type}: {rec} ({rule})")
                focal_learning_context = (
                    "User's historical focal card corrections:\n"
                    + "\n".join(lines)
                    + "\nDo NOT pick item types marked 'reject' or 'auto_reject'. "
                    "Deprioritize types with low confidence."
                )
        except Exception:
            pass  # Fail-open: no patterns → default context

        # ── Home mode learning: fetch patterns so the LLM knows which
        # modes the user has historically overridden ──
        home_mode_learning_context = "No pattern data yet."
        try:
            from core.lib.telemetry import get_pattern_summary
            mode_patterns = await get_pattern_summary(
                "home_mode", min_observations=3, max_patterns=5
            )
            if mode_patterns:
                lines = []
                for p in mode_patterns:
                    feat = p.get("features", {})
                    mode = feat.get("mode", "unknown")
                    rec = p.get("recommendation", "review")
                    rule = p.get("rule", "")
                    lines.append(f"- {mode}: {rec} ({rule})")
                home_mode_learning_context = (
                    "User's historical mode overrides:\n"
                    + "\n".join(lines)
                    + "\nIf a mode is marked 'reject', do NOT suggest it. "
                    "The user has consistently switched away from it."
                )
        except Exception:
            pass  # Fail-open: no patterns → default context

        ctx = BriefingContext(
            sample_task_id=st_id,
            sample_task_title=st_title,
            sample_task_reason=f"This is blocking the next phase of {st_title}.",
            season_config=season_config,
            briefing_mode=briefing_mode,
            current_time_str=current_time_str,
            is_overloaded=is_overloaded,
            is_monday_morning=is_monday_morning,
            overdue_tasks_json=overdue_tasks_json,
            stale_context=stale_context or "None",
            system_context=system_context,
            is_hindsight_stale=is_hindsight_stale,
            hindsight_empty=hindsight_empty,
            calendar_context=calendar_context,
            recent_memories_context=recent_memories_context,
            hindsight_context=hindsight_context,
            weekly_patterns_str=weekly_patterns_str,
            graph_task_context=graph_task_context,
            morning_pulse_narrative=morning_pulse_narrative,
            serendipity_context=serendipity_context,
            canonical_context=master_page_context,
            delta_context=delta_context,
            practices_context=practices_context,
            cluster_task_list=cluster_task_list,
            urgency_lists=urgency_lists,
            new_inputs=new_inputs_text,
            new_input_tags=new_input_summary,
            session_memory_context=session_memory_context,
            pattern_context=pattern_context,
            newly_enriched_context=newly_enriched_context,
            recent_urls_context=recent_urls_context,
            active_clusters_context=active_clusters_context,
            dependency_context=dependency_context,
            social_graph_context=social_graph_context,
            temporal_context=temporal_context,
            centrality_context=centrality_context,
            adaptive_context=adaptive_context,
            people_names=people_names,
            universal_task_map=universal_task_map,
            core=json.dumps(core) if core else "None",
            focal_learning_context=focal_learning_context,
            home_mode_learning_context=home_mode_learning_context,
        )
        prompt = build_pulse_briefing_prompt(ctx, user_name=user_name)

        system_instruction = build_pulse_system_instruction(
            system_persona=system_persona,
            briefing_history_context=briefing_history_context,
            routing_logic=project_routing_logic,
            drift_context=drift_context,
            user_name=user_name,
        )

        # ── Single LLM call with structured output ──
        response = await generate_content_with_fallback(
            prompt=prompt,
            workload=WorkloadProfile.SYNTHESIS,
            system_instruction=system_instruction,
            primary_model=SYNTHESIS_MODEL,
            config={'response_mime_type': 'application/json'},
            require_json=True,
        )

        output = response.parse_json() if hasattr(response, 'parse_json') else {}

        if isinstance(output, dict):
            try:
                pulse_output = PulseOutput(**output)
                briefing_text = pulse_output.briefing or ""
            except Exception:
                # PulseOutput validation failed — try extracting briefing from parsed dict directly
                raw_briefing = output.get("briefing")
                if isinstance(raw_briefing, str):
                    briefing_text = raw_briefing
                elif isinstance(raw_briefing, dict):
                    # LLM returned briefing as nested dict — extract content or use safe fallback
                    briefing_text = raw_briefing.get("text") or raw_briefing.get("content") or "No briefing generated."
                else:
                    briefing_text = str(raw_briefing) if raw_briefing is not None else ""
            if not briefing_text:
                # LLM returned valid JSON but briefing was empty — use safe fallback
                audit_log_sync("pulse", "WARNING", "LLM returned empty briefing field — using safe fallback")
                briefing_text = "No actionable items to report."
        else:
            briefing_text = response.text or "No briefing generated."

        # ── Clean briefing text ──
        if not briefing_text:
            briefing_text = "No actionable items to report."

        briefing_text = briefing_text.strip()

        # Inject formatting for readability
        briefing_text = re.sub(r'(?<!\n)\n(?=🚀|🏠|⛪|💡|✅|📅|🔴|🟡|⚪|⏳|🛡️)', r'\n\n', briefing_text)
        briefing_text = re.sub(r'\[ID:\d+\]', '', briefing_text)
        # Defense-in-depth: the pulse prompt bans markdown ### headers, but a
        # disobedient LLM run can still emit them. Convert "### Home" → "**Home**"
        # so Telegram renders a clean bold section header instead of raw markdown.
        # Requires whitespace after the hashes so task titles like "#1 Follow up"
        # (which start a line with # but no space) are never mangled.
        briefing_text = re.sub(r'^#{1,6}\s+(.+?)\s*$', r'**\1**', briefing_text, flags=re.MULTILINE)

        # ── Opening guarantee: headline + Rhodey opening line (server-side) ──
        # The prompt mandates an opening, but a weak LLM run can still drop it.
        # Enforce it here so every pulse opens with the headline and a voice
        # line before any section header.
        try:
            opening_hint = ""
            if isinstance(output, dict):
                try:
                    opening_hint = (pulse_output.voice_line or "").strip()
                except Exception:
                    pass
            if not opening_hint:
                if new_inputs_text and new_inputs_text != "None":
                    opening_hint = "A few things came in since the last briefing — here's where things stand."
                elif overdue_tasks or urgent_tasks:
                    opening_hint = "There are items that need your attention — here's where things stand."
                else:
                    opening_hint = "Here's where things stand."
            briefing_text = _ensure_briefing_opening(briefing_text, briefing_mode, opening_hint)
        except Exception as e:
            audit_log_sync("pulse", "WARNING", f"Opening guarantee failed: {e}")

        # ── Sunday transparency report (must run before Telegram send) ──
        # Save pre-report briefing for voice_line extraction (report text could confuse _extract_insight)
        pre_report_briefing = briefing_text
        transparency_report_text = ""
        if now.weekday() == 6:
            try:
                transparency_report_text = await build_transparency_report()
                if transparency_report_text:
                    briefing_text += f"\n\n{transparency_report_text}"
                    audit_log_sync("pulse", "INFO", "📊 Appended Sunday transparency report to briefing")
            except Exception as e:
                audit_log_sync("pulse", "WARNING", f"Transparency report failed: {e}")

        # ── Newly-tracked, unconfirmed connections (plans/73) ──
        # One gentle check-in line replacing the retired "things to clarify"
        # questions: list recently-tracked low-confidence pending edges so the
        # user can correct in passing instead of being interrogated per item.
        # Empty when nothing is unconfirmed — "all clear" stays a feature.
        try:
            unconfirmed_cutoff = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
            uc_res = supabase.table('pending_graph_edges') \
                .select('source_label, target_label, relationship') \
                .eq('status', 'pending') \
                .lt('confidence', 0.7) \
                .gte('created_at', unconfirmed_cutoff) \
                .order('created_at', desc=True) \
                .limit(3) \
                .execute()
            if uc_res.data:
                uc_items = [
                    f"{r['source_label']} {r['relationship']} {r['target_label']}"
                    for r in uc_res.data
                ]
                briefing_text += ("\n\nHeads up — I've started tracking: "
                                  + ", ".join(uc_items)
                                  + ". Tell me if I've got any wrong.")
                audit_log_sync("pulse", "INFO", "Appended unconfirmed-tracking check-in to briefing")
        except Exception as e:
            audit_log_sync("pulse", "WARNING", f"Unconfirmed-tracking check-in failed: {e}")

        # ═══════════════════════════════════════
        # WRITE PHASE (all side effects happen here)
        # ═══════════════════════════════════════

        # M-leak fix: per-tenant Telegram chat — NEVER the env var from a
        # tenant-scoped loop. users.telegram_chat_id for THIS tenant (Danny
        # has his chat stored); app-only tenants get None → no Telegram send
        # (their channel is push). Using the env chat here would deliver
        # every tenant's briefing to tenant #1's Telegram (cross-tenant
        # leak, Aug 9).
        telegram_chat_id = resolve_telegram_chat_id()
        send_success = False

        if telegram_chat_id and briefing_text:
            send_success = await send_telegram(
                chat_id=int(telegram_chat_id),
                message_text=briefing_text,
                show_keyboard=False,
                inline_keyboard=None,
                # The dedicated "Rhodey Pulse" push below is the single push for
                # briefings — suppress send_telegram's internal one to avoid a
                # duplicate notification for every pulse briefing.
                notify_push=False,
            )

        # Send push notification regardless of Telegram success
        # Decoupled from send_success so app gets notified even if Telegram fails
        try:
            notification_body = briefing_text[:80].strip() if briefing_text.strip() else "New pulse briefing available"
            # M18 Phase 2A: never-guard — if the banner preview touches a
            # persona never-topic (e.g. debt/stress), fall back to a neutral
            # banner. The FULL briefing still travels in data.content, so the
            # app renders it intact; only the lock-screen copy is neutral.
            from core.services.persona import persona_guard_text

            notification_body = persona_guard_text(
                notification_body, fallback="New pulse briefing available"
            )
            push_count = await send_push_notification(
                title="Rhodey",
                body=notification_body,
                data={"type": "briefing", "content": push_data_content(briefing_text)},
            )
            audit_log_sync("pulse", "INFO", f"📲 Push notification sent to {push_count} device(s)")
            # Silent data-only push for Flutter background refresh
            silent_count = await send_silent_push({"type": "briefing_refresh"})
            audit_log_sync("pulse", "INFO", f"📲 Silent push sent to {silent_count} device(s)")
        except Exception as e:
            audit_log_sync("pulse", "WARNING", f"Push notification failed: {e}")

        # ── Store history ──
        try:
            _store_briefing_to_history(briefing_text)
        except Exception as e:
            audit_log_sync("pulse", "WARNING", f"Failed to store briefing history: {e}")

        # ── Store as raw_dump for Flutter app ──
        try:
            supabase.table('raw_dumps').insert({
                'content': briefing_text,
                'source': 'pulse_engine',
                'message_type': 'pulse_briefing',
                'direction': 'outgoing',
                'status': 'completed',
                'metadata': {
                    'briefing_mode': briefing_mode,
                    'run_id': run_id,
                    'insight': _extract_insight(briefing_text),
                    'overdue_tasks': overdue_tasks,
                    'stale_tasks': [t.get('title') for t in stale_tasks],
                    'delta': delta_context if delta_context != "None" else '',
                    'vaulted_count': len(active_tasks) - len(filtered_tasks),
                }
            }).execute()
        except Exception as e:
            audit_log_sync("pulse", "WARNING", f"Failed to store raw_dump: {e}")

        # ── Store app intelligence (for Flutter app) ──
        try:
            voice_line = ""
            # Extract home_mode from PulseOutput (defaults to proceed)
            home_mode = "proceed"
            if isinstance(output, dict):
                try:
                    if pulse_output.home_mode in ("proceed", "decide", "sprint", "catch_up", "wrap"):
                        home_mode = pulse_output.home_mode
                except Exception:
                    # PulseOutput not available — fall back to raw parsed dict
                    raw_home = output.get("home_mode", "proceed")
                    if isinstance(raw_home, str) and raw_home in ("proceed", "decide", "sprint", "catch_up", "wrap"):
                        home_mode = raw_home

            # Extract top_focal_item from PulseOutput
            top_focal_item = {}
            if isinstance(output, dict):
                try:
                    raw_item = pulse_output.top_focal_item
                    if raw_item and isinstance(raw_item, dict) and raw_item.get("title"):
                        top_focal_item = raw_item
                except Exception:
                    raw_item = output.get("top_focal_item", {})
                    if isinstance(raw_item, dict) and raw_item.get("title"):
                        top_focal_item = raw_item

            if isinstance(output, dict):
                try:
                    voice_line = pulse_output.voice_line.strip()
                except Exception:
                    pass
            if not voice_line:
                voice_line = _extract_insight(pre_report_briefing)[:120]

            # Map briefing_mode to a clean pulse_mode for the app
            pulse_mode_clean = _map_pulse_mode(briefing_mode)

            nag_list = overdue_tasks if overdue_tasks else []
            stale_names = [t.get('title', '') for t in stale_tasks] if stale_tasks else []
            vaulted_count = len(active_tasks) - len(filtered_tasks)

            # Build delta snapshot as list of {id, title} pairs
            delta_snapshot = [{"id": str(t.get('id')), "title": t.get('title')} for t in filtered_tasks]

            # Build insights list
            insights = []
            if overdue_tasks:
                insights.append(f"🔴 {len(overdue_tasks)} stale task{'s' if len(overdue_tasks) != 1 else ''} — needs attention")
            if stale_names:
                insights.append(f"⏳ {len(stale_names)} task{'s' if len(stale_names) != 1 else ''} stale >7d")
            if vaulted_count > 0:
                insights.append(f"📦 {vaulted_count} item{'s' if vaulted_count != 1 else ''} vaulted behind the pulse")

            # Build context from voice_line + nag count
            context_text = voice_line[:120] if voice_line else _extract_insight(pre_report_briefing)[:120]

            intelligence_run_id = run_id or f"pulse_{datetime.now(timezone.utc).isoformat()}"

            # UPSERT on pulse_run_id so re-runs don't create duplicates
            existing_intel = supabase.table('app_intelligence') \
                .select('id') \
                .eq('pulse_run_id', intelligence_run_id) \
                .limit(1) \
                .execute()
            intel_payload = {
                'home_mode': home_mode,
                'voice_line': voice_line,
                'pulse_mode': pulse_mode_clean,
                'nag_list': json.dumps(nag_list),
                'stale_list': json.dumps(stale_names),
                'overdue_list': json.dumps(overdue_tasks if overdue_tasks else []),
                'vaulted_count': vaulted_count,
                'delta_snapshot': json.dumps(delta_snapshot),
                'context': context_text,
                'insights': json.dumps(insights),
                'top_focal_item': json.dumps(top_focal_item) if top_focal_item else None,
                'transparency_report': transparency_report_text if transparency_report_text else None,
                'pulse_run_id': intelligence_run_id,
                'metadata': json.dumps({
                    'briefing_mode': briefing_mode,
                    'send_success': send_success,
                }),
            }
            if existing_intel.data:
                supabase.table('app_intelligence') \
                    .update(intel_payload) \
                    .eq('pulse_run_id', intelligence_run_id) \
                    .execute()
            else:
                supabase.table('app_intelligence') \
                    .insert(intel_payload) \
                    .execute()
        except Exception as e:
            audit_log_sync("pulse", "WARNING", f"Failed to store app intelligence: {e}")

        # ── Store daily summary ──
        try:
            daily_summary = briefing_text[:200].replace('\n', ' ').strip()
            chk = supabase.table('core_config').select('id').eq('key', 'last_pulse_summary').execute()
            if chk.data:
                supabase.table('core_config').update({"content": daily_summary}).eq('key', 'last_pulse_summary').execute()
            else:
                supabase.table('core_config').insert({"key": "last_pulse_summary", "content": daily_summary}).execute()
        except Exception as e:
            audit_log_sync("pulse", "WARNING", f"Failed to store daily summary: {e}")

        # ── After-action report ──
        try:
            await generate_after_action_report()
        except Exception as e:
            audit_log_sync("pulse", "WARNING", f"After-action report failed: {e}")

        # ── Auto-expiry ──
        try:
            _auto_expire_recurring_tasks()
        except Exception as e:
            audit_log_sync("pulse", "WARNING", f"Auto-expiry maintenance failed: {e}")

        # ── Complete ──
        await complete_pulse_run(supabase, run_id, status="completed",
            dumps_processed=0, tasks_created=0,
            metadata={"briefing_mode": briefing_mode, "send_success": send_success})
        release_lock(lock_key)
        return {"success": True, "briefing": briefing_text}

    except Exception as e:
        import traceback
        audit_log_sync("pulse", "CRITICAL", f"Pulse Engine Critical Error: {e}")
        traceback.print_exc()
        if run_id:
            await complete_pulse_run(supabase, run_id, status="failed", error_message=str(e))
        release_lock(lock_key)
        return {"error": str(e)}
