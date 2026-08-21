import os
import html
import hmac
import hashlib
import time
import httpx
import json
import uuid
import asyncio
import contextvars
from urllib.parse import urlencode, quote
from contextlib import asynccontextmanager
from datetime import datetime, timezone, timedelta
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from core.lib.audit_logger import audit_log_sync, trace_id_var
from core.lib.telemetry import emit_observation
from core.lib.decision_features import build_decision_features
from core.decisions import record_decision
from core.actions import begin_action_context, clear_action_context

from core.webhook import (
    process_channel_pending_decision,
    process_webhook,
    send_draft_reply,
    _emit_draft_observation,
    process_email_pending_decision,
)
from core.services.briefing_refresh import (
    briefing_cache_key,
    fire_briefing_refresh,
    trigger_briefing_refresh,
)
from core.pulse.graph import process_pending_edge_decision, enrich_pending_edges_with_conflicts
from api.briefing import _snooze_ok, _notes_ok
from core.pulse.sentinel import process_sentinel
from core.pulse import (
    process_pulse,
    process_decision_pulse,
    get_tasks_service,
    sync_to_google,
    delete_calendar_event,

    write_outcome_memory,
    get_outlook_calendar_events,
    get_outlook_calendar_events_range,
    format_rfc3339,
)
from core.pulse.tools import skip_recurring_instance
from core.pulse.pipeline import run_full_health_check
from core.services.db import (
    active_user_ids, maybe_single_safe, exec_query,
    get_tenant, set_tenant, resolve_telegram_chat_id, resolve_user_by_api_key,
    tenant_aware_client, tenant_scope,
)
from core.services.push_notification import send_push_notification
from core.services.inbox_feed import (
    fetch_pending_channel_messages, fetch_pending_drafts, fetch_fyi_messages,
)


@asynccontextmanager
async def lifespan(app):
    """FastAPI lifespan: initializes asyncpg pool on startup, closes on shutdown.

    Also upgrades the thread pool from default (min(32, 6)=6) to 16 workers
    because interrogate_brain fires 17+ sync Supabase calls via asyncio.to_thread().
    """
    yield


app = FastAPI(title="Integrated-OS", lifespan=lifespan)

# CORS allowlist (audit 2.3): the API is consumed by the Flutter app (native,
# no CORS) and the Next.js dashboard. Wildcard origins were a defense-in-depth
# hole — restrict to the env-driven ALLOWED_ORIGINS list (comma-separated),
# defaulting to the local dev dashboard. Server-to-server callers (Telegram,
# cron, Modal) are not subject to CORS and are unaffected.
_ALLOWED_ORIGINS = [
    o.strip()
    for o in os.getenv("ALLOWED_ORIGINS", "http://localhost:3000").split(",")
    if o.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_ALLOWED_ORIGINS,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/")
def health_check():
    return {"status": "Integrated OS API is running on Python 🐍"}

# --- TELEGRAM INTAKE (Inline processing with 55s timeout) ---
@app.post("/api/webhook")
async def webhook_route(request: Request):
    # Telegram webhook authentication (audit 2.1): when TELEGRAM_WEBHOOK_SECRET
    # is configured, Telegram sends it in the X-Telegram-Bot-Api-Secret-Token
    # header (set via setWebhook(secret_token=...)). Reject requests without a
    # matching token — otherwise anyone who knows the Modal URL can inject fake
    # updates. When the secret is NOT configured, log a loud warning but accept
    # (backward-compat until the webhook is re-registered with a secret_token).
    secret = os.getenv("TELEGRAM_WEBHOOK_SECRET")
    if secret:
        token = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if not hmac.compare_digest(token, secret):
            print("Webhook rejected: bad X-Telegram-Bot-Api-Secret-Token")
            raise HTTPException(status_code=401, detail="Unauthorized")
    else:
        print("⚠️ Webhook auth DISABLED — set TELEGRAM_WEBHOOK_SECRET and "
              "re-register the webhook with secret_token to close this hole.")

    update = await request.json()
    trace_id_var.set(f"tg_{update.get('update_id', uuid.uuid4().hex[:8])}")
    begin_action_context()
    try:
        await asyncio.wait_for(process_webhook(update), timeout=295)
        return {"success": True}
    except asyncio.TimeoutError:
        print("Webhook processing timed out (>295s). Modal may kill at 300s.")
        return {"success": True, "message": "Processing started"}
    except Exception as e:
        print(f"Webhook error: {e}")
        raise HTTPException(status_code=500, detail="Internal processing error")
    finally:
        clear_action_context()

def verify_hmac(payload: bytes, signature: str, secret: str) -> bool:
    expected = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)

def require_api_auth(request: Request) -> str | None:
    """Authenticate an API request and resolve the tenant context (M3).

    Resolution order:
      1. Per-user key (multi-tenant): X-API-Key matches a users.api_key_hash
         (sha256). Sets the tenant context to the user's id and RETURNS it.
         The tenant STAYS set for the rest of the handler — FastAPI runs each
         request in its own task, so contextvars cannot leak between
         requests, and fire-and-forget tasks spawned inside a handler
         inherit the caller's tenant, which is correct (they process that
         user's data). Handlers are on the tenant-aware facade (M3), so a
         per-user key auto-scopes every query in the handler body.
      2. Legacy shared key: X-API-Key matches API_SECRET_KEY. Authorized but
         UN-scoped (pre-db/78 production / transition) — returns None.
      3. Fail closed (audit 2.2): when API_SECRET_KEY is unset the API is
         REJECTED unless ALLOW_DEV_AUTH=1 is explicitly set (local dev). A
         missing env var in production must never leave the whole API open.
    """
    api_key = request.headers.get("X-API-Key")
    expected = os.getenv("API_SECRET_KEY")

    if api_key:
        user = resolve_user_by_api_key(api_key)
        if user and user.get("status", "active") == "active":
            set_tenant(str(user["id"]))
            return str(user["id"])

    if not expected:
        # Fail closed: reject unless dev mode is explicitly enabled.
        if os.getenv("ALLOW_DEV_AUTH") == "1":
            from core.services.db import resolve_channel_tenant
            uid = resolve_channel_tenant()
            if uid:
                set_tenant(uid)
            return uid
        raise HTTPException(status_code=503, detail="API auth not configured")
    if not api_key or not hmac.compare_digest(api_key, expected):
        raise HTTPException(status_code=401, detail="Unauthorized")
        
    from core.services.db import resolve_channel_tenant
    uid = resolve_channel_tenant()
    if uid:
        set_tenant(uid)
    return uid

# --- THE PULSE ENGINE (Routes to pulse.py) ---
@app.post("/api/pulse")
async def pulse_route_post(request: Request):
    trace_id_var.set(f"pulse_{uuid.uuid4().hex[:8]}")
    # HMAC-SHA256 verification for Pulse trigger requests
    raw_body = await request.body()
    sig_header = request.headers.get('X-Rhodey-Signature', '')
    
    pulse_secret = os.getenv("PULSE_SECRET")
    if not verify_hmac(raw_body, sig_header, pulse_secret):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    # Extracts the secret from the GitHub Actions cron header
    secret = request.headers.get("x-pulse-secret")
    
    # Executes the strategic briefing logic
    result = await process_pulse(auth_secret=secret, trigger="api")
    
    # Gatekeeper error handling
    if result.get("error"):
        raise HTTPException(status_code=result.get("status", 500), detail=result["error"])
        
    return {"success": True, "briefing": result.get("briefing")}

# --- PULSE HEARTBEAT (cron-job.org — gated per-tenant briefing) ---
@app.get("/api/pulse-cron")
@app.post("/api/pulse-cron")
async def pulse_cron_route(request: Request):
    """Triggered by cron-job.org every 30 minutes — the briefing heartbeat.

    Option B fan-out: instead of running every tenant's briefing sequentially
    inside this web request (which overran the 300s Modal timeout and killed
    every tenant after the first), this endpoint spawns ONE dedicated Modal
    worker (brief_tenant, 900s timeout) per DUE tenant, in parallel, and
    returns immediately. Each tenant runs in its own container with its own
    timeout budget — a slow tenant can never starve or kill the others, and
    the fan-out scales to any number of tenants.

    The schedule gate still applies (per-tenant briefing_schedule): only
    tenants whose slot is due get a worker. If the Modal SDK is unavailable
    (local dev / tests), it falls back to the inline sequential path.
    """
    auth_header = request.headers.get("Authorization", "")
    cron_secret = os.getenv("CRON_SECRET", os.getenv("PULSE_SECRET"))

    if not cron_secret:
        raise HTTPException(status_code=500, detail="CRON_SECRET missing")

    if auth_header != f"Bearer {cron_secret}" and request.headers.get("x-pulse-secret") != cron_secret:
        raise HTTPException(status_code=401, detail="Unauthorized")

    from core.pulse.briefing import due_tenant_ids

    due = due_tenant_ids(trigger="cron")
    if not due:
        # Nobody due at this heartbeat — keep the channel tenant's heartbeat
        # fresh so the health check reports a healthy pipeline (same behavior
        # as the inline path's fallback).
        try:
            from core.services.db import channel_tenant_scope
            from core.pulse.pipeline import update_heartbeat
            with channel_tenant_scope():
                await update_heartbeat()
        except Exception:
            pass
        return {"success": True, "mode": "fanout", "spawned": 0,
                "tenants": [], "note": "no tenant due"}

    try:
        import modal
    except ImportError:
        # Local dev / tests without the Modal SDK — inline sequential fallback
        # (identical behavior to the pre-Option-B path).
        result = await process_pulse(auth_secret=cron_secret, trigger="cron")
        result["mode"] = "inline_fallback"
        return result

    spawned = []
    failed = []
    for uid in due:
        try:
            await modal.Function.from_name("rhodey-os", "brief_tenant").spawn.aio(
                uid=uid, auth_secret=cron_secret, trigger="cron"
            )
            spawned.append(uid)
        except Exception as e:
            audit_log_sync("pulse", "WARNING", f"brief_tenant spawn failed for {uid}: {e}")
            failed.append(uid)

    inline_results = []
    if failed:
        # Partial fallback: run ONLY the tenants whose spawn failed, inline
        # (per-tenant unit — never double-briefs the successfully-spawned
        # tenants). Covers Modal SDK/auth hiccups without a total outage.
        from core.pulse.briefing import process_pulse_for_tenant

        for uid in failed:
            try:
                inline_results.append(
                    await process_pulse_for_tenant(uid, auth_secret=cron_secret, trigger="cron")
                )
            except Exception as e:
                audit_log_sync("pulse", "WARNING", f"Inline fallback briefing failed for {uid}: {e}")

    return {
        "success": True,
        "mode": "fanout",
        "spawned": len(spawned),
        "inline_fallback": len(inline_results),
        "tenants": [str(u)[:8] for u in spawned],
        "total_due": len(due),
    }

# --- THE SENTINEL WATCHER (Vercel Cron) ---
@app.get("/api/sentinel")
@app.post("/api/sentinel")
async def sentinel_route(request: Request):
    """Triggered by Vercel Cron every 5 minutes."""
    # Vercel Cron uses a bearer token
    auth_header = request.headers.get("Authorization", "")
    cron_secret = os.getenv("CRON_SECRET", os.getenv("PULSE_SECRET"))
    
    if not cron_secret:
        raise HTTPException(status_code=500, detail="CRON_SECRET missing")
        
    if auth_header != f"Bearer {cron_secret}" and request.headers.get("x-pulse-secret") != cron_secret:
        raise HTTPException(status_code=401, detail="Unauthorized")
        
    result = await process_sentinel(auth_secret=cron_secret, trigger="cron")
    return result



# --- DECISION PULSE (Pending Approvals) ---
@app.get("/api/decision-pulse")
@app.post("/api/decision-pulse")
async def decision_pulse_route(request: Request):
    """Triggered by cron-job.org — pending approvals (no AI)."""
    auth_header = request.headers.get("Authorization", "")
    cron_secret = os.getenv("CRON_SECRET", os.getenv("PULSE_SECRET"))

    if not cron_secret:
        raise HTTPException(status_code=500, detail="CRON_SECRET missing")

    if auth_header != f"Bearer {cron_secret}" and request.headers.get("x-pulse-secret") != cron_secret:
        raise HTTPException(status_code=401, detail="Unauthorized")

    result = await process_decision_pulse(auth_secret=cron_secret, trigger="cron")
    return result

# Backward-compat redirect for old /api/maintenance (now /api/health)
@app.get("/api/maintenance")
@app.post("/api/maintenance")
async def maintenance_redirect_route(request: Request):
    """Redirect to /api/health. Old route kept for backward compat."""
    return await health_check_route(request)

# --- HEALTH CHECK (replaces old /api/maintenance) ---
@app.get("/api/health")
@app.post("/api/health")
async def health_check_route(request: Request):
    """Triggered by cron-job.org or GitHub Actions — runs full health check.

    Replaces the old /api/maintenance route. Runs all health checks
    (stuck dumps, DLQ, recent errors, LLM degradation) and returns results.
    Supports query param ?mode=standard|daily|weekly (modes preserved for compat).
    """
    auth_header = request.headers.get("Authorization", "")
    cron_secret = os.getenv("CRON_SECRET", os.getenv("PULSE_SECRET"))

    if not cron_secret:
        raise HTTPException(status_code=500, detail="CRON_SECRET missing")

    if auth_header != f"Bearer {cron_secret}" and request.headers.get("x-pulse-secret") != cron_secret:
        raise HTTPException(status_code=401, detail="Unauthorized")

    result = await run_full_health_check()
    return result


@app.get("/api/admin/spend")
@app.post("/api/admin/spend")
async def admin_spend_route(request: Request, days: int = 7):
    """(M6) Per-tenant LLM spend — cost-per-user per day/week.

    Admin-only (same bearer/x-pulse-secret gate as /api/health). Reads the
    llm_spend ledger (db/85) through the tenant facade, grouped by day.
    Returns {days: [...], users: {uid: {name, total_usd, days: {date: usd}}}}.
    """
    auth_header = request.headers.get("Authorization", "")
    cron_secret = os.getenv("CRON_SECRET", os.getenv("PULSE_SECRET"))
    if not cron_secret:
        raise HTTPException(status_code=500, detail="CRON_SECRET missing")
    if auth_header != f"Bearer {cron_secret}" and request.headers.get("x-pulse-secret") != cron_secret:
        raise HTTPException(status_code=401, detail="Unauthorized")

    try:
        days = max(1, min(int(days), 90))
    except (TypeError, ValueError):
        days = 7
    from datetime import datetime, timedelta, timezone
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

    from core.services.db import get_supabase
    from core.services.user_settings import resolve_user_name

    # Global admin read — raw client on purpose (no tenant context here; the
    # tenant facade would fail closed without one). Gated by the secret above.
    client = get_supabase()
    try:
        res = (
            client.table("llm_spend")
            .select("owner_id, ts, est_cost_usd")
            .gte("ts", cutoff)
            .execute()
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"llm_spend unavailable: {e}")

    # Start from ALL active users (a user with credit but zero spend must
    # still appear in the credits view), then overlay ledger + credit.
    users: dict = {}
    try:
        ures = client.table("users").select("id, name").eq("status", "active").execute()
        for u in ures.data or []:
            uid = u.get("id")
            if uid:
                users[uid] = {"name": u.get("name") or uid, "total_usd": 0.0, "days": {}}
    except Exception:
        pass
    for row in res.data or []:
        uid = row.get("owner_id")
        if not uid:
            continue
        date = (row.get("ts") or "")[:10]
        cost = float(row.get("est_cost_usd") or 0.0)
        u = users.setdefault(uid, {"name": resolve_user_name(uid), "total_usd": 0.0, "days": {}})
        u["total_usd"] += cost
        u["days"][date] = u["days"].get(date, 0.0) + cost
    # Credit overlay: table-driven monthly credit, cycle = signup day, spend
    # from the same ledger, remaining floored at 0. These helpers take an
    # explicit uid and use the raw client internally, so no tenant context
    # is required here.
    from core.llm.budget import (
        credit_remaining, cycle_spend_usd, cycle_start_utc, resolve_monthly_credit,
    )
    for uid, u in users.items():
        u["total_usd"] = round(u["total_usd"], 4)
        u["days"] = {k: round(v, 4) for k, v in sorted(u["days"].items())}
        try:
            u["monthly_credit_usd"] = resolve_monthly_credit(uid)
            u["cycle_start_utc"] = cycle_start_utc(uid).isoformat()
            u["cycle_spent_usd"] = round(cycle_spend_usd(uid), 4)
            u["credit_remaining_usd"] = round(credit_remaining(uid), 4)
        except Exception:
            pass
    return {"days": days, "users": users}


# --- GET TASKS (for Today tab — active + overdue) ---
@app.get("/api/tasks")
async def get_tasks_route(request: Request, status: str = None, limit: int = 50, offset: int = 0,
                         include_snoozed: bool = False):
    """List tasks filtered by status. Default: active (todo) tasks.

    include_snoozed=True returns ALL active tasks including snoozed ones
    (with snoozed_until populated) so the app's task ledger can show them
    dimmed. The default (False) keeps the focal-card "Not now" deferral
    behavior — snoozed tasks stay hidden from the focus queue.
    """
    require_api_auth(request)
    try:
        supabase = tenant_aware_client()
        # NOTE: requires migration 75 (tasks.organization_id -> graph_nodes).
        select_cols = ('id, title, status, priority, deadline, created_at, '
                       'organization_id, direction, committed_to, recurrence, '
                       'graph_nodes(label)')
        # Only select notes when the column exists (pre-migration safe)
        if _notes_ok(supabase, 'tasks'):
            select_cols += ', notes'
        # Only select snoozed_until when the column exists (pre-migration safe)
        if include_snoozed and _snooze_ok(supabase, 'tasks'):
            select_cols += ', snoozed_until'
        query = supabase.table('tasks')\
            .select(select_cols)\
            .eq('is_current', True)
        
        if status:
            # Comma-separated statuses are supported (e.g. "todo,in_progress")
            # so the app can keep committed (in_progress) tasks visible next
            # to open todos in the same fetch.
            statuses = [s.strip() for s in status.split(',') if s.strip()]
            if len(statuses) > 1:
                query = query.in_('status', statuses)
            else:
                query = query.eq('status', statuses[0] if statuses else 'todo')
        else:
            query = query.in_('status', ['todo'])
        
        # Exclude snoozed items by default (focal-card "Not now" deferral).
        # The task-ledger view passes include_snoozed=True to see everything.
        if not include_snoozed and _snooze_ok(supabase, 'tasks'):
            query = query.or_('snoozed_until.is.null,snoozed_until.lt.now')
        
        result = query.order('created_at', desc=True).limit(limit).offset(offset).execute()
        tasks = result.data or []
        # Flatten the nested graph_nodes(label) join into a plain
        # organization_name field — same shape the planner produces — so the
        # app can show "which org this task belongs to" on focal cards.
        # (migration 75: tasks.organization_id now references graph_nodes)
        for t in tasks:
            org = t.pop('graph_nodes', None) or {}
            t['organization_name'] = org.get('label') if isinstance(org, dict) else None
        return {"tasks": tasks}
    except Exception as e:
        print(f"Get tasks error: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")

# --- GET CAPTURES (for Dump tab — recent raw dumps) ---
@app.get("/api/captures")
async def get_captures_route(request: Request, limit: int = 50, offset: int = 0):
    """List recent raw_dumps — the unfiltered capture stream."""
    require_api_auth(request)
    try:
        supabase = tenant_aware_client()
        result = supabase.table('raw_dumps')\
            .select('id, content, created_at, direction, sender, message_type, status, source')\
            .order('created_at', desc=True)\
            .limit(limit)\
            .offset(offset)\
            .execute()
        return {"captures": result.data or []}
    except Exception as e:
        print(f"Get captures error: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")

# --- BRIEFING ENDPOINT (for home-surface feed) ---
@app.get("/api/briefing")
async def get_briefing_route(request: Request):
    """Structured briefing for the Rhodey Surface home screen.

    Returns greeting + sections (briefing, decisions, recent).
    Decisions section is omitted when empty.
    """
    require_api_auth(request)
    try:
        from api.briefing import build_briefing
        supabase = tenant_aware_client()
        briefing = await build_briefing(supabase)
        # Deep-serialize through JSON to strip ALL nested TypedDict subclasses
        # FastAPI's jsonable_encoder chokes on TypedDict subclasses on Vercel
        return json.loads(json.dumps(briefing, default=str))
    except Exception as e:
        print(f"Briefing error: {e}")
        import traceback
        traceback.print_exc()
        from core.services.user_settings import resolve_user_name
        return {
            "greeting": f"Hey, {resolve_user_name()}.",
            "next_event": None,
            "sections": [],
            "pending_count": 0,
            "_error": str(e)[:500],
        }


# --- HOME FEED (single round-trip for app open) ---
@app.get("/api/home-feed")
async def home_feed_route(request: Request):
    """One request returning everything the home screen needs on open.

    Collapses the app's 6 startup round-trips (briefing + pending
    nodes/edges/merges/messages + active tasks) into a single call, all
    fetched in parallel server-side.

    Response shape:
      briefing          → same payload as GET /api/briefing
      pending_nodes     → same rows as /api/pending-graph-nodes
      pending_edges     → same rows as /api/pending-graph-edges
      pending_merges    → same rows as /api/pending-merges
      pending_messages  → same rows as /api/messages (limit 50)
      tasks             → same rows as /api/tasks?status=todo,in_progress (limit 200)
      persona           → Phase 2B surface summary (or null, fail-closed)
    """
    require_api_auth(request)
    try:
        supabase = tenant_aware_client()

        # ── Briefing (includes P1-gated auto-approval) ──
        briefing_fut = asyncio.ensure_future(
            _home_feed_briefing())

        # Supabase's client is SYNCHRONOUS — bare .execute() blocks the event
        # loop and turns asyncio.gather into serial execution. Offloading the
        # blocking I/O to worker threads (exec_query helper) lets these run in
        # parallel AND keeps the loop free so concurrent requests
        # (inbox/today/entities) aren't queued behind home-feed — the root
        # cause of the 20s screen loads.

        # ── Pending nodes (mirror pending_nodes_route) ──
        async def _nodes():
            try:
                q = supabase.table('pending_nodes') \
                    .select('id, label, type:node_type, status, source_text, created_at, eval_context') \
                    .in_('status', ['pending', 'flagged'])
                if _snooze_ok(supabase, 'pending_nodes'):
                    q = q.or_('snoozed_until.is.null,snoozed_until.lt.now')
                res = await exec_query(q.order('created_at', desc=True).limit(100))
                return res.data or []
            except Exception:
                return []

        # ── Pending edges (mirror pending_graph_edges_route) ──
        async def _edges():
            try:
                q = supabase.table('pending_graph_edges') \
                    .select('id, source_label, target_label, relationship, status, confidence, created_at') \
                    .in_('status', ['pending', 'flagged'])
                if _snooze_ok(supabase, 'pending_graph_edges'):
                    q = q.or_('snoozed_until.is.null,snoozed_until.lt.now')
                res = await exec_query(q.order('created_at', desc=True).limit(100))
                return await enrich_pending_edges_with_conflicts(res.data or [])
            except Exception:
                return []

        # ── Pending merges (mirror pending_merges_route) ──
        async def _merges():
            try:
                q = supabase.table('merge_proposals') \
                    .select('id, source_label, source_type, target_label, target_node_id, rationale, status') \
                    .eq('status', 'proposed')
                if _snooze_ok(supabase, 'merge_proposals'):
                    q = q.or_('snoozed_until.is.null,snoozed_until.lt.now')
                res = await exec_query(q.order('id', desc=True).limit(100))
                return res.data or []
            except Exception:
                return []

        # ── Pending messages (mirror /api/messages, limit 50) ──
        async def _messages():
            try:
                res = await exec_query(
                    supabase.table('raw_dumps') \
                    .select('id, content, created_at, direction, sender, message_type, status, metadata, source') \
                    .order('created_at', desc=True) \
                    .limit(50)
                )
                return res.data or []
            except Exception:
                return []

        # Actionable, undecided channel items — the same feed the Inbox tab
        # serves, so the home focal board and Inbox agree on pending counts.
        async def _channel_messages():
            try:
                return await asyncio.to_thread(
                    fetch_pending_channel_messages, supabase, 50
                )
            except Exception:
                return []

        # ── Active tasks (mirror /api/tasks default: status=todo, limit 200) ──
        async def _tasks():
            try:
                # NOTE: requires migration 75 (tasks.organization_id -> graph_nodes).
                select_cols = ('id, title, status, priority, deadline, created_at, '
                               'organization_id, direction, committed_to, recurrence, '
                               'graph_nodes(label)')
                if _notes_ok(supabase, 'tasks'):
                    select_cols += ', notes'
                # Open + committed ("I'll do it") tasks — a committed task
                # stays on the home board until it's actually completed.
                q = supabase.table('tasks') \
                    .select(select_cols) \
                    .eq('is_current', True) \
                    .in_('status', ['todo', 'in_progress'])
                if _snooze_ok(supabase, 'tasks'):
                    q = q.or_('snoozed_until.is.null,snoozed_until.lt.now')
                rows = (await exec_query(q.order('created_at', desc=True).limit(200))).data or []
                # Flatten the nested graph_nodes(label) join into
                # organization_name — same shape /api/tasks produces, so
                # home-feed consumers (Flutter focal cards, web
                # what-to-do-now) keep showing the org.
                for t in rows:
                    org = t.pop('graph_nodes', None) or {}
                    t['organization_name'] = org.get('label') if isinstance(org, dict) else None
                return rows
            except Exception:
                return []

        # ── Persona surface summary (Phase 2B): closed-enum transport ──
        # persona_surface_summary is sync (cached per-tenant after the first
        # read) — offload to a worker thread so home-feed stays non-blocking.
        async def _persona():
            try:
                from core.services.persona import persona_surface_summary
                loop = asyncio.get_running_loop()
                return await loop.run_in_executor(None, persona_surface_summary)
            except Exception as e:
                print(f"Home feed persona error: {e}")
                return None

        nodes_fut = asyncio.ensure_future(_nodes())
        edges_fut = asyncio.ensure_future(_edges())
        merges_fut = asyncio.ensure_future(_merges())
        msgs_fut = asyncio.ensure_future(_messages())
        channel_fut = asyncio.ensure_future(_channel_messages())
        tasks_fut = asyncio.ensure_future(_tasks())
        persona_fut = asyncio.ensure_future(_persona())

        briefing, nodes, edges, merges, messages, channel_msgs, tasks, persona = \
            await asyncio.gather(
                briefing_fut, nodes_fut, edges_fut, merges_fut, msgs_fut,
                channel_fut, tasks_fut, persona_fut)

        return {
            "briefing": briefing,
            "pending_nodes": nodes,
            "pending_edges": edges,
            "pending_merges": merges,
            "pending_messages": messages,
            "pending_channel_messages": channel_msgs,
            "tasks": tasks,
            "persona": persona,
        }
    except Exception as e:
        print(f"Home feed error: {e}")
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail="Internal server error")


@app.get("/api/persona")
async def get_persona_route(request: Request):
    """Phase 2B: safe per-tenant persona surface summary for the app.

    Returns {display_name, voice_style, signoffs} or null. Fail-closed: no
    card (or a legacy shared key without a tenant) => null, so the app
    renders today's neutral copy everywhere. Closed-enum transport (R4) —
    the raw card, curated people, and never-topics never leave the server.
    """
    require_api_auth(request)
    try:
        from core.services.persona import persona_surface_summary
        return persona_surface_summary()
    except Exception as e:
        print(f"Persona summary error: {e}")
        return None


async def _home_feed_briefing():
    """Build the briefing payload for /api/home-feed (defensive wrapper).

    Read-through Redis cache (2 min TTL, PER-TENANT key): the briefing is a
    generated artifact (LLM + 9 data sources) that takes 9-16s to rebuild
    and was blocking the event loop for every request queued behind it.
    Within the TTL window home-feed serves the cached payload in ~1s —
    Pulse regenerates the underlying data on its own schedule, so staleness
    is bounded and deliberate. Cache failures fail open to a live build.
    """
    from api.briefing import build_briefing
    from core.lib.redis_cache import cache_get, cache_set

    cache_key = briefing_cache_key()
    try:
        cached = cache_get(cache_key)
        if cached is not None and isinstance(cached, dict):
            return cached
    except Exception as e:
        print(f"[Briefing] Cache read failed (non-fatal): {e}")

    supabase = tenant_aware_client()
    briefing = await build_briefing(supabase)
    payload = json.loads(json.dumps(briefing, default=str))

    try:
        cache_set(cache_key, payload, ttl=120)
    except Exception as e:
        print(f"[Briefing] Cache write failed (non-fatal): {e}")
    return payload

# --- EVENING ROUNDUP ---
@app.get("/api/roundup")
@app.post("/api/roundup")
async def roundup_route(request: Request):
    """Triggered by cron-job.org — evening roundup prompt."""
    auth_header = request.headers.get("Authorization", "")
    cron_secret = os.getenv("CRON_SECRET", os.getenv("PULSE_SECRET"))

    if not cron_secret:
        raise HTTPException(status_code=500, detail="CRON_SECRET missing")

    if auth_header != f"Bearer {cron_secret}" and request.headers.get("x-pulse-secret") != cron_secret:
        raise HTTPException(status_code=401, detail="Unauthorized")

    try:
        from datetime import datetime, timezone, timedelta
        from core.webhook.telegram import send_telegram

        async def _roundup_for_tenant(uid: str | None) -> dict:
            """Evening roundup for ONE tenant: skip when 3+ notes today, else
            prompt via their channel. Telegram tenants get the prompt in chat;
            app-only tenants (no Telegram chat id) get it as an app push via
            the same Telegram-independent delivery path.

            `uid` None → legacy unscoped run (no tenant context); the chat id
            then resolves from env via the channel scope."""
            supabase = tenant_aware_client()

            # Check if 3+ notes were logged today
            ist_offset = timezone(timedelta(hours=5, minutes=30))
            now = datetime.now(ist_offset)
            start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0)

            notes_res = supabase.table('memories') \
                .select('id, content') \
                .in_('memory_type', ['note', 'Journal']) \
                .gte('created_at', start_of_day.isoformat()) \
                .execute()

            if notes_res.data:
                text_notes = [n for n in notes_res.data if not n.get('content', '').strip().startswith('http')]
                if len(text_notes) >= 3:
                    return {"tenant": uid, "skipped": True,
                            "message": "Already captured enough notes today. Skipping prompt."}

            roundup_text = "🌆 Evening roundup — any meeting notes, ideas, or project updates from today?"
            chat_id = resolve_telegram_chat_id(uid)
            if chat_id:
                await send_telegram(chat_id, roundup_text)
                return {"tenant": uid, "sent": True, "channel": "telegram",
                        "message": "Roundup prompt sent"}

            # App-only tenant (no Telegram): deliver via the app channel —
            # raw_dumps persist + FCM push — the same Telegram-independent
            # path send_telegram uses internally for its primary channel.
            from core.services.reply_delivery import deliver_outbound_reply
            pushed = await deliver_outbound_reply(roundup_text, notify_push=True)
            return {"tenant": uid, "sent": True, "channel": "app",
                    "devices_pushed": pushed,
                    "message": "Roundup prompt delivered to app"}

        # M4: fan out over all active users. Legacy (no users table / no
        # active users) runs once under the channel scope — the same helper,
        # so the two paths can't drift.
        uids = active_user_ids()
        if not uids:
            from core.services.db import channel_tenant_scope
            with channel_tenant_scope():
                result = await _roundup_for_tenant(None)
            return {"success": True, "results": [result]}

        results = []
        for uid in uids:
            with tenant_scope(uid):
                try:
                    results.append(await _roundup_for_tenant(uid))
                except Exception as tenant_err:
                    print(f"Roundup error for tenant {uid}: {tenant_err}")
                    results.append({"tenant": uid, "error": str(tenant_err)})
        return {"success": True, "tenants": len(uids), "results": results}
    except Exception as e:
        print(f"Roundup error: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")

# --- SEND DRAFT REPLY (Routes to webhook.py) ---
@app.post("/api/send-draft")
async def send_draft_route(request: Request):
    require_api_auth(request)
    body = await request.json()
    draft_id = body.get("draft_id")
    if not draft_id:
        raise HTTPException(status_code=400, detail="draft_id required")
    success, error = await send_draft_reply(draft_id)
    return {"success": success, "error": error}

# --- SEND MESSAGE VIA WEB UI (Mirrors Telegram exactly) ---
async def _run_web_message_pipeline(fake_update: dict, session_id: str | None) -> tuple[str | None, str | None]:
    """Execute the full web-message pipeline (classify → route → reply → push).

    Single source of truth for BOTH the inline fallback path and the Modal
    background worker (process_message_background). Begins its own action
    context because the worker runs in a separate container.

    The reply is delivered to the app two ways (kept from the original path):
      - send_telegram (inside process_webhook) fires an FCM push with the
        reply text — the app's push handler polls conversation history.
      - The briefing rebuild + silent push refreshes the home screen.

    Returns (response_text, resulting_session_id) — used by the inline
    fallback path only (the Modal worker ignores the return value).
    """
    from core.actions import begin_action_context, clear_action_context
    begin_action_context()
    try:
        print("🧪 Processing web message as Telegram update")
        await process_webhook(fake_update)

        from core.actions import get_captured_response, get_captured_session_id
        response_text = get_captured_response()
        resulting_session_id = get_captured_session_id() or session_id

        # ── Briefing rebuild + silent push (off the ack path) ──
        # Shared trigger (core.services.briefing_refresh): invalidates the
        # cache first, rebuilds, repopulates the cache, pushes a silent
        # briefing_refresh — audited and retried once on failure (the old
        # block swallowed errors with a bare print and had no retry).
        await trigger_briefing_refresh(source="send_message")

        return response_text, resulting_session_id
    finally:
        clear_action_context()


@app.post("/api/send-message")
async def send_message_route(request: Request):
    # Capture the resolved tenant BEFORE the fast-ack spawn: the contextvar
    # set here does NOT survive into the background Modal worker (separate
    # process), so we must pass the uid explicitly and re-scope inside
    # process_message_background. Without this, the worker falls back to
    # resolve_channel_tenant() = first active user (Danny) and tenant #2's
    # messages silently run under tenant #1 (real cross-tenant bug).
    uid = require_api_auth(request)
    try:
        body = await request.json()
        message_text = body.get("message")
        if not message_text:
            raise HTTPException(status_code=400, detail="message required")
        
        # Telegram is now an OPTIONAL secondary channel. The app's reply
        # delivery (raw_dumps + FCM push) is Telegram-independent — see
        # core/services/reply_delivery.py — so a missing TELEGRAM_CHAT_ID
        # no longer blocks the app from sending. When present, the pipeline
        # still routes replies to Telegram too (send_telegram handles the
        # graceful skip internally when only the chat id is absent).
        telegram_chat_id = os.getenv("TELEGRAM_CHAT_ID") or "0"
        chat_id = int(telegram_chat_id)
        
        # Create a fake update object (mirrors what Telegram sends when
        # configured; a neutral chat_id keeps thread continuity working
        # in app-only mode). Prefix update_id with "web_" to identify
        # web UI messages. Pass optional session_id for thread continuity.
        session_id = body.get("session_id")
        metadata = {}
        if session_id:
            metadata["session_id"] = session_id
        
        fake_update = {
            "update_id": f"web_{int(time.time() * 1000)}",
            "message": {
                "chat": {"id": chat_id},
                "text": message_text,
                "date": int(time.time())
            },
            "metadata": metadata
        }
        
        supabase = tenant_aware_client()

        # ── Fast-path: vault badge messages skip the full LLM pipeline ──
        # The vault badge tap sends a system message that doesn't need intent
        # classification or Action Planner processing — it's purely for
        # conversation thread continuity. Storing it in raw_dumps is enough.
        is_vault_message = message_text.startswith('📦 Vault items:')

        if is_vault_message:
            # Store the inbound vault message
            supabase.table('raw_dumps').insert({
                'content': message_text,
                'source': 'flutter',
                'direction': 'inbound',
                'message_type': 'text',
                'status': 'processed',
                'sender': 'user',
            }).execute()

            response_text = '📦 Vault items noted. They are tracked and will resurface as deadlines approach. Want to pull any forward? Just ask.'
            resulting_session_id = session_id

            # Store the bot response so poll and history can pick it up
            supabase.table('raw_dumps').insert({
                'content': response_text,
                'source': 'flutter',
                'direction': 'outbound',
                'message_type': 'text',
                'status': 'sent',
                'sender': 'rhodey',
            }).execute()

            return {
                "success": True,
                "message": "Message processed",
                "response": response_text,
                "session_id": resulting_session_id,
            }

        # ── Fast-ack (P3): return instantly, process in a Modal worker ──
        # The full pipeline (intent classify → entity extraction → routing →
        # LLM reply) runs in a dedicated Modal container via
        # process_message_background. The reply reaches the app through the
        # FCM push fired inside send_telegram + the backup poll.
        try:
            import modal
            modal.Function.from_name("rhodey-os", "process_message_background").spawn({
                "fake_update": fake_update,
                "session_id": session_id,
                "uid": uid,
            })
            return {
                "success": True,
                "fast_ack": True,
                "message": "Processing",
                "response": "Got it. Processing...",
                "session_id": session_id,
            }
        except Exception as e:
            print(f"Send-message: background spawn failed ({e}) — falling back to inline")

        # Fallback (local dev / non-Modal): run the full pipeline inline.
        # Note: no uid needed here — this runs IN-PROCESS, so the tenant
        # contextvar set by require_api_auth() above is still active and the
        # pipeline scopes itself correctly. Only the cross-process Modal
        # spawn above needs the explicit uid.
        response_text, resulting_session_id = await _run_web_message_pipeline(fake_update, session_id)
        return {
            "success": True,
            "message": "Message processed",
            "response": response_text or "Got it. Processing...",
            "session_id": resulting_session_id,
        }
    
    except HTTPException:
        raise
    except Exception as e:
        print(f"Send message error: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")

# --- ONBOARDING DEMO (M10) ---
# The demo rides the REAL pipeline (classify → route → reply → persist) so
# the "aha" is genuine — a demo task lands on the board, a demo note links to
# a person, a demo query reads THEIR seeded graph. Artifacts are stamped
# demo-owned so the tenant can clear them (POST /api/demo/cleanup).

_DEMO_STAMP_MARKER = "[onboarding-demo]"


async def _run_demo_message_pipeline(fake_update: dict, session_id: str | None) -> tuple[str | None, str | None]:
    """Execute the REAL web-message pipeline INLINE for an onboarding demo.

    Same core as _run_web_message_pipeline (classify → route → reply) but
    skips the briefing rebuild + silent push — the demo UI shows the reply
    directly, so the extra ~10s rebuild is wasted latency on every tap. The
    reply is still persisted by the pipeline (raw_dumps + conversations), so
    the thread stays real and appears in History.

    Returns (response_text, resulting_session_id).
    """
    from core.actions import begin_action_context, clear_action_context
    begin_action_context()
    try:
        await process_webhook(fake_update)
        from core.actions import get_captured_response, get_captured_session_id
        response_text = get_captured_response()
        resulting_session_id = get_captured_session_id() or session_id
        return response_text, resulting_session_id
    finally:
        clear_action_context()


def _merge_demo_meta(existing) -> dict:
    meta = dict(existing or {})
    meta["demo"] = True
    return meta


def _stamp_demo_artifacts(supabase, message_text: str, window_start_iso: str) -> dict:
    """Stamp artifacts a demo message created as demo-owned.

    Window-scoped recovery tagging (honest limits — read-before-write
    without a lock leaves a small race window, but the demo runs during
    onboarding when no real traffic exists):
      - raw_dumps inbound row whose content == the exact scripted message
      - raw_dumps outbound rows created in the window (the bot's reply)
      - tasks created in the window → notes suffixed [onboarding-demo]
      - memories created in the window → metadata.demo = true
    Graph nodes are intentionally NOT stamped: they enter the normal
    pending-approval flow, and rejecting them in the Inbox is the designed
    training signal.
    Returns counts of stamped rows.
    """
    stamped = {"raw_dumps": 0, "conversations": 0, "tasks": 0, "memories": 0}

    # conversations: the user↔bot thread rows written by log_exchange
    # during the demo turn (inbound user message + bot replies).
    try:
        res = supabase.table("conversations") \
            .select("id, metadata, role, content") \
            .gte("created_at", window_start_iso) \
            .execute()
        for row in res.data or []:
            content = row.get("content") or ""
            if (row.get("role") == "user") and content != message_text:
                continue  # only the exact scripted message
            supabase.table("conversations") \
                .update({"metadata": _merge_demo_meta(row.get("metadata"))}) \
                .eq("id", row["id"]).execute()
            stamped["conversations"] += 1
    except Exception as e:
        audit_log_sync("demo", "WARNING", f"demo stamp conversations: {e}")

    # raw_dumps: exact-content inbound row + window outbound replies
    try:
        res = supabase.table("raw_dumps") \
            .select("id, metadata, direction") \
            .gte("created_at", window_start_iso) \
            .execute()
        for row in res.data or []:
            direction = (row.get("direction") or "").lower()
            content = row.get("content") or ""
            is_inbound = direction in ("incoming", "inbound")
            is_outbound = direction in ("outgoing", "outbound")
            if is_inbound and content != message_text:
                continue  # only the exact scripted message
            if not (is_inbound or is_outbound):
                continue
            supabase.table("raw_dumps") \
                .update({"metadata": _merge_demo_meta(row.get("metadata"))}) \
                .eq("id", row["id"]).execute()
            stamped["raw_dumps"] += 1
    except Exception as e:
        audit_log_sync("demo", "WARNING", f"demo stamp raw_dumps: {e}")

    # tasks: created in the window → notes marker
    try:
        res = supabase.table("tasks") \
            .select("id, notes") \
            .gte("created_at", window_start_iso) \
            .execute()
        for row in res.data or []:
            notes = (row.get("notes") or "")
            if _DEMO_STAMP_MARKER not in notes:
                notes = f"{notes} {_DEMO_STAMP_MARKER}".strip()
            supabase.table("tasks").update({"notes": notes}).eq("id", row["id"]).execute()
            stamped["tasks"] += 1
    except Exception as e:
        audit_log_sync("demo", "WARNING", f"demo stamp tasks: {e}")

    # memories: created in the window → metadata.demo
    try:
        res = supabase.table("memories") \
            .select("id, metadata") \
            .gte("created_at", window_start_iso) \
            .execute()
        for row in res.data or []:
            supabase.table("memories") \
                .update({"metadata": _merge_demo_meta(row.get("metadata"))}) \
                .eq("id", row["id"]).execute()
            stamped["memories"] += 1
    except Exception as e:
        audit_log_sync("demo", "WARNING", f"demo stamp memories: {e}")

    return stamped


@app.post("/api/demo/message")
async def demo_message_route(request: Request):
    """Onboarding demo: run ONE scripted message through the REAL pipeline
    inline, returning the reply synchronously, and stamp the artifacts it
    creates as demo-owned so they're cleanable.

    Stamping is deliberately precise (never tags real user traffic): the
    inbound raw_dumps row is only stamped when its content EXACTLY equals
    the message the app sent through this endpoint, within a 2-second
    window. The seed at journey step 7 already marks the tenant 'seeded'
    BEFORE the demo steps run, so a status gate would wrongly block the
    demo — the exact-content + window match is the real guard.
    """
    require_api_auth(request)
    try:
        body = await request.json()
        message_text = body.get("message")
        if not message_text:
            raise HTTPException(status_code=400, detail="message required")

        telegram_chat_id = os.getenv("TELEGRAM_CHAT_ID") or "0"
        chat_id = int(telegram_chat_id)
        session_id = body.get("session_id")
        metadata = {}
        if session_id:
            metadata["session_id"] = session_id

        fake_update = {
            "update_id": f"web_demo_{int(time.time() * 1000)}",
            "message": {
                "chat": {"id": chat_id},
                "text": message_text,
                "date": int(time.time()),
            },
            "metadata": metadata,
        }

        supabase = tenant_aware_client()
        window_start = (datetime.now(timezone.utc) - timedelta(seconds=2)).isoformat()

        response_text, resulting_session_id = await _run_demo_message_pipeline(fake_update, session_id)

        stamped = _stamp_demo_artifacts(supabase, message_text, window_start)

        return {
            "success": True,
            "response": response_text or "Got it. Processing...",
            "session_id": resulting_session_id,
            "stamped": stamped,
        }
    except HTTPException:
        raise
    except Exception as e:
        print(f"Demo message error: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@app.post("/api/demo/cleanup")
async def demo_cleanup_route(request: Request):
    """Delete the tenant's demo-tagged artifacts (idempotent).

    - raw_dumps rows with metadata.demo = true
    - tasks whose notes contain [onboarding-demo] AND are still 'todo'
      (completed demo tasks stay — their outcome memory is a learning
      signal; deleting it would erase what the demo taught)
    - memories with metadata.demo = true
    Graph nodes are left: they entered the normal pending-approval flow,
    and rejecting them in the Inbox is the designed training signal.
    """
    require_api_auth(request)
    try:
        supabase = tenant_aware_client()
        removed = {"raw_dumps": 0, "conversations": 0, "tasks": 0, "memories": 0}

        try:
            res = supabase.table("raw_dumps").delete() \
                .eq("metadata->>demo", "true").execute()
            removed["raw_dumps"] = len(res.data or [])
        except Exception as e:
            audit_log_sync("demo", "WARNING", f"demo cleanup raw_dumps: {e}")

        try:
            res = supabase.table("conversations").delete() \
                .eq("metadata->>demo", "true").execute()
            removed["conversations"] = len(res.data or [])
        except Exception as e:
            audit_log_sync("demo", "WARNING", f"demo cleanup conversations: {e}")

        try:
            res = supabase.table("tasks").delete() \
                .ilike("notes", f"%{_DEMO_STAMP_MARKER}%") \
                .eq("status", "todo").execute()
            removed["tasks"] = len(res.data or [])
        except Exception as e:
            audit_log_sync("demo", "WARNING", f"demo cleanup tasks: {e}")

        try:
            res = supabase.table("memories").delete() \
                .eq("metadata->>demo", "true").execute()
            removed["memories"] = len(res.data or [])
        except Exception as e:
            audit_log_sync("demo", "WARNING", f"demo cleanup memories: {e}")

        return {"success": True, "removed": removed}
    except Exception as e:
        print(f"Demo cleanup error: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


# --- GET MESSAGE HISTORY ---
@app.get("/api/messages")
async def get_messages_route(request: Request, limit: int = 50, offset: int = 0):
    require_api_auth(request)
    try:
        supabase = tenant_aware_client()
        result = supabase.table('raw_dumps')\
            .select('id, content, created_at, direction, sender, message_type, status, metadata, source')\
            .order('created_at', desc=True)\
            .limit(limit)\
            .offset(offset)\
            .execute()
        rows = result.data or []
        # Flatten structured ack metadata (intent + title) so the app can
        # render cards without parsing the voice-rendered ack text.
        for row in rows:
            meta = row.get('metadata') or {}
            if meta.get('intent'):
                row['intent'] = meta['intent']
            if meta.get('title'):
                row['title'] = meta['title']
        return {"messages": rows}
    except Exception as e:
        print(f"Get messages error: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")

# --- CONVERSATION HISTORY (merged user↔Rhodey thread) ---

def _history_dedup_key(role: str, content: str, created_at) -> str:
    """Dedup key for merged history: role + normalized content + minute bucket.

    Bot outputs are written to BOTH the conversations table (log_exchange) and
    raw_dumps (telegram_bot/pulse/pulse_engine), so rounding created_at to the
    minute and normalizing whitespace collapses those near-identical rows.
    Including the role prevents a user message and a bot reply that happen to
    share identical short text in the same minute from being wrongly collapsed.
    """
    normalized = " ".join((content or "").split())[:200]
    bucket = ""
    if created_at:
        try:
            bucket = str(created_at)[:16]  # "2026-07-31T08:22" — minute granularity
        except Exception:
            bucket = ""
    return f"{role}|{normalized}|{bucket}"


@app.get("/api/conversation-history")
async def conversation_history_route(request: Request, limit: int = 100, offset: int = 0):
    """Return the complete user↔Rhodey conversation, newest-first.

    The `conversations` table holds user exchanges (and some bot exchanges via
    log_exchange), while raw_dumps holds EVERY bot output — task closures,
    query responses, and pulse briefings (sources telegram_bot/pulse/
    pulse_engine). Neither alone is the full chat, so this endpoint merges
    both, dedupes by content+minute, and returns one chronological thread.
    """
    require_api_auth(request)
    try:
        supabase = tenant_aware_client()
        # Fetch a capped window from each table sized to the requested slice,
        # then merge/dedupe in memory and slice. The window must leave headroom
        # over (offset + limit) because content+minute dedup shrinks the merged
        # unique set — but a fixed 2000-row cap made every small read (e.g. the
        # app's tap-to-open with limit=30) fetch 2000 rows from EACH table,
        # which is the source of the tap-to-briefing lag. Scale with the request.
        window = min(max(limit + offset + 300, limit * 4), 5000)

        conv_res = supabase.table('conversations') \
            .select('id, role, intent, content, created_at, session_id, metadata')\
            .order('created_at', desc=True)\
            .limit(window)\
            .execute()

        dump_res = supabase.table('raw_dumps') \
            .select('id, content, created_at, direction, sender, message_type, source, metadata')\
            .in_('direction', ['outgoing', 'outbound'])\
            .order('created_at', desc=True)\
            .limit(window)\
            .execute()

        entries = []
        seen = set()

        # conversations first — preferred because it carries role + intent
        for row in (conv_res.data or []):
            content = row.get('content') or ''
            created = row.get('created_at')
            raw_role = row.get('role') or 'bot'
            role = 'user' if raw_role == 'user' else 'bot'
            key = _history_dedup_key(role, content, created)
            if key in seen:
                continue
            seen.add(key)
            entries.append({
                'id': f"c{row['id']}",
                'role': role,
                'intent': row.get('intent'),
                'content': content,
                'created_at': created,
                'session_id': row.get('session_id'),
                'metadata': row.get('metadata'),
            })

        # raw_dumps bot outputs not already present (task acks, pulses, etc.)
        for row in (dump_res.data or []):
            content = row.get('content') or ''
            created = row.get('created_at')
            key = _history_dedup_key('bot', content, created)
            if key in seen:
                continue
            seen.add(key)
            source = row.get('source') or ''
            meta = row.get('metadata') or {}
            entries.append({
                'id': f"r{row['id']}",
                'role': 'bot',
                'intent': meta.get('intent') or ('BRIEFING' if source in ('pulse', 'pulse_engine') else 'RESPONSE'),
                'content': content,
                'created_at': created,
                'session_id': None,
                'metadata': meta,
                'title': meta.get('title'),
            })

        entries.sort(key=lambda e: e.get('created_at') or '', reverse=True)
        page = entries[offset:offset + limit]
        return {"messages": page}
    except Exception as e:
        print(f"Conversation history error: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


# --- SEARCH SENT EMAILS ---
@app.post("/api/email-search/sent")
async def search_sent_route(request: Request):
    require_api_auth(request)
    try:
        body = await request.json()
        query = body.get("query", "")
        max_results = body.get("max_results", 5)
        
        from core.email_search import search_gmail_sent, search_outlook_sent
        import asyncio
        
        # Run both searches concurrently in threads since they are sync
        g_task = asyncio.to_thread(search_gmail_sent, query, max_results)
        o_task = asyncio.to_thread(search_outlook_sent, query, max_results)
        
        g_res, o_res = await asyncio.gather(g_task, o_task)
        
        # Sort combined results by received_at descending
        combined = g_res + o_res
        combined.sort(key=lambda x: x.get('received_at', ''), reverse=True)
        
        return {"success": True, "results": combined[:max_results]}
    except Exception as e:
        print(f"Sent email search error: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")

# --- CALENDAR EVENTS (Fetches from Google + Outlook) ---
@app.get("/api/calendar-events")
async def get_calendar_events(request: Request, date: str = None, start: str = None, end: str = None):
    require_api_auth(request)
    try:
        if start and end:
            start_dt = datetime.fromisoformat(start).replace(hour=0, minute=0, second=0)
            end_dt = datetime.fromisoformat(end).replace(hour=23, minute=59, second=59)
            rfc_start = format_rfc3339(start_dt)
            rfc_end = format_rfc3339(end_dt)
        elif date == "today" or not date:
            today = datetime.now()
            start_dt = today.replace(hour=0, minute=0, second=0)
            end_dt = start_dt.replace(hour=23, minute=59, second=59)
            rfc_start = format_rfc3339(start_dt)
            rfc_end = format_rfc3339(end_dt)
        else:
            target = datetime.fromisoformat(date)
            start_dt = target.replace(hour=0, minute=0, second=0)
            end_dt = start_dt.replace(hour=23, minute=59, second=59)
            rfc_start = format_rfc3339(start_dt)
            rfc_end = format_rfc3339(end_dt)

        # 5-minute TTL cache: Today's screen must not hit the live Google +
        # Outlook APIs on every open. Fail-open — a cache miss/error just
        # fetches live, exactly as before. Tenant-namespaced: calendar
        # events are tenant data — a global key would serve tenant B
        # tenant A's cached calendar.
        _uid = get_tenant()
        _base = date or (f'{start}|{end}' if start and end else 'today')
        cache_key = f"rhodey:calendar:{_uid}:{_base}" if _uid else f"rhodey:calendar:{_base}"
        from core.lib.redis_cache import cache_get, cache_set
        cached = cache_get(cache_key)
        if cached is not None and isinstance(cached, list):
            return {"events": cached}

        simplified = []

        # Google + Outlook calls are blocking network I/O — run them in a
        # worker thread so a slow provider can't stall the event loop (and
        # every other request queued behind this one).
        def _fetch_google():
            from core.services.google_service import get_cached_service
            service = get_cached_service('calendar', 'v3')
            if service is None:
                return {"items": []}  # tenant has no Google creds (M5)
            return service.events().list(
                calendarId='primary',
                timeMin=rfc_start,
                timeMax=rfc_end,
                singleEvents=True,
                orderBy='startTime',
                maxResults=50
            ).execute()

        events_res = await asyncio.to_thread(_fetch_google)
        for event in events_res.get('items', []):
            simplified.append({
                'id': event.get('id'),
                'summary': event.get('summary', 'No Title'),
                'start': event.get('start', {}),
                'end': event.get('end', {}),
                'description': event.get('description', ''),
                'source': 'google',
            })

        try:
            if start and end:
                outlook_events = await asyncio.to_thread(
                    get_outlook_calendar_events_range, start_dt, end_dt
                )
            else:
                outlook_events = await asyncio.to_thread(
                    get_outlook_calendar_events, start_dt
                )
            for e in outlook_events:
                simplified.append({
                    'id': e.get('id'),
                    'summary': e.get('title'),
                    'start': {'dateTime': e['time']},
                    'source': 'outlook',
                })
        except Exception as ol_err:
            print(f"Outlook calendar events error: {ol_err}")

        # Store the simplified list (json-serializable) with a 5-min TTL.
        cache_set(cache_key, simplified, ttl=300)

        return {"events": simplified}
    except Exception as e:
        print(f"Calendar events error: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")

# --- SHARED TASK COMPLETION LOGIC (used by both PATCH and focal-action) ---
async def _complete_task(task_id: int, new_status: str = "done") -> dict:
    """Core task completion logic: handles recurring, Google sync, outcome memory, caches.

    Returns a dict with 'success' and 'message' keys. On success also includes 'task'.
    This is the shared implementation used by both PATCH /api/tasks/{id}/status
    and POST /api/focal-action (done action).
    """
    supabase = tenant_aware_client()

    task_res = supabase.table('tasks').select('*').eq('id', task_id).eq('is_current', True).single().execute()
    if not task_res.data:
        return {"success": False, "message": "Task not found"}

    task = task_res.data
    current_status = task.get('status')
    if current_status == new_status:
        return {"success": True, "task": task, "message": f"Task already {current_status}"}
    if current_status == 'cancelled':
        return {"success": False, "message": "Task was cancelled — cannot change status"}

    # --- RECURRING TASK: done = skip instance, cancelled = end series ---
    if task.get('recurrence') not in [None, '', 'none'] and new_status == 'done':
        skip_msg = ""
        if task.get('google_event_id'):
            skip_msg = skip_recurring_instance(task_id)
        else:
            skip_msg = "No linked calendar event."
        await write_outcome_memory(task.get('title', 'Untitled Task'))
        # A completed instance is still a state change — refresh the live
        # briefing so the focal card/voice line catch up (same guarantee as
        # the regular completion path below).
        fire_briefing_refresh(source="task_status_change")
        return {"success": True, "task": task, "message": f"Marked this week's instance done. {skip_msg} Series continues."}

    g_id = task.get('google_task_id')
    e_id = task.get('google_event_id')
    task_title = task.get('title', 'Untitled Task')

    if e_id and new_status in ['done', 'cancelled']:
        try:
            delete_calendar_event(e_id)
        except Exception as e:
            print(f"Calendar event delete failed (non-critical): {e}")

    if g_id and new_status in ['done', 'cancelled']:
        try:
            tasks_service = get_tasks_service()
            sync_to_google(tasks_service, title=task_title, task_id=g_id, status=new_status)
        except Exception as e:
            print(f"Google Tasks sync failed (non-critical): {e}")

    update_data = {'status': new_status}
    if new_status == 'done':
        update_data['completed_at'] = datetime.now().isoformat()
        # M12: completing the task resets its focal snooze escalation
        # counter — the next "Not now" starts the ladder over at 1 day.
        update_data['snooze_count'] = 0
        update_data['snooze_feedback'] = None

    supabase.table('tasks').update(update_data).eq('id', task_id).execute()

    if new_status == 'done':
        org_name = None
        org_id = task.get('organization_id')
        if org_id:
            org_lookup = maybe_single_safe(supabase.table('graph_nodes').select('label').eq('id', org_id))
            org_name = org_lookup.data['label'] if org_lookup.data else None
        await write_outcome_memory(task_title, org_name)

    # Invalidate task caches
    try:
        from core.pulse.context import context_provider
        context_provider.caches['tasks'].invalidate()
        context_provider.caches['recent_tasks'].invalidate()
    except Exception:
        pass

    # Invalidate the home-feed briefing cache so the next request returns a
    # briefing that no longer names the completed task. Without this, the
    # cached briefing (2-min TTL) keeps serving the completed task as its
    # top_focal_item / voice-line subject — the server-side half of the
    # "completed task still showing on the focal card" ghost.
    #
    # Keep this even though fire_briefing_refresh() also invalidates: when a
    # refresh is DEBOUNCED (a rebuild happened <2min ago), fire only nudges
    # and skips invalidation — this synchronous delete is what still
    # guarantees the next fetch rebuilds fresh for this completion.
    try:
        from core.lib.redis_cache import cache_delete
        cache_delete(briefing_cache_key())
    except Exception:
        pass

    new_task_res = supabase.table('tasks').select('*').eq('supersedes_id', task_id).eq('is_current', True).single().execute()
    new_task = new_task_res.data if new_task_res.data else task

    # Fire the shared live-briefing refresh (background, off the ack path):
    # rebuild + cache invalidation + briefing_refresh push so the app catches
    # up immediately. Aug-11 fix — app-side completions (PATCH status /
    # focal-action) never triggered the rebuild before; only send-message
    # did. The cache invalidation above guarantees the next fetch is fresh
    # even if the background task dies with the container.
    fire_briefing_refresh(source="task_status_change")

    return {"success": True, "task": new_task}


# --- UPDATE TASK STATUS (Mark Done) ---
@app.patch("/api/tasks/{task_id}/status")
async def update_task_status(request: Request, task_id: int):
    require_api_auth(request)
    try:
        body = await request.json()
        new_status = body.get('status', 'done')
        result = await _complete_task(task_id, new_status)
        if not result['success']:
            raise HTTPException(status_code=400 if 'cannot' in result.get('message', '') else 404, detail=result['message'])
        return result
    except HTTPException:
        raise
    except Exception as e:
        print(f"Update task status error: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")

# --- ALIAS MANAGEMENT ---
@app.get("/api/aliases")
async def list_aliases_route(request: Request):
    """List all person aliases.

    Migration 76: aliases live on graph_nodes.metadata.aliases. The response
    keeps the legacy shape ({id, alias, canonical_name, resolution_count}) so
    clients are unchanged — id is now the NODE UUID and delete requires the
    alias text.
    """
    require_api_auth(request)
    try:
        supabase = tenant_aware_client()
        result = supabase.table('graph_nodes') \
            .select('id, label, metadata') \
            .eq('type', 'person') \
            .eq('is_current', True) \
            .execute()
        aliases = []
        seen = set()
        for n in (result.data or []):
            meta = n.get('metadata') or {}
            if isinstance(meta, str):
                import json as _j
                try:
                    meta = _j.loads(meta)
                except Exception:
                    meta = {}
            usage = meta.get('alias_usage') or {}
            for a in (meta.get('aliases') or []):
                a_str = str(a).strip()
                key = a_str.lower()
                if not a_str or key in seen:
                    continue
                seen.add(key)
                aliases.append({
                    'id': n['id'],
                    'alias': a_str,
                    'canonical_name': n.get('label', ''),
                    'resolution_count': int(usage.get(key, 0) or 0),
                })
        aliases.sort(key=lambda x: x['alias'].lower())
        return {"aliases": aliases}
    except Exception as e:
        print(f"List aliases error: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


def _invalidate_alias_caches():
    """Drop in-memory alias caches in graph_rules so edits take effect now."""
    import sys as _sys
    gr = _sys.modules.get('core.lib.graph_rules')
    if gr is not None:
        gr._alias_cache = None
        gr._person_index_cache = None
        gr._user_node_cache = None


def _find_person_node_by_label(canonical_name: str) -> dict | None:
    """Resolve a canonical name to a live person node (exact label, then chain)."""
    supabase = tenant_aware_client()
    res = supabase.table('graph_nodes').select('id, label, metadata') \
        .eq('type', 'person').eq('is_current', True) \
        .ilike('label', canonical_name).limit(1).execute()
    if res and res.data:
        return res.data[0]
    # archived label -> canonical chain
    arch = supabase.table('graph_nodes').select('id, label, canonical_id, metadata') \
        .eq('type', 'person').ilike('label', canonical_name).limit(1).execute()
    if arch and arch.data and arch.data[0].get('canonical_id'):
        cid = arch.data[0]['canonical_id']
        cnode = supabase.table('graph_nodes').select('id, label, metadata') \
            .eq('id', cid).eq('is_current', True).limit(1).execute()
        if cnode and cnode.data:
            return cnode.data[0]
    return None


def _read_node_meta(node: dict) -> dict:
    meta = node.get('metadata') or {}
    if isinstance(meta, str):
        import json as _j
        try:
            meta = _j.loads(meta)
        except Exception:
            meta = {}
    return meta


def _alias_payload(node: dict, alias: str) -> dict:
    meta = _read_node_meta(node)
    usage = meta.get('alias_usage') or {}
    return {
        'id': node['id'],
        'alias': alias,
        'canonical_name': node.get('label', ''),
        'resolution_count': int(usage.get(alias.lower(), 0) or 0),
    }


@app.post("/api/aliases")
async def create_alias_route(request: Request):
    """Create a new person alias (alias -> canonical_name) on the person node."""
    require_api_auth(request)
    try:
        body = await request.json()
        alias = (body.get('alias') or '').strip()
        canonical_name = (body.get('canonical_name') or '').strip()

        if not alias or not canonical_name:
            raise HTTPException(status_code=400, detail="alias and canonical_name required")
        if alias.lower() == canonical_name.lower():
            raise HTTPException(status_code=400, detail="alias and canonical_name must be different")

        supabase = tenant_aware_client()
        node = _find_person_node_by_label(canonical_name)
        if not node:
            return {"success": False, "message": f"No person node found for '{canonical_name}'"}

        meta = _read_node_meta(node)
        aliases = [str(a).strip() for a in (meta.get('aliases') or []) if str(a).strip()]
        key = alias.lower()
        if any(str(a).lower() == key for a in aliases):
            return {"success": False, "message": f"Alias '{alias}' already exists."}

        aliases.append(alias)
        supabase.table('graph_nodes').update({
            'metadata': {**meta, 'aliases': aliases},
        }).eq('id', node['id']).execute()
        _invalidate_alias_caches()
        return {"success": True, "alias": _alias_payload(node, alias)}
    except HTTPException:
        raise
    except Exception as e:
        print(f"Create alias error: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@app.delete("/api/aliases")
async def delete_alias_route(request: Request):
    """Delete an alias from a person node. Body: {alias, canonical_name}."""
    require_api_auth(request)
    try:
        body = await request.json()
        alias = (body.get('alias') or '').strip()
        canonical_name = (body.get('canonical_name') or '').strip()
        if not alias:
            raise HTTPException(status_code=400, detail="alias required")

        supabase = tenant_aware_client()
        node = _find_person_node_by_label(canonical_name or "")
        if not node:
            # Try by node id if passed instead of a name
            nid = (body.get('node_id') or '').strip()
            if nid:
                res = supabase.table('graph_nodes').select('id, label, metadata') \
                    .eq('id', nid).limit(1).execute()
                if res and res.data:
                    node = res.data[0]
        if not node:
            raise HTTPException(status_code=404, detail="Person node not found")

        meta = _read_node_meta(node)
        aliases = [str(a).strip() for a in (meta.get('aliases') or []) if str(a).strip()]
        key = alias.lower()
        remaining = [a for a in aliases if a.lower() != key]
        if len(remaining) == len(aliases):
            return {"success": False, "message": f"Alias '{alias}' not found on this person"}

        usage = dict(meta.get('alias_usage') or {})
        usage.pop(key, None)
        new_meta = {**meta, 'aliases': remaining}
        if usage:
            new_meta['alias_usage'] = usage
        else:
            new_meta.pop('alias_usage', None)
        supabase.table('graph_nodes').update({'metadata': new_meta}).eq('id', node['id']).execute()
        _invalidate_alias_caches()
        return {"success": True, "message": f"Deleted alias '{alias}'"}
    except HTTPException:
        raise
    except Exception as e:
        print(f"Delete alias error: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@app.get("/api/people/{person_id}/tasks")
async def person_tasks_route(person_id: str, request: Request):
    """Open tasks that mention a person's name — mirrors the dashboard's
    person-detail active-tasks section."""
    require_api_auth(request)
    try:
        name = request.query_params.get('name', '').strip()
        supabase = tenant_aware_client()

        # NOTE: requires migration 75 (tasks.organization_id -> graph_nodes).
        org_join = 'graph_nodes(label)'
        org_key = 'graph_nodes'
        org_label_field = 'label'

        if name:
            tasks_res = supabase.table('tasks') \
                .select(f'id, title, status, priority, reminder_at, deadline, created_at, organization_id, {org_join}') \
                .ilike('title', f'%{name}%') \
                .eq('is_current', True) \
                .not_.in_('status', ('done', 'cancelled')) \
                .order('created_at', desc=True) \
                .limit(100) \
                .execute()
        else:
            # Fallback: tasks linked to the person's org if no name given.
            # person_id IS the graph node UUID (migration 75).
            person_res = maybe_single_safe(
                supabase.table('graph_nodes').select('metadata').eq('type', 'person').eq('is_current', True).eq('id', person_id)
            )
            org_name = None
            if person_res and person_res.data:
                meta = person_res.data.get('metadata') or {}
                if isinstance(meta, str):
                    try:
                        meta = json.loads(meta)
                    except Exception:
                        meta = {}
                org_name = (meta.get('enrichment') or {}).get('organization_name')
            if not org_name:
                return []
            tasks_res = supabase.table('tasks') \
                .select(f'id, title, status, priority, reminder_at, deadline, created_at, organization_id, {org_join}') \
                .ilike('title', f'%{org_name}%') \
                .eq('is_current', True) \
                .not_.in_('status', ('done', 'cancelled')) \
                .order('created_at', desc=True) \
                .limit(100) \
                .execute()

        tasks = tasks_res.data or []
        result = []
        for t in tasks:
            org = t.get(org_key) or {}
            result.append({
                'id': t.get('id'),
                'title': t.get('title'),
                'status': t.get('status'),
                'priority': t.get('priority'),
                'reminder_at': t.get('reminder_at'),
                'deadline': t.get('deadline'),
                'created_at': t.get('created_at'),
                'organization_id': t.get('organization_id'),
                'organization_name': org.get(org_label_field) if isinstance(org, dict) else None,
            })
        return result
    except Exception:
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail="Internal server error")



# --- Shared merge-reject (keep both) ---

async def _reject_merge_proposal(supabase, merge_proposal_id: int) -> dict:
    """Reject a node merge proposal: keep both — promote the pending label
    to its own live node and resolve the proposal as rejected.

    Shared by /api/graph-merge-action and /api/focal-action (reject) so the
    focal card's "Reject" behaves identically to the Inbox's.
    """
    from core.services.db import maybe_single_safe
    from core.lib.node_tables import resolve_merge_proposal
    from core.pulse.graph import create_graph_node_with_db_record

    mp_res = maybe_single_safe(supabase.table('merge_proposals').select('*').eq('id', merge_proposal_id))
    if not mp_res or not mp_res.data:
        return {"success": False, "message": "Merge proposal not found."}
    merge_proposal = mp_res.data
    if merge_proposal.get('status') != 'proposed':
        return {"success": False, "message": "Merge proposal already processed."}

    origin_id = merge_proposal.get('origin_id')
    pending_label = merge_proposal.get('source_label', '')
    pending_type = merge_proposal.get('source_type', 'person')

    result = await create_graph_node_with_db_record(
        label=pending_label,
        node_type=pending_type,
        source_text='',
        source_tag='pending_approval',
        force=True,
    )
    if not result.get('success'):
        return {"success": False, "message": result.get('message', 'Failed to approve node')}

    if origin_id:
        supabase.table('pending_nodes').update({'status': 'approved'}).eq('id', origin_id).execute()
    resolve_merge_proposal(merge_proposal['id'], "rejected")
    try:
        record_decision(
            decision_type="graph_node_merge_rejection",
            title=f"Keep both: '{pending_label}' as separate node",
            entity_type="graph_node", entity_id=str(origin_id),
            confidence=1.0, source="web_ui",
        )
    except Exception:
        pass
    try:
        await emit_observation(
            subsystem='entity_extraction', event_type='correction',
            features={"action": "reject_merge", "source_label": pending_label},
            predicted="merge", actual="keep_separate",
            outcome='corrected', source='web_ui',
        )
    except Exception:
        pass
    fire_briefing_refresh(source="merge_reject")
    return {"success": True, "message": f"Keep both — approved '{pending_label}' as separate node."}


# --- Shared merge-accept (combine into canonical) ---

async def _accept_merge_proposal(supabase, merge_proposal_id: int, swap: bool = False) -> dict:
    """Accept a node merge proposal: combine source into target (or swapped).

    Shared by /api/graph-merge-action and /api/focal-action (done) so the
    focal card's "Approve" behaves identically to the Inbox's approve.
    """
    from core.services.db import maybe_single_safe
    from core.lib.node_tables import resolve_merge_proposal

    mp_res = maybe_single_safe(supabase.table('merge_proposals').select('*').eq('id', merge_proposal_id))
    if not mp_res or not mp_res.data:
        return {"success": False, "message": "Merge proposal not found."}
    merge_proposal = mp_res.data
    if merge_proposal.get('status') != 'proposed':
        return {"success": False, "message": "Merge proposal already processed."}

    origin_id = merge_proposal.get('origin_id')
    pending_label = merge_proposal.get('source_label', '')

    target_id = merge_proposal.get('target_node_id')
    if not target_id:
        return {"success": False, "message": "Merge candidate not found in proposal."}

    from core.lib.graph_rules import get_canonical_id, execute_graph_node_merge

    source_node_res = maybe_single_safe(supabase.table('graph_nodes').select('id, label').eq('label', pending_label).eq('is_current', True))
    source_node_id = source_node_res.data['id'] if source_node_res and source_node_res.data else None
    target_canonical = get_canonical_id(target_id)

    if not source_node_id:
        # Pending label was merged before it was ever created as a graph node.
        if origin_id:
            supabase.table('pending_nodes').update({'status': 'approved'}).eq('id', origin_id).execute()
        resolve_merge_proposal(merge_proposal['id'], "accepted")
        try:
            record_decision(decision_type="graph_node_merge",
                            title=f"Aliased pending '{pending_label}' to target",
                            entity_type="graph_node", entity_id=str(origin_id),
                            confidence=1.0, source="web_ui")
        except Exception:
            pass
        try:
            await emit_observation(subsystem='entity_extraction', event_type='correction',
                                   features={"action": "alias_merge", "source_label": pending_label},
                                   predicted=pending_label, actual="aliased",
                                   outcome='corrected', source='web_ui')
        except Exception:
            pass
        fire_briefing_refresh(source="merge_accept")
        return {"success": True, "message": f"Pending label '{pending_label}' is now aliased to the target node."}

    loser_id = target_canonical if swap else source_node_id
    winner_id = source_node_id if swap else target_canonical

    execute_graph_node_merge(loser_id, winner_id, "ui_merge_accept")

    if origin_id:
        supabase.table('pending_nodes').update({'status': 'approved'}).eq('id', origin_id).execute()
    resolve_merge_proposal(merge_proposal['id'], "accepted")

    # Learner feedback
    try:
        record_decision(decision_type="graph_node_merge",
                        title=f"Merged '{pending_label}' into canonical node",
                        entity_type="graph_node", entity_id=str(origin_id),
                        confidence=1.0, source="web_ui")
    except Exception:
        pass
    try:
        await emit_observation(subsystem='entity_extraction', event_type='correction',
                               features={"action": "accept_merge", "source_label": pending_label},
                               predicted=pending_label, actual="merged",
                               outcome='corrected', source='web_ui')
    except Exception:
        pass

    fire_briefing_refresh(source="merge_accept")
    return {"success": True, "message": f"Merged '{pending_label}' into canonical node."}


# --- FOCAL ITEM ACTION (Phase 2 v2: done/snooze/correct/reject) ---
@app.post("/api/focal-action")
async def focal_action_route(request: Request):
    """Handle the three-button focal card actions.

    Body: {
        "action": "done",       // "done", "commit", "snooze", "correct"
        "item_type": "task",    // "task", "graph_node", "graph_edge", "merge"
        "item_id": "123",       // Task ID or pending item ID
        "title": "Fill DBS forms",
        "reason": "Blocking Qhord"   // Original LLM reason (for learning)
    }

    - "done":    Marks the task/decision as completed
    - "snooze":  Deferral with an escalation ladder (1 day -> 3 days -> 7
                  days; the 3rd tap warns and captures feedback as a
                  learning signal). Accepts dry_run=true to preview the
                  ladder position without persisting.
    - "correct": Same 7-day deferral + correction signal to the learning
                  loop.
    """
    require_api_auth(request)
    try:
        body = await request.json()
        action = body.get("action", "")
        item_type = body.get("item_type", "")
        item_id = body.get("item_id", "")
        title = body.get("title", "")
        reason = body.get("reason", "")

        if not action or not item_type or not item_id:
            raise HTTPException(status_code=400, detail="action, item_type, and item_id required")

        # LLM-provided item ids are strings, and by the time Danny taps a
        # button the item may already be resolved — or the id may not be a
        # plain integer at all (a hallucinated / UUID id for an
        # already-handled item). Never crash on that: an unparseable id is an
        # unactionable item, reported honestly instead of a 500.
        def _item_int() -> int | None:
            try:
                return int(item_id)
            except (TypeError, ValueError):
                return None

        # Honest failure payload for an item that can no longer be acted on.
        _unactionable = {"success": False, "message": "I couldn't complete that item — it may have changed."}
        _unactionable_reject = {"success": False, "message": "I couldn't reject that item — it may have changed."}

        # ── Focal snooze escalation ladder (M12) ──────────────────────────
        # 1st "Not now" → 1 day · 2nd → 3 days · 3rd → 7 days behind a
        # warning + feedback gate · 4th+ → 7 days (cap, quiet). The counter
        # lives on the item (snooze_count, db/92) and resets only when the
        # item is completed — not on deferral expiry.
        _ITEM_TABLE = {
            "task": "tasks",
            "graph_node": "pending_nodes",
            "graph_edge": "pending_graph_edges",
            "merge": "merge_proposals",
        }

        def _snooze_days_for_count(count: int) -> int:
            if count <= 1:
                return 1
            if count == 2:
                return 3
            return 7

        _SNOOZE_LADDER_OK: dict[str, bool] = {}

        async def _ladder_ok(item_type: str) -> bool:
            """True when the item's table has the db/92 ladder columns (cached).

            Mirrors the _snooze_ok probe idiom (api/briefing.py): before db/92
            is applied, the ladder is unavailable and snooze must fall back to
            the flat 7-day path instead of failing the deferral outright.
            """
            table = _ITEM_TABLE.get(item_type)
            if not table:
                return False
            if table in _SNOOZE_LADDER_OK:
                return _SNOOZE_LADDER_OK[table]
            try:
                supabase = tenant_aware_client()
                supabase.table(table).select('snooze_count').limit(1).execute()
                _SNOOZE_LADDER_OK[table] = True
            except Exception:
                _SNOOZE_LADDER_OK[table] = False
            return _SNOOZE_LADDER_OK[table]

        async def _read_snooze_count(item_type: str, iid: int | None) -> int | None:
            """Current snooze_count for an item (None = unreadable/unknown)."""
            if iid is None:
                return None
            table = _ITEM_TABLE.get(item_type)
            if not table:
                return None
            try:
                supabase = tenant_aware_client()
                q = supabase.table(table).select('snooze_count').eq('id', iid)
                if table == 'tasks':
                    q = q.eq('is_current', True)
                res = q.limit(1).execute()
                rows = res.data or []
                if not rows:
                    return None
                return int(rows[0].get('snooze_count') or 0)
            except Exception as e:
                print(f"Focal snooze count read error: {e}")
                return None

        async def _persist_ladder_deferral(item_type: str, iid: int | None, feedback: str) -> dict:
            """Persist an escalation-ladder deferral and bump the counter.

            Falls back to the legacy flat 7-day deferral (no count bump) when
            the db/92 ladder columns aren't live yet, so snooze never breaks
            between code deploy and migration apply.
            """
            if iid is None:
                return {"persisted": False, "count": 0, "days": 0, "ladder": False}
            table = _ITEM_TABLE.get(item_type)
            if not table:
                return {"persisted": False, "count": 0, "days": 0, "ladder": False}
            if not await _ladder_ok(item_type):
                # Pre-db/92: legacy flat 7-day path (matches old behavior).
                try:
                    defer_until = (datetime.now(timezone.utc) + timedelta(days=7)).isoformat()
                    supabase = tenant_aware_client()
                    q = supabase.table(table).update({'snoozed_until': defer_until}).eq('id', iid)
                    if table == 'tasks':
                        q = q.eq('is_current', True)
                    res = q.execute()
                    return {"persisted": bool(res.data), "count": 0, "days": 7, "ladder": False}
                except Exception as e:
                    print(f"Focal fallback deferral error: {e}")
                    return {"persisted": False, "count": 0, "days": 0, "ladder": False}
            try:
                cur = await _read_snooze_count(item_type, iid)
                if cur is None:
                    return {"persisted": False, "count": 0, "days": 0, "ladder": True}
                nxt = cur + 1
                days = _snooze_days_for_count(nxt)
                defer_until = (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()
                update = {'snoozed_until': defer_until, 'snooze_count': nxt}
                if feedback:
                    update['snooze_feedback'] = feedback
                supabase = tenant_aware_client()
                q = supabase.table(table).update(update).eq('id', iid)
                if table == 'tasks':
                    q = q.eq('is_current', True)
                res = q.execute()
                return {"persisted": bool(res.data), "count": nxt, "days": days, "ladder": True}
            except Exception as e:
                print(f"Focal ladder deferral persist error: {e}")
                return {"persisted": False, "count": 0, "days": 0, "ladder": True}

        # Shared: persist a 7-day deferral so a snoozed/corrected item stops
        # resurfacing until the deferral expires (see db/72_focal_snooze.sql).
        # Returns True only if the row was actually updated — the caller must
        # surface a failure instead of telling the user the item was dismissed.
        async def _persist_deferral() -> bool:
            try:
                supabase = tenant_aware_client()
                iid = _item_int()
                if iid is None:
                    return False
                defer_until = (datetime.now(timezone.utc) + timedelta(days=7)).isoformat()
                if item_type == "task":
                    res = supabase.table('tasks').update({'snoozed_until': defer_until})\
                        .eq('id', iid).eq('is_current', True).execute()
                    return bool(res.data)
                elif item_type == "graph_node":
                    res = supabase.table('pending_nodes').update({'snoozed_until': defer_until})\
                        .eq('id', iid).execute()
                    return bool(res.data)
                elif item_type == "graph_edge":
                    res = supabase.table('pending_graph_edges').update({'snoozed_until': defer_until})\
                        .eq('id', iid).execute()
                    return bool(res.data)
                elif item_type == "merge":
                    res = supabase.table('merge_proposals').update({'snoozed_until': defer_until})\
                        .eq('id', iid).execute()
                    return bool(res.data)
                return False  # Unknown item type — nothing deferred
            except Exception as e:
                print(f"Focal deferral persist error: {e}")
                return False

        # ── Phase 2B: persona-toned confirmations (R2 single composer home) ──
        # persona_surface_summary is fail-closed (None without a card), so
        # every composed message degrades to today's exact neutral string.
        from core.services import message_voice
        from core.services.persona import persona_surface_summary
        _persona_summary = persona_surface_summary()

        if action == "commit":
            # "I'll do it" — a commitment, NOT a completion. Flip the task to
            # in_progress: no completed_at, no outcome memory, no calendar
            # delete. The task stays on the user's board until they actually
            # finish it (a later 'done' closes it for real).
            if item_type != "task":
                return {"success": False, "message": "Commit only applies to tasks."}
            iid = _item_int()
            if iid is None:
                return _unactionable
            return await _complete_task(iid, "in_progress")

        if action == "done":
            if item_type == "task":
                iid = _item_int()
                if iid is None:
                    return _unactionable
                # Use the shared _complete_task() which handles Google sync, outcome memory, etc.
                return await _complete_task(iid, "done")
            elif item_type == "merge":
                iid = _item_int()
                if iid is None:
                    return _unactionable
                # Accept the merge — shared helper, identical to the Inbox's approve.
                supabase = tenant_aware_client()
                return await _accept_merge_proposal(supabase, iid)
            elif item_type in ("graph_node", "graph_edge"):
                iid = _item_int()
                if iid is None:
                    return _unactionable
                from core.pulse.graph import process_pending_edge_decision, process_graph_pending_decision
                supabase = tenant_aware_client()
                try:
                    if item_type == "graph_edge":
                        res = await process_pending_edge_decision(iid, "approve", auto_decided=False)
                    else:
                        res = await process_graph_pending_decision(iid, "approve", auto_decided=False)
                except Exception as branch_err:
                    print(f"Focal graph decision error: {branch_err}")
                    return _unactionable
                # Respect the real outcome — the pulse may have already
                # auto-approved this row before Danny tapped the button.
                if not res.get("success", False):
                    return res
                return {"success": True, "message": f"Approved {item_type}"}
            elif item_type in ("email", "whatsapp", "teams", "call"):
                # Briefing-promoted pending channel suggestion — approving
                # routes through the SAME handlers as the Inbox's approve
                # button, so the result (task created) and learning signals
                # are identical no matter where the tap happened.
                iid = _item_int()
                if iid is None:
                    return _unactionable
                try:
                    # Both handlers are already imported at module top (via
                    # `from core.webhook import ...`) — same as the Inbox.
                    if item_type == "email":
                        res = await process_email_pending_decision(
                            iid, "approve", auto_decided=False
                        )
                    else:
                        res = await process_channel_pending_decision(
                            item_type, iid, "approve", auto_decided=False
                        )
                except Exception as branch_err:
                    print(f"Focal channel approval error: {branch_err}")
                    return _unactionable
                if not res.get("success", False):
                    return res
                return {"success": True, "message": f"Approved {item_type} task"}
            return {
                "success": True,
                "message": message_voice.compose_done(_persona_summary),
            }

        elif action == "reject":
            # Permanent rejection for graph nodes/edges/merges — mirrors the
            # Inbox's "Reject". Tasks have no reject semantics; the frontend
            # keeps "Not right" (correction) for tasks.
            if item_type == "graph_node":
                iid = _item_int()
                if iid is None:
                    return _unactionable_reject
                from core.pulse.graph import process_graph_pending_decision
                try:
                    return await process_graph_pending_decision(iid, "reject", auto_decided=False)
                except Exception as branch_err:
                    print(f"Focal reject error: {branch_err}")
                    return _unactionable_reject
            elif item_type == "graph_edge":
                iid = _item_int()
                if iid is None:
                    return _unactionable_reject
                from core.pulse.graph import process_pending_edge_decision
                try:
                    return await process_pending_edge_decision(iid, "reject", auto_decided=False)
                except Exception as branch_err:
                    print(f"Focal reject error: {branch_err}")
                    return _unactionable_reject
            elif item_type == "merge":
                iid = _item_int()
                if iid is None:
                    return _unactionable_reject
                supabase = tenant_aware_client()
                return await _reject_merge_proposal(supabase, iid)
            return {"success": True, "message": "Action completed"}

        elif action == "snooze":
            # Escalation ladder: 1 day → 3 days → 7 days (warning on the 3rd
            # tap) → 7 days cap. The app calls once with dry_run=true to learn
            # whether the 3rd-tap warning gate is up WITHOUT persisting; the
            # real call then persists the deferral and bumps snooze_count.
            # Anything else ("correct") keeps the flat 7-day path above.
            dry_run = bool(body.get("dry_run", False))
            feedback = (body.get("feedback") or "").strip()[:500]
            iid = _item_int()

            if dry_run:
                if not await _ladder_ok(item_type):
                    # Pre-db/92 — no gate; the app should just snooze flat.
                    return {
                        "success": True,
                        "dry_run": True,
                        "count": 0,
                        "warn": False,
                        "days": 7,
                        "ladder": False,
                    }
                cur = await _read_snooze_count(item_type, iid)
                if cur is None:
                    return {"success": False, "message": "Couldn't check this item — please try again."}
                nxt = cur + 1
                return {
                    "success": True,
                    "dry_run": True,
                    "count": nxt,
                    "warn": nxt == 3,
                    "days": _snooze_days_for_count(nxt),
                    "ladder": True,
                }

            result = await _persist_ladder_deferral(item_type, iid, feedback)
            if not result.get("persisted"):
                return {"success": False, "message": "Couldn't defer this item — please try again."}

            # The board changed (item hidden behind a deferral) — refresh the
            # live briefing so the focal card/voice line catch up immediately.
            fire_briefing_refresh(source="focal_snooze")

            # 3rd-tap feedback is a learning signal: store it AND feed it to
            # the observation + decision loop so the "why" actually trains
            # the OS (the "snooze without learning" anti-pattern).
            if result.get("count", 0) == 3 and feedback:
                try:
                    await emit_observation(
                        subsystem='focal_selection',
                        event_type='snooze_feedback',
                        features={
                            "item_type": item_type,
                            "item_id": item_id,
                            "title": title,
                            "reason": reason,
                            "feedback": feedback,
                        },
                        predicted=item_type,
                        actual='snoozed',
                        outcome='deferred_with_feedback',
                        source='flutter',
                    )
                    try:
                        await record_decision(
                            decision_type="focal_snooze_feedback",
                            title=f"User snoozed '{title}' for the 3rd time",
                            context=f"Snooze feedback: {feedback}. LLM reason: {reason[:200] if reason else 'N/A'}",
                            entity_type=item_type,
                            entity_id=str(item_id),
                            confidence=1.0,
                            source="flutter",
                            auto_decided=False,
                        )
                    except Exception as dec_err:
                        print(f"Focal snooze feedback decision record error: {dec_err}")
                except Exception as obs_err:
                    print(f"Focal snooze feedback observation error (non-fatal): {obs_err}")

            return {
                "success": True,
                "count": result.get("count", 0),
                "warn": result.get("count", 0) == 3,
                "message": message_voice.compose_snoozed(
                    _persona_summary,
                    _snooze_days_for_count(result.get("count", 0)),
                ),
            }

        elif action == "correct":
            # Defer the item too (so a corrected focal pick doesn't resurface)
            # AND emit a correction signal for the learning loop. If the
            # deferral fails, still record the correction but be honest about it.
            persisted = await _persist_deferral()
            if persisted:
                fire_briefing_refresh(source="focal_correct")
            # Correction: emit observation for learning loop
            try:
                await emit_observation(
                    subsystem='focal_selection',
                    event_type='correction',
                    features={
                        "item_type": item_type,
                        "item_id": item_id,
                        "title": title,
                        "reason": reason,
                    },
                    predicted=item_type,
                    actual='rejected',
                    outcome='corrected',
                    source='flutter',
                )
                try:
                    await record_decision(
                        decision_type="focal_selection_correction",
                        title=f"User corrected focal selection: '{title}'",
                        context=f"User tapped 'Not right' on focal item. LLM reason: {reason[:200] if reason else 'N/A'}",
                        entity_type=item_type,
                        entity_id=str(item_id),
                        confidence=1.0,
                        source="flutter",
                        auto_decided=False,
                    )
                except Exception as dec_err:
                    print(f"Focal correction decision record error: {dec_err}")
            except Exception as e:
                print(f"Focal correction observation error (non-fatal): {e}")
            if not persisted:
                # Channel suggestions have no snooze columns — the correction
                # signal above was still recorded (that's the learning value).
                # The item leaves the focal card for this session and stays in
                # the Inbox queue. Honest: no deferral happened.
                if item_type in ("email", "whatsapp", "teams", "call"):
                    return {
                        "success": True,
                        "message": "Noted — I'll adjust my focus. It stays in your Inbox queue.",
                    }
                return {"success": False, "message": "Correction noted, but I couldn't defer this item — it may resurface."}
            return {
                "success": True,
                "message": message_voice.compose_corrected(_persona_summary),
            }

        raise HTTPException(status_code=400, detail=f"Unknown action: {action}")

    except HTTPException:
        raise
    except Exception as e:
        print(f"Focal action error: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


# --- HOME MODE SWITCH (correction feedback for Rhodey's learning) ---
@app.post("/api/home-mode-switch")
async def home_mode_switch_route(request: Request):
    """Record a user mode switch as a correction signal for Rhodey.

    Called by the Flutter app when the user overrides the pulse's
    home_mode via the mode switcher. Logs to subsystem_telemetry
    and classifier_corrections so the LLM learns from the preference.

    Body: { "previous_mode": "proceed", "new_mode": "decide" }
    """
    require_api_auth(request)
    try:
        body = await request.json()
        previous_mode = body.get("previous_mode", "")
        new_mode = body.get("new_mode", "")

        if not previous_mode or not new_mode:
            raise HTTPException(status_code=400, detail="previous_mode and new_mode required")
        if previous_mode == new_mode:
            return {"success": True, "message": "No change — same mode"}

        valid_modes = {"proceed", "decide", "sprint", "catch_up", "wrap"}
        if previous_mode not in valid_modes or new_mode not in valid_modes:
            raise HTTPException(status_code=400, detail=f"Invalid mode. Valid: {', '.join(sorted(valid_modes))}")

        # Record as a learner observation — the sentinel's ingest_feedback_overrides()
        # will pick this up on the next pulse and create the classifier_corrections row
        try:
            await emit_observation(
                subsystem='home_mode',
                event_type='correction',
                features={"previous_mode": previous_mode, "new_mode": new_mode},
                predicted=previous_mode,
                actual=new_mode,
                outcome='corrected',
                source='flutter',
            )
        except Exception as e:
            print(f"Home mode observation error (non-critical): {e}")

        return {"success": True, "message": f"Mode switch recorded: {previous_mode} → {new_mode}"}

    except HTTPException:
        raise
    except Exception as e:
        print(f"Home mode switch error: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


# --- VAULT ACTIONS (pull forward from the vault drawer) ---
@app.post("/api/vault-action")
async def vault_action_route(request: Request):
    """Pull a vaulted task forward so it re-enters the briefing horizon.

    Body: { "action": "pull_forward", "task_id": 123 }

    Pull-forward semantics: clears snoozed_until (if any) and drops the
    reminder_at to "now" so the horizon guard no longer vaults the task —
    it shows up in the next briefing and focal queue. No learning signal
    (this is a preference, not a correction).
    """
    require_api_auth(request)
    try:
        body = await request.json()
        action = body.get("action", "")
        task_id = body.get("task_id")
        if action != "pull_forward" or not task_id:
            raise HTTPException(status_code=400, detail="action=pull_forward and task_id required")
        try:
            tid = int(task_id)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="task_id must be an integer")

        supabase = tenant_aware_client()
        now_iso = datetime.now(timezone.utc).isoformat()
        res = supabase.table('tasks').update({
            'reminder_at': now_iso,
            'snoozed_until': None,
        }).eq('id', tid).eq('is_current', True).execute()
        if not res.data:
            return {"success": False, "message": "Task not found"}
        return {"success": True, "message": "Pulled forward — it's back on your board.", "task_id": tid}
    except HTTPException:
        raise
    except Exception as e:
        print(f"Vault action error: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


# --- EMAIL PENDING TASK DECISIONS (approve/reject from frontend) ---
        raise
    except Exception as e:
        print(f"Home mode switch error: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


# --- EMAIL PENDING TASK DECISIONS (approve/reject from frontend) ---
@app.post("/api/email-action")
async def email_action_route(request: Request):
    """Approve or reject email pending task via API (called from frontend)."""
    require_api_auth(request)
    try:
        body = await request.json()
        pending_id = body.get('id') or body.get('shortcode')
        action = body.get('action', '')  # 'approve'/'reject' or 'yes'/'no'

        if not pending_id or not action:
            raise HTTPException(status_code=400, detail="id and action required")

        # Normalize action: 'yes'/'no' → 'approve'/'reject'
        if action == 'yes':
            action = 'approve'
        elif action == 'no':
            action = 'reject'

        result = await process_email_pending_decision(
            int(pending_id), action,
            rejection_context=body.get('rejection_context'),
        )

        if result['success']:
            return {"success": True, "message": result['message'], "action": result['action']}
        else:
            return {"success": False, "message": result['message'], "action": result['action']}

    except Exception as e:
        print(f"Email action error: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


async def _run_batch_concurrently(
    ids: list, worker, concurrency: int = 5,
) -> tuple[int, int, int]:
    """Run a per-item decision worker over ids with bounded concurrency.

    Two jobs: actually parallelize, and report truthfully.

    1. Parallelize. The workers are async functions, but they call the
       synchronous supabase `.execute()` directly (not `exec_query`), which
       blocks the event loop. `asyncio.gather` of such workers is therefore
       strictly serial: a 100-item FYI batch ran ~2.7s/item for ~270s and
       blew the app's 120s timeout, so every item was reported failed while
       the backend kept grinding no-op updates. Each item now runs on its own
       thread and event loop (`asyncio.to_thread` + `asyncio.run`), so up to
       `concurrency` items genuinely run in parallel; the semaphore caps
       concurrent provider calls so we don't trip Gemini/OpenRouter rate
       limits. Fail-closed per item: an exception counts that item as failed,
       everything else proceeds.

       The handler's tenant scope is a contextvar, and threads do NOT inherit
       the caller's contextvars — so we capture the context here and re-enter
       it inside each worker thread, or every worker's `tenant_aware_client()`
       would silently lose the tenant.

    2. Classify. A worker returns a dict like {"success": True} or
       {"success": False, "action": "already_decided"}. An item that was
       already decided is not a failure — it's skipped, so the app stops
       reporting "N failed" for items that merely changed already. Workers
       signal "already done" inconsistently (action key for email/graph,
       message text for channels), so both are checked.
    """
    _SKIP_ACTIONS = {
        "already_decided", "already_processed", "not_found",
        "duplicate", "not_rejected",
    }
    sem = asyncio.Semaphore(concurrency)

    def _classify(result) -> str:
        if not isinstance(result, dict) or result.get("success") is not False:
            return "processed"
        message = (result.get("message") or "").lower()
        if (
            result.get("action") in _SKIP_ACTIONS
            or "already" in message
            or "not found" in message
        ):
            return "skipped"
        return "failed"

    async def _guarded(item_id):
        async with sem:
            try:
                # A Context object can be entered by only one thread at a
                # time, so each item gets its own copy (fresh objects from
                # concurrent entries of a shared copy would collide).
                ctx = contextvars.copy_context()
                result = await asyncio.to_thread(
                    lambda: ctx.run(lambda: asyncio.run(worker(item_id)))
                )
                return _classify(result)
            except Exception:
                return "failed"

    results = await asyncio.gather(*(_guarded(i) for i in ids))
    return (
        results.count("processed"),
        results.count("failed"),
        results.count("skipped"),
    )


async def _run_batch_job(kind, ids, action, worker, bulk=False):
    """Fire a batch action in the background; notify via push when done.

    Returns the job_id immediately; the work runs in a background task so the
    caller (the app) frees the user instantly instead of staring at a spinner
    for the whole batch. On completion a push notification carries the honest
    counts, so the status reaches the user even if they've left the screen or
    backgrounded the app. Fail-closed: a job error still notifies, so a batch
    can't silently vanish.

    worker is either a per-item callable (bulk=False, run through
    _run_batch_concurrently) or a single async callable invoked once as
    worker(ids) returning (processed, failed, skipped) (bulk=True, e.g. the
    FYI batch's single atomic UPDATE).
    """
    job_id = uuid.uuid4().hex[:12]
    asyncio.create_task(
        _execute_batch_job(job_id, kind, ids, action, worker, bulk=bulk))
    return job_id


async def _execute_batch_job(job_id, kind, ids, action, worker, bulk=False):
    try:
        if bulk:
            processed, failed, skipped = await worker(list(ids))
        else:
            processed, failed, skipped = await _run_batch_concurrently(ids, worker)
        audit_log_sync(
            kind, "INFO",
            f"Batch {action} (job {job_id}): {processed} processed, "
            f"{skipped} skipped, {failed} failed",
        )
        await send_push_notification(
            title=f"Rhodey: {kind} {action} finished",
            body=f"{processed} done · {skipped} already done · {failed} failed",
            data={
                "type": "batch_done",
                "kind": kind,
                "action": action,
                "processed": str(processed),
                "skipped": str(skipped),
                "failed": str(failed),
            },
        )
    except Exception as e:
        print(f"Batch job {job_id} ({kind} {action}) failed: {e}")
        audit_log_sync(kind, "ERROR", f"Batch {action} (job {job_id}) failed: {e}")
        await send_push_notification(
            title=f"Rhodey: {kind} {action} failed",
            body="Something went wrong processing the batch. Try again.",
            data={"type": "batch_failed", "kind": kind, "action": action},
        )


@app.post("/api/email-action/batch")
async def email_action_batch_route(request: Request):
    """Batch approve/reject email items. One call, server processes all.

    If no ids are provided, approves/rejects ALL pending actionable emails
    (mirrors the inbox "Approve all" bar which sends action only).
    """
    require_api_auth(request)
    try:
        body = await request.json()
        ids = body.get('ids', [])
        action = body.get('action', '')
        if action not in ('approve', 'reject'):
            raise HTTPException(status_code=400, detail="action required (approve|reject)")

        if not ids:
            # Approve-all: fetch every pending actionable email
            supabase = tenant_aware_client()
            pending_res = supabase.table('messages') \
                .select('id') \
                .is_('danny_decision', 'null') \
                .eq('direction', 'incoming') \
                .eq('channel', 'email') \
                .eq('classification', 'actionable') \
                .execute()
            ids = [r['id'] for r in (pending_res.data or [])]
            if not ids:
                return {"success": True, "processed": 0, "skipped": 0, "failed": 0}

        if body.get('background'):
            job_id = await _run_batch_job(
                'email', ids, action,
                lambda pid: process_email_pending_decision(int(pid), action),
            )
            return {"accepted": True, "job_id": job_id, "total": len(ids)}

        processed, failed, skipped = await _run_batch_concurrently(
            ids,
            lambda pid: process_email_pending_decision(int(pid), action),
        )
        return {"success": True, "processed": processed, "skipped": skipped, "failed": failed}
    except HTTPException:
        raise
    except Exception as e:
        print(f"Email batch action error: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


# --- AUTO-DECISIONS (count / confirm / undo from the Inbox banner) ---
# Mirrors the Telegram callback logic in core/webhook/handler.py so the app
# behaves identically to the inline keyboards.

@app.get("/api/auto-decisions")
async def auto_decisions_count_route(request: Request):
    """Count unverified auto-decisions (status=unverified)."""
    require_api_auth(request)
    try:
        supabase = tenant_aware_client()
        now = datetime.now(timezone.utc)
        cutoff = (now - timedelta(minutes=30)).isoformat()

        decision_res = supabase.table('decisions') \
            .select('id') \
            .eq('auto_decided', True) \
            .eq('status', 'active') \
            .is_('verified_at', None) \
            .gte('decided_at', cutoff) \
            .execute()
        return {"count": len(decision_res.data or [])}
    except HTTPException:
        raise
    except Exception as e:
        print(f"Auto-decisions count error: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@app.post("/api/auto-decisions/confirm")
async def auto_decisions_confirm_route(request: Request):
    """Confirm (verify) all unverified auto-decisions from the last 30 minutes.

    Sets verified_at on each and emits an observation to reinforce pattern
    confidence — identical to the Telegram 'confirm_auto_all' callback.
    """
    require_api_auth(request)
    try:
        supabase = tenant_aware_client()
        now = datetime.now(timezone.utc)
        cutoff = (now - timedelta(minutes=30)).isoformat()

        decision_res = supabase.table('decisions') \
            .select('id, decision_type, source, metadata') \
            .eq('auto_decided', True) \
            .eq('status', 'active') \
            .is_('verified_at', None) \
            .gte('decided_at', cutoff) \
            .execute()

        confirmed_count = 0
        trained_count = 0
        for row in (decision_res.data or []):
            supabase.table('decisions').update({
                'verified_at': now.isoformat(),
            }).eq('id', row['id']).execute()
            # Vision #4: per-item learning signal against the decision's REAL
            # subsystem + EXACT decision-time features (ledger X3 — the old
            # single 'auto_decisions' observation was decorative).
            from core.webhook.utils import emit_confirmed_observation
            if await emit_confirmed_observation(row, source_tag='auto_decisions_confirm'):
                trained_count += 1
            confirmed_count += 1

        if confirmed_count > 0:
            print(f"User confirmed {confirmed_count} auto-decisions via app "
                  f"({trained_count} emitted learning signals)")

        return {"success": True, "confirmed": confirmed_count}
    except HTTPException:
        raise
    except Exception as e:
        print(f"Auto-decisions confirm error: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@app.post("/api/auto-decisions/undo")
async def auto_decisions_undo_route(request: Request):
    """Undo auto-processed items (channels/graph/edge) — mirrors Telegram undo callbacks.

    Reverses the decision record and reverts the underlying DB row back to
    pending so it reappears for re-review. body: {"target": "channels" | "graph" | "edge"}
    """
    require_api_auth(request)
    try:
        body = await request.json()
        undo_target = body.get('target', 'channels')
        if undo_target not in ('channels', 'graph', 'edge'):
            raise HTTPException(status_code=400, detail="target must be channels, graph, or edge")

        from core.decisions import reverse_decision
        supabase = tenant_aware_client()
        now = datetime.now(timezone.utc)
        cutoff = (now - timedelta(minutes=30)).isoformat()

        decision_type = {
            'channels': 'channel_approval',
            'graph': 'graph_node_approval',
            'edge': 'graph_edge_approval',
        }[undo_target]

        decision_res = supabase.table('decisions').select('id, entity_id, decision_type, metadata') \
            .eq('auto_decided', True) \
            .eq('status', 'active') \
            .is_('verified_at', None) \
            .gte('decided_at', cutoff) \
            .eq('decision_type', decision_type) \
            .execute()

        undone_count = 0
        for row in (decision_res.data or []):
            decision_id = row['id']
            entity_id = row.get('entity_id')

            # Reverse the decision record
            reverse_decision(decision_id, rationale="User undid auto-approve via app")

            # Vision #4: an undo is a learning signal — emit the inverse
            # observation so the pattern that caused the wrong auto-approve
            # demotes (see emit_undo_correction).
            from core.webhook.utils import emit_undo_correction
            await emit_undo_correction(row)

            # Attempt to undo the actual DB action
            if undo_target == 'channels' and entity_id and str(entity_id).isdigit():
                try:
                    # Revert message back to pending for re-review
                    supabase.table('messages').update({'danny_decision': None}).eq('id', int(entity_id)).execute()
                    undone_count += 1
                except Exception as e:
                    print(f"Undo channels failed: {e}")
            elif undo_target == 'graph' and entity_id:
                try:
                    # Move the auto-approved pending node back to pending.
                    # entity_id may be a serial int or a UUID string — try both.
                    node_id = int(entity_id) if str(entity_id).isdigit() else entity_id
                    supabase.table('pending_nodes').update({'status': 'pending'}).eq('id', node_id).execute()
                    undone_count += 1
                except Exception as e:
                    print(f"Undo graph failed: {e}")
            elif undo_target == 'edge' and entity_id:
                try:
                    edge_id = int(entity_id) if str(entity_id).isdigit() else entity_id
                    supabase.table('pending_graph_edges').update({'status': 'pending'}).eq('id', edge_id).execute()
                    undone_count += 1
                except Exception as e:
                    print(f"Undo edge failed: {e}")

        return {"success": True, "undone": undone_count, "target": undo_target}
    except HTTPException:
        raise
    except Exception as e:
        print(f"Auto-decisions undo error: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@app.post("/api/decisions/undo")
async def decision_undo_route(request: Request):
    """Undo ONE manual approve/reject decision by id (Layer 1+2 undo).

    Covers the accidental-tap class that the auto-decisions undo never could:
    a manual approval/rejection (auto_decided=False) is reversed — the
    decision record is marked reversed, the underlying message/pending row is
    re-pended, and for channel approvals whose plan executed actions, the
    executed-action ledger (decisions.metadata.actions) is walked backwards
    and each reversible action is compensated (reopened tasks, soft-deleted
    created tasks/notes/events). Honest per action: anything that genuinely
    can't be reversed (deleted events, suppressed instances) is reported in
    ``actions_not_reversed`` rather than silently claimed.
    """
    require_api_auth(request)
    try:
        body = await request.json()
        decision_id = body.get('decision_id')
        if not decision_id:
            raise HTTPException(status_code=400, detail="decision_id required")

        from core.decisions import reverse_decision
        from core.actions.models import Action
        supabase = tenant_aware_client()

        decision_res = supabase.table('decisions').select('*').eq('id', int(decision_id)).limit(1).execute()
        decision = (decision_res.data or [None])[0]
        if not decision:
            return {"success": False, "message": f"Decision #{decision_id} not found."}
        if decision.get('status') != 'active':
            return {"success": False, "message": f"Decision #{decision_id} was already {decision.get('status')}."}
        if decision.get('verified_at'):
            return {"success": False, "message": "This decision was already verified and can't be undone."}
        if decision.get('reversible') is False:
            return {"success": False, "message": "This decision isn't reversible."}

        entity_type = decision.get('entity_type')
        entity_id = decision.get('entity_id')

        # 1. Reverse the decision record.
        reverse_decision(decision['id'], rationale="User undid manual decision via app undo")

        # Vision #4: an undo is a learning signal — emit the inverse
        # observation so the pattern that overstepped demotes (see
        # emit_undo_correction). Fail-open: never breaks the undo.
        from core.webhook.utils import emit_undo_correction
        await emit_undo_correction(decision)

        # 2. Revert the underlying row back to pending so it reappears.
        reverted_rows = 0
        if entity_type == 'message' and entity_id:
            try:
                supabase.table('messages').update({
                    'danny_decision': None, 'decided_at': None,
                }).eq('id', int(entity_id)).execute()
                reverted_rows += 1
            except Exception as e:
                print(f"Undo decision {decision_id}: revert message {entity_id} failed: {e}")
        elif entity_type in ('graph_node', 'pending_node') and entity_id:
            try:
                node_id = int(entity_id) if str(entity_id).isdigit() else entity_id
                supabase.table('pending_nodes').update({'status': 'pending'}).eq('id', node_id).execute()
                reverted_rows += 1
            except Exception as e:
                print(f"Undo decision {decision_id}: revert node {entity_id} failed: {e}")
        elif entity_type in ('graph_edge', 'pending_graph_edge') and entity_id:
            try:
                edge_id = int(entity_id) if str(entity_id).isdigit() else entity_id
                supabase.table('pending_graph_edges').update({'status': 'pending'}).eq('id', edge_id).execute()
                reverted_rows += 1
            except Exception as e:
                print(f"Undo decision {decision_id}: revert edge {entity_id} failed: {e}")

        # 3. Walk the executed-action ledger backwards and reverse side effects.
        #    Only channel approvals carry a ledger (their plan runs the
        #    executor); graph rows have none, so re-pending is all that applies.
        actions_reversed = []
        actions_not_reversed = []
        ledger = (decision.get('metadata') or {}).get('actions') or []
        if ledger:
            from core.actions.executor import compensate_action
            for entry in reversed(ledger):
                op = entry.get('operation')
                tid = entry.get('target_id')
                label = entry.get('title') or op
                try:
                    if op in ("close_task", "cancel_recurring", "suppress_instance",
                              "modify_recurring", "reschedule", "update_metadata", "delete_event"):
                        if tid is None:
                            continue
                        action = Action(operation=op, target_id=int(tid) if str(tid).isdigit() else tid)
                    elif op in ("create_task", "create_note", "create_event"):
                        # compensate_action stashes created ids under the
                        # object-type key (e.g. create_task → _created_task_id).
                        if tid is None:
                            continue
                        param_key = {
                            "create_task": "_created_task_id",
                            "create_note": "_created_note_id",
                            "create_event": "_created_event_id",
                        }[op]
                        action = Action(operation=op, params={
                            param_key: int(tid) if str(tid).isdigit() else tid,
                        })
                    else:
                        continue
                    await compensate_action(action, supabase)
                    actions_reversed.append(label)
                except Exception as e:
                    print(f"Undo decision {decision_id}: compensate {op} ({tid}) failed: {e}")
                    actions_not_reversed.append(label)

        return {
            "success": True,
            "message": "Undone.",
            "decision_id": decision['id'],
            "reverted": reverted_rows,
            "actions_reversed": actions_reversed,
            "actions_not_reversed": actions_not_reversed,
        }
    except HTTPException:
        raise
    except Exception as e:
        print(f"Decision undo error: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


# --- CALL PENDING ITEM DECISIONS (approve/reject from frontend) ---
@app.post("/api/call-action")
async def call_action_route(request: Request):
    """Approve or reject call pending item via API (called from frontend)."""
    require_api_auth(request)
    try:
        body = await request.json()
        pending_id = body.get('id') or body.get('shortcode')
        action = body.get('action', '')

        if not pending_id or not action:
            raise HTTPException(status_code=400, detail="id and action required")

        if action == 'yes':
            action = 'approve'
        elif action == 'no':
            action = 'reject'

        result = await process_channel_pending_decision('call', int(pending_id), action)

        if result['success']:
            return {"success": True, "message": result['message'], "action": result['action']}
        else:
            return {"success": False, "message": result['message'], "action": result['action']}

    except Exception as e:
        print(f"Call action error: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@app.post("/api/call-action/batch")
async def call_action_batch_route(request: Request):
    """Batch approve/reject call items. One call, server processes all."""
    require_api_auth(request)
    try:
        body = await request.json()
        ids = body.get('ids', [])
        action = body.get('action', '')
        if not ids or action not in ('approve', 'reject'):
            raise HTTPException(status_code=400, detail="ids and action required")
        if body.get('background'):
            job_id = await _run_batch_job(
                'call', ids, action,
                lambda pid: process_channel_pending_decision('call', int(pid), action),
            )
            return {"accepted": True, "job_id": job_id, "total": len(ids)}
        processed, failed, skipped = await _run_batch_concurrently(
            ids,
            lambda pid: process_channel_pending_decision('call', int(pid), action),
        )
        return {"success": True, "processed": processed, "skipped": skipped, "failed": failed}
    except HTTPException:
        raise
    except Exception as e:
        print(f"Call batch action error: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


# --- WHATSAPP PENDING DECISIONS (approve/reject from frontend) ---
@app.post("/api/whatsapp-action")
async def whatsapp_action_route(request: Request):
    """Approve or reject WhatsApp pending message via API (called from frontend)."""
    require_api_auth(request)
    try:
        body = await request.json()
        pending_id = body.get('id') or body.get('shortcode')
        action = body.get('action', '')

        if not pending_id or not action:
            raise HTTPException(status_code=400, detail="id and action required")

        if action == 'yes':
            action = 'approve'
        elif action == 'no':
            action = 'reject'

        result = await process_channel_pending_decision('whatsapp', int(pending_id), action)

        if result['success']:
            return {"success": True, "message": result['message'], "action": result['action']}
        else:
            return {"success": False, "message": result['message'], "action": result['action']}

    except Exception as e:
        print(f"WhatsApp action error: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@app.post("/api/whatsapp-action/batch")
async def whatsapp_action_batch_route(request: Request):
    """Batch approve/reject WhatsApp items. One call, server processes all."""
    require_api_auth(request)
    try:
        body = await request.json()
        ids = body.get('ids', [])
        action = body.get('action', '')
        if not ids or action not in ('approve', 'reject'):
            raise HTTPException(status_code=400, detail="ids and action required")
        if body.get('background'):
            job_id = await _run_batch_job(
                'whatsapp', ids, action,
                lambda pid: process_channel_pending_decision('whatsapp', int(pid), action),
            )
            return {"accepted": True, "job_id": job_id, "total": len(ids)}
        processed, failed, skipped = await _run_batch_concurrently(
            ids,
            lambda pid: process_channel_pending_decision('whatsapp', int(pid), action),
        )
        return {"success": True, "processed": processed, "skipped": skipped, "failed": failed}
    except HTTPException:
        raise
    except Exception as e:
        print(f"WhatsApp batch action error: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


# --- TEAMS PENDING DECISIONS (approve/reject from frontend) ---
@app.post("/api/teams-action")
async def teams_action_route(request: Request):
    """Approve or reject Teams pending message via API (called from app)."""
    require_api_auth(request)
    try:
        body = await request.json()
        pending_id = body.get('id') or body.get('shortcode')
        action = body.get('action', '')

        if not pending_id or not action:
            raise HTTPException(status_code=400, detail="id and action required")

        if action == 'yes':
            action = 'approve'
        elif action == 'no':
            action = 'reject'

        result = await process_channel_pending_decision('teams', int(pending_id), action)

        if result['success']:
            return {"success": True, "message": result['message'], "action": result['action']}
        else:
            return {"success": False, "message": result['message'], "action": result['action']}

    except Exception as e:
        print(f"Teams action error: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@app.post("/api/teams-action/batch")
async def teams_action_batch_route(request: Request):
    """Batch approve/reject Teams items. One call, server processes all."""
    require_api_auth(request)
    try:
        body = await request.json()
        ids = body.get('ids', [])
        action = body.get('action', '')
        if not ids or action not in ('approve', 'reject'):
            raise HTTPException(status_code=400, detail="ids and action required")
        if body.get('background'):
            job_id = await _run_batch_job(
                'teams', ids, action,
                lambda pid: process_channel_pending_decision('teams', int(pid), action),
            )
            return {"accepted": True, "job_id": job_id, "total": len(ids)}
        processed, failed, skipped = await _run_batch_concurrently(
            ids,
            lambda pid: process_channel_pending_decision('teams', int(pid), action),
        )
        return {"success": True, "processed": processed, "skipped": skipped, "failed": failed}
    except HTTPException:
        raise
    except Exception as e:
        print(f"Teams batch action error: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


# --- EMAIL DRAFT ACTIONS (send or drop a generated reply draft) ---
def _fetch_draft_body(supabase, draft_id: int) -> str:
    """Fail-open read of a draft's current body (for learning deltas).

    A telemetry read must never block the user's send/edit/drop — on any
    error we degrade to '' so the action proceeds and the observation just
    carries an empty predicted body.
    """
    try:
        res = supabase.table('email_drafts')\
            .select('draft_body')\
            .eq('id', draft_id)\
            .limit(1)\
            .execute()
        if res.data:
            return res.data[0].get('draft_body') or ''
    except Exception:
        pass
    return ''


@app.post("/api/draft-action")
async def draft_action_route(request: Request):
    """Send, edit, or drop a pending email reply draft from the app.

    action='send' → deliver via Gmail/Outlook (send_draft_reply), status='sent'.
    action='edit' → update draft_body (mirrors the web dashboard's edit) so
        the user can fix a draft before sending. Requires draft_body.
    action='drop' → mark draft rejected (status='rejected') so it leaves the
    Inbox's Email Drafts section. Never deletes the row (audit-able).
    """
    require_api_auth(request)
    try:
        body = await request.json()
        draft_id = body.get('draft_id')
        action = (body.get('action') or '').lower()
        if not draft_id or action not in ('send', 'edit', 'drop'):
            raise HTTPException(status_code=400, detail="draft_id and action (send|edit|drop) required")

        if action == 'send':
            success, error = await send_draft_reply(int(draft_id))
            if not success:
                return {"success": False, "message": error or "Failed to send draft"}
            return {"success": True, "message": "Draft sent"}

        supabase = tenant_aware_client()

        if action == 'edit':
            new_body = (body.get('draft_body') or '').strip()
            if not new_body:
                raise HTTPException(status_code=400, detail="draft_body required for edit")
            # Read-before-write for the learning signal — must fail open so a
            # read hiccup never blocks the user's edit itself.
            old_body = _fetch_draft_body(supabase, int(draft_id))
            supabase.table('email_drafts')\
                .update({'draft_body': new_body[:3000]})\
                .eq('id', int(draft_id))\
                .eq('status', 'pending')\
                .execute()
            # The user corrected the AI's draft — the strongest learning signal
            # the draft flow has. predicted=AI draft, actual=user's fix.
            await _emit_draft_observation(
                'correction', 'corrected', old_body,
                actual_body=new_body,
                edit_delta_chars=abs(len(new_body) - len(old_body)),
            )
            audit_log_sync("draft_action", "INFO", f"Draft {draft_id} edited from app Inbox")
            return {"success": True, "message": "Draft updated"}

        # drop
        old_body = _fetch_draft_body(supabase, int(draft_id))
        supabase.table('email_drafts')\
            .update({'status': 'rejected'})\
            .eq('id', int(draft_id))\
            .eq('status', 'pending')\
            .execute()
        await _emit_draft_observation('rejection', 'rejected', old_body)
        audit_log_sync("draft_action", "INFO", f"Draft {draft_id} dropped from app Inbox")
        return {"success": True, "message": "Draft dropped"}
    except HTTPException:
        raise
    except Exception as e:
        print(f"Draft action error: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


# --- FYI ACKNOWLEDGE (dismiss an FYI item from the Inbox) ---
@app.post("/api/fyi-action")
async def fyi_action_route(request: Request):
    """Acknowledge (dismiss) an FYI item so it leaves the Inbox's FYI section.

    FYI items carry no approve/reject decision — they are informational.
    Acknowledging marks danny_decision='acknowledged' (distinct from
    'approved'/'rejected') and keeps the row for telemetry/history.
    """
    require_api_auth(request)
    try:
        body = await request.json()
        item_id = body.get('id')
        if not item_id:
            raise HTTPException(status_code=400, detail="id required")
        supabase = tenant_aware_client()
        msg_row = None
        try:
            row = supabase.table('messages')\
                .select('id, channel, suggested_title, subject, sender_name, summary, suggested_project, metadata')\
                .eq('id', int(item_id))\
                .limit(1)\
                .execute()
            if row.data:
                msg_row = row.data[0]
        except Exception:
            pass  # ack must never fail because the read hiccupped
        supabase.table('messages')\
            .update({'danny_decision': 'acknowledged'})\
            .eq('id', int(item_id))\
            .is_('danny_decision', 'null')\
            .eq('direction', 'incoming')\
            .eq('classification', 'fyi')\
            .execute()
        if msg_row:
            # Ack = the user read and dismissed it. Emitted as 'confirmed' so
            # the pattern learner builds per-sender/per-channel confidence on
            # which FYIs you actually engage with (vs ones that expire unseen
            # and never emit a signal at all).
            try:
                features = build_decision_features(msg_row, msg_row.get('channel') or 'email')
            except Exception:
                features = {}
            await emit_observation(
                subsystem='fyi_pipeline',
                event_type='engagement',
                outcome='confirmed',
                predicted='fyi',
                actual='acknowledged',
                features=features,
                source='fyi_action',
            )
        audit_log_sync("fyi_action", "INFO", f"FYI item {item_id} acknowledged from app Inbox")
        return {"success": True, "message": "Acknowledged"}
    except HTTPException:
        raise
    except Exception as e:
        print(f"FYI action error: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@app.post("/api/fyi-action/batch")
async def fyi_action_batch_route(request: Request):
    """Batch acknowledge FYI items. One bulk UPDATE, not N per-item calls.

    The old loop ran each item's update through a sync `.execute()` serially
    on one event loop (~2.7s/item → 100 items ≈ 270s), blowing the app's
    120s timeout while reporting every item as failed. Worse, it logged "FYI
    item N acknowledged" for every id sent even when the update matched ZERO
    rows (the app was re-sending already-acked ids). A single conditional
    UPDATE is atomic and idempotent (the danny_decision IS NULL guard means
    re-sends can't double-ack), and PostgREST returns the rows it actually
    changed — so processed/skipped are honest.

    background=true runs the same bulk work in a background job and pushes
    the result — the app fires it and forgets, like the other batch actions.
    """
    require_api_auth(request)
    try:
        body = await request.json()
        ids = body.get('ids', [])
        if not ids:
            return {"success": True, "processed": 0, "skipped": 0, "failed": 0}

        id_ints = [int(i) for i in ids]

        if body.get('background'):
            async def _run_bulk(ids_list):
                processed, skipped = await _acknowledge_fyi_bulk(ids_list)
                return processed, 0, skipped

            job_id = await _run_batch_job(
                'fyi', id_ints, 'acknowledge', _run_bulk, bulk=True)
            return {"accepted": True, "job_id": job_id, "total": len(id_ints)}

        processed, skipped = await _acknowledge_fyi_bulk(id_ints)
        return {"success": True, "processed": processed, "skipped": skipped, "failed": 0}
    except HTTPException:
        raise
    except Exception as e:
        print(f"FYI batch action error: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


async def _acknowledge_fyi_bulk(id_ints):
    """Atomic bulk-ack FYI items. Returns (processed, skipped).

    Shared by the sync and background batch paths. One conditional UPDATE
    (danny_decision IS NULL guard) so re-sends can't double-ack; the rows
    PostgREST actually changed are the honest processed count. Fires one
    engagement observation per acknowledged row in the background —
    best-effort by design, the learning loop must never gate an ack.
    """
    supabase = tenant_aware_client()
    res = await exec_query(
        supabase.table('messages')
        .update({'danny_decision': 'acknowledged'})
        .in_('id', id_ints)
        .is_('danny_decision', 'null')
        .eq('direction', 'incoming')
        .eq('classification', 'fyi')
    )
    updated = res.data or []
    processed = len(updated)
    skipped = len(id_ints) - processed

    audit_log_sync(
        "fyi_action", "INFO",
        f"FYI batch: acknowledged {processed} of {len(id_ints)} "
        f"({skipped} already decided)",
    )

    if updated:
        async def _emit_observations():
            for row in updated:
                try:
                    features = build_decision_features(
                        row, row.get('channel') or 'email')
                except Exception:
                    features = {}
                try:
                    await emit_observation(
                        subsystem='fyi_pipeline',
                        event_type='engagement',
                        outcome='confirmed',
                        predicted='fyi',
                        actual='acknowledged',
                        features=features,
                        source='fyi_action',
                    )
                except Exception:
                    pass

        asyncio.create_task(_emit_observations())

    return processed, skipped



# --- WHATSAPP INGEST (Receives MacroDroid webhook) ---

# --- GRAPH EDGE DECISIONS (approve/reject/edit from frontend) ---
@app.post("/api/graph-edge-action")
async def graph_edge_action_route(request: Request):
    """Approve, reject, or edit graph pending edge via API (called from frontend)."""
    require_api_auth(request)
    try:
        body = await request.json()
        pending_id = body.get('id')
        action = body.get('action', '')
        new_source = body.get('new_source')
        new_target = body.get('new_target')
        new_rel = body.get('new_rel')
        new_context = body.get('new_context')

        if not pending_id or not action:
            raise HTTPException(status_code=400, detail="id and action required")

        result = await process_pending_edge_decision(
            pending_id=int(pending_id),
            decision=action,
            new_source=new_source,
            new_target=new_target,
            new_rel=new_rel,
            context=new_context
        )

        if result['success']:
            return {"success": True, "message": result['message'], "action": action}
        else:
            return {"success": False, "message": result['message'], "action": action}

    except Exception:
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail="Internal server error")


@app.post("/api/graph-edge-action/batch")
async def graph_edge_action_batch_route(request: Request):
    """Batch approve/reject graph edges. One call, server processes all.

    If no ids are provided, approves/rejects ALL pending edges
    (mirrors the inbox "Approve all" bar which sends action only).
    """
    require_api_auth(request)
    try:
        body = await request.json()
        ids = body.get('ids', [])
        action = body.get('action', '')
        if action not in ('approve', 'reject'):
            raise HTTPException(status_code=400, detail="action required (approve|reject)")

        if not ids:
            # Approve-all: fetch every pending edge
            supabase = tenant_aware_client()
            pending_res = supabase.table('pending_graph_edges') \
                .select('id') \
                .eq('status', 'pending') \
                .execute()
            ids = [r['id'] for r in (pending_res.data or [])]
            if not ids:
                return {"success": True, "processed": 0, "skipped": 0, "failed": 0}

        if body.get('background'):
            job_id = await _run_batch_job(
                'graph edge', ids, action,
                lambda pid: process_pending_edge_decision(pending_id=int(pid), decision=action),
            )
            return {"accepted": True, "job_id": job_id, "total": len(ids)}

        processed, failed, skipped = await _run_batch_concurrently(
            ids,
            lambda pid: process_pending_edge_decision(pending_id=int(pid), decision=action),
        )
        return {"success": True, "processed": processed, "skipped": skipped, "failed": failed}
    except HTTPException:
        raise
    except Exception:
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail="Internal server error")


@app.post("/api/graph-merge-action")
async def graph_merge_action_route(request: Request):
    """Accept or reject a node merge proposal via API (called from frontend)."""
    require_api_auth(request)
    try:
        body = await request.json()
        merge_proposal_id = body.get('id')
        action = body.get('action', '')
        swap = body.get('swap', False)

        if not merge_proposal_id or action not in ('accept', 'reject'):
            raise HTTPException(status_code=400, detail="id and valid action (accept/reject) required")

        supabase = tenant_aware_client()

        if action == 'reject':
            # Shared with /api/focal-action (reject) — keeps both by promoting
            # the pending label to its own live node and resolving the
            # proposal as rejected.
            return await _reject_merge_proposal(supabase, int(merge_proposal_id))

        # Accept merge — shared helper (identical to the Inbox's approve).
        return await _accept_merge_proposal(supabase, int(merge_proposal_id), swap=bool(swap))

    except Exception:
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail="Internal server error")


@app.post("/api/graph-node-action")
async def graph_node_action_route(request: Request):
    """Approve or reject a pending graph node via UI."""
    require_api_auth(request)
    try:
        body = await request.json()
        pending_id = body.get('id')
        action = body.get('action')
        new_label = body.get('label')
        
        if not pending_id or action not in ('approve', 'reject', 'unreject'):
            raise HTTPException(status_code=400, detail="id and valid action (approve/reject/unreject) required")
            
        from core.pulse.graph import process_graph_pending_decision
        result = await process_graph_pending_decision(int(pending_id), action, new_label=new_label)
        
        if not result.get("success"):
            raise HTTPException(status_code=400, detail=result.get("message", "Failed to process node decision"))
            
        return result
    except HTTPException:
        raise
    except Exception:
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail="Internal server error")


@app.post("/api/graph-node-action/batch")
async def graph_node_action_batch_route(request: Request):
    """Batch approve/reject graph nodes. One call, server processes all.

    If no ids are provided, approves/rejects ALL pending nodes
    (mirrors the inbox "Approve all" bar which sends action only).
    """
    require_api_auth(request)
    try:
        body = await request.json()
        ids = body.get('ids', [])
        action = body.get('action', '')
        if action not in ('approve', 'reject'):
            raise HTTPException(status_code=400, detail="action required (approve|reject)")

        if not ids:
            # Approve-all: fetch every pending node (respecting snooze)
            supabase = tenant_aware_client()
            pending_q = supabase.table('pending_nodes') \
                .select('id') \
                .eq('status', 'pending')
            try:
                pending_q = pending_q.or_('snoozed_until.is.null,snoozed_until.lt.now')
            except Exception:
                pass  # Column not yet migrated — fall back to unfiltered
            pending_res = pending_q.execute()
            ids = [r['id'] for r in (pending_res.data or [])]
            if not ids:
                return {"success": True, "processed": 0, "skipped": 0, "failed": 0}

        from core.pulse.graph import process_graph_pending_decision
        if body.get('background'):
            job_id = await _run_batch_job(
                'graph node', ids, action,
                lambda pid: process_graph_pending_decision(int(pid), action),
            )
            return {"accepted": True, "job_id": job_id, "total": len(ids)}

        processed, failed, skipped = await _run_batch_concurrently(
            ids,
            lambda pid: process_graph_pending_decision(int(pid), action),
        )
        return {"success": True, "processed": processed, "skipped": skipped, "failed": failed}
    except HTTPException:
        raise
    except Exception:
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail="Internal server error")


@app.post("/api/org-relationship")
async def org_relationship_route(request: Request):
    """Create an org→org relationship edge (Vendor/Client/Partner)."""
    require_api_auth(request)
    try:
        body = await request.json()
        source_org_id = body.get('source_org_id')
        target_org_id = body.get('target_org_id')
        relationship = body.get('relationship', '').upper()
        note = body.get('note')
        
        if not source_org_id or not target_org_id:
            raise HTTPException(status_code=400, detail="source_org_id and target_org_id required")
        
        valid_rels = ['VENDOR_TO', 'CLIENT_OF', 'PARTNER']
        if relationship not in valid_rels:
            raise HTTPException(status_code=400, detail=f"relationship must be one of {valid_rels}")
        
        supabase = tenant_aware_client()
        
        # Get org labels
        source_res = maybe_single_safe(supabase.table('graph_nodes').select('id, label, type').eq('id', source_org_id).eq('type', 'organization').eq('is_current', True))
        target_res = maybe_single_safe(supabase.table('graph_nodes').select('id, label, type').eq('id', target_org_id).eq('type', 'organization').eq('is_current', True))
        
        if not source_res or not source_res.data:
            raise HTTPException(status_code=400, detail="Source org not found")
        if not target_res or not target_res.data:
            raise HTTPException(status_code=400, detail="Target org not found")
        
        source_label = source_res.data['label']
        target_label = target_res.data['label']
        
        # Create pending edge for approval
        from core.lib.graph_rules import insert_pending_edge
        metadata = {'source_type': 'organization', 'target_type': 'organization'}
        if note:
            metadata['note'] = note
        
        insert_pending_edge(
            source_label,
            target_label,
            relationship,
            metadata
        )
        
        audit_log_sync("api", "INFO", f"Created org relationship: {source_label} → {relationship} → {target_label}")
        return {"success": True, "message": f"Relationship {source_label} → {relationship} → {target_label} created for approval"}
    except HTTPException:
        raise
    except Exception:
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail="Internal server error")


@app.put("/api/graph-node/{pending_id}")
async def graph_node_rename_route(pending_id: str, request: Request):
    require_api_auth(request)
    try:
        body = await request.json()
        new_label = body.get('label')
        scope = body.get('scope', 'pending')
        supabase = tenant_aware_client()
        
        if scope == 'live':
            live_res = maybe_single_safe(supabase.table('graph_nodes').select('label, type').eq('id', pending_id))
            if not live_res or not live_res.data:
                return {"success": False, "message": "Live node not found"}
            old_label = live_res.data['label']
            if old_label == new_label:
                return {"success": True, "message": "Label unchanged"}
                
            supabase.table('graph_nodes').update({'label': new_label}).eq('id', pending_id).execute()
            
            # Update pending edges referencing this live node
            supabase.table('pending_graph_edges').update({'source_label': new_label}).eq('source_label', old_label).execute()
            supabase.table('pending_graph_edges').update({'target_label': new_label}).eq('target_label', old_label).execute()
            
            # Update concept nodes linked_entity (if they linked by label)
            concepts_res = supabase.table('pending_nodes').select('id, eval_context').eq('node_type', 'concept').execute()
            if concepts_res and concepts_res.data:
                for c in concepts_res.data:
                    ctx = c.get('eval_context') or {}
                    if ctx.get('linked_entity') == old_label:
                        ctx['linked_entity'] = new_label
                        supabase.table('pending_nodes').update({'eval_context': ctx}).eq('id', c['id']).execute()
            
            # Cascade to type overrides table
            override_res = maybe_single_safe(supabase.table('graph_type_overrides').select('*').eq('label', old_label))
            if override_res and override_res.data:
                override_data = override_res.data
                supabase.table('graph_type_overrides').delete().eq('label', old_label).execute()
                supabase.table('graph_type_overrides').upsert({
                    'label': new_label,
                    'node_type': override_data['node_type'],
                    'created_at': override_data['created_at']
                }).execute()
            
            # Learner feedback
            node_type = live_res.data.get('type', 'unknown')
            try:
                record_decision(
                    decision_type="graph_node_rename",
                    title=f"Renamed {old_label} → {new_label}",
                    entity_type="graph_node",
                    entity_id=str(pending_id),
                    confidence=1.0,
                    source="web_ui",
                )
            except Exception:
                pass
            try:
                await emit_observation(
                    subsystem='entity_extraction',
                    event_type='correction',
                    features={"old_label": old_label, "new_label": new_label, "node_type": node_type},
                    predicted=node_type,
                    actual='corrected',
                    outcome='corrected',
                    source='web_ui'
                )
            except Exception:
                pass

            return {"success": True, "message": "Renamed live node"}

        if not new_label or not new_label.strip():
            raise HTTPException(status_code=400, detail="label required")
        
        new_label = new_label.strip()
        
        try:
            pending_id_int = int(pending_id)
        except ValueError:
            return {"success": False, "message": "Invalid pending ID"}
            
        pending_res = maybe_single_safe(supabase.table('pending_nodes').select('label, node_type').eq('id', pending_id_int))
        if not pending_res or not pending_res.data:
            return {"success": False, "message": "Pending node not found"}
            
        old_label = pending_res.data['label']
        if old_label == new_label:
            return {"success": True, "message": "Label unchanged"}

        supabase.table('pending_nodes').update({'label': new_label}).eq('id', pending_id_int).execute()
        
        supabase.table('pending_graph_edges').update({'source_label': new_label}).eq('source_label', old_label).execute()
        supabase.table('pending_graph_edges').update({'target_label': new_label}).eq('target_label', old_label).execute()
        
        # Also update linked_entity in concepts
        concepts_res = supabase.table('pending_nodes').select('id, eval_context').eq('node_type', 'concept').execute()
        if concepts_res and concepts_res.data:
            for c in concepts_res.data:
                ctx = c.get('eval_context') or {}
                if ctx.get('linked_entity') == old_label:
                    ctx['linked_entity'] = new_label
                    supabase.table('pending_nodes').update({'eval_context': ctx}).eq('id', c['id']).execute()

        # Cascade to type overrides table
        override_res = maybe_single_safe(supabase.table('graph_type_overrides').select('*').eq('label', old_label))
        if override_res and override_res.data:
            override_data = override_res.data
            supabase.table('graph_type_overrides').delete().eq('label', old_label).execute()
            supabase.table('graph_type_overrides').upsert({
                'label': new_label,
                'node_type': override_data['node_type'],
                'created_at': override_data['created_at']
            }).execute()

        # Learner feedback
        p_node_type_p = pending_res.data.get('node_type', 'unknown')
        try:
            record_decision(
                decision_type="graph_node_rename",
                title=f"Renamed pending {p_node_type_p}: {old_label} → {new_label}",
                entity_type="graph_node",
                entity_id=str(pending_id_int),
                confidence=1.0,
                source="web_ui",
            )
        except Exception:
            pass
        try:
            await emit_observation(
                subsystem='entity_extraction',
                event_type='correction',
                features={"old_label": old_label, "new_label": new_label, "node_type": p_node_type_p},
                predicted=p_node_type_p,
                actual='corrected',
                outcome='corrected',
                source='web_ui'
            )
        except Exception:
            pass

        return {"success": True, "message": f"Renamed to '{new_label}'"}
    except Exception:
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail="Internal server error")

@app.patch("/api/graph-node/{pending_id}/type")
async def graph_node_change_type_route(pending_id: str, request: Request):
    require_api_auth(request)
    try:
        body = await request.json()
        new_type = body.get('type')
        scope = body.get('scope', 'pending')
        
        if not new_type or new_type not in ['person', 'organization', 'concept', 'place', 'event', 'animal', 'emotional_state']:
            raise HTTPException(status_code=400, detail="valid type required")
        supabase = tenant_aware_client()
        
        if scope == 'live':
            live_res = maybe_single_safe(supabase.table('graph_nodes').select('id, label, type, db_record_id').eq('id', pending_id))
            if not live_res or not live_res.data:
                return {"success": False, "message": "Live node not found"}
            label = live_res.data['label']
            old_type = live_res.data.get('type')
            node_id = pending_id
            
            # (migration 75: no mirror rows to archive — graph node is truth)
            supabase.table('graph_nodes').update({'type': new_type}).eq('id', pending_id).execute()
            supabase.table('graph_type_overrides').upsert({'label': label, 'node_type': new_type}).execute()

            # Self-canonical id when a node becomes a grounded entity type
            if new_type in ('person', 'organization'):
                try:
                    nm_res = maybe_single_safe(supabase.table('graph_nodes').select('metadata').eq('id', pending_id))
                    nm = (nm_res.data.get('metadata') or {}) if nm_res and nm_res.data else {}
                    if isinstance(nm, str):
                        try:
                            nm = json.loads(nm)
                        except Exception:
                            nm = {}
                    if new_type == 'person':
                        nm['people_id'] = pending_id
                    else:
                        nm['organization_id'] = pending_id
                    supabase.table('graph_nodes').update({'metadata': nm, 'db_record_id': pending_id}).eq('id', pending_id).execute()
                except Exception:
                    pass

            # Learner feedback
            try:
                record_decision(
                    decision_type="graph_node_type_change",
                    title=f"Changed {label}: {old_type} → {new_type}",
                    entity_type="graph_node",
                    entity_id=str(pending_id),
                    confidence=1.0,
                    source="web_ui",
                )
            except Exception:
                pass
            try:
                await emit_observation(
                    subsystem='entity_extraction',
                    event_type='correction',
                    features={"old_type": old_type, "new_type": new_type, "node_type": new_type},
                    predicted=old_type,
                    actual=new_type,
                    outcome='corrected',
                    source='web_ui'
                )
            except Exception:
                pass

            return {"success": True, "message": f"Changed type to {new_type}"}
            
        try:
            pending_id_int = int(pending_id)
        except ValueError:
            return {"success": False, "message": "Invalid pending ID"}
            
        pending_res = maybe_single_safe(supabase.table('pending_nodes').select('id, label, type:node_type').eq('id', pending_id_int))
        if not pending_res or not pending_res.data:
            return {"success": False, "message": "Pending node not found"}
            
        label = pending_res.data['label']
        old_type = pending_res.data.get('type')
        
        # (migration 75: no mirror rows to archive — graph node is truth)
        supabase.table('pending_nodes').update({'node_type': new_type}).eq('id', pending_id_int).execute()
        supabase.table('graph_type_overrides').upsert({'label': label, 'node_type': new_type}).execute()

        # Self-canonical id when a node becomes a grounded entity type
        if new_type in ('person', 'organization'):
            live_node = maybe_single_safe(supabase.table('graph_nodes').select('id, metadata').eq('label', label).eq('is_current', True))
            node_id = str(live_node.data['id']) if live_node and live_node.data else None
            if node_id:
                try:
                    nm = live_node.data.get('metadata') or {}
                    if isinstance(nm, str):
                        try:
                            nm = json.loads(nm)
                        except Exception:
                            nm = {}
                    if new_type == 'person':
                        nm['people_id'] = node_id
                    else:
                        nm['organization_id'] = node_id
                    supabase.table('graph_nodes').update({'metadata': nm, 'db_record_id': node_id}).eq('id', node_id).execute()
                except Exception:
                    pass

        # Learner feedback
        try:
            record_decision(
                decision_type="graph_node_type_change",
                title=f"Changed pending {label}: {old_type} → {new_type}",
                entity_type="graph_node",
                entity_id=str(pending_id_int),
                confidence=1.0,
                source="web_ui",
            )
        except Exception:
            pass
        try:
            await emit_observation(
                subsystem='entity_extraction',
                event_type='correction',
                features={"old_type": old_type, "new_type": new_type, "node_type": new_type},
                predicted=old_type,
                actual=new_type,
                outcome='corrected',
                source='web_ui'
            )
        except Exception:
            pass

        return {"success": True, "message": f"Changed type to {new_type}"}
    except Exception:
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail="Internal server error")

@app.delete("/api/graph-node/{pending_id}")
async def graph_node_delete_route(pending_id: str, request: Request):
    require_api_auth(request)
    try:
        scope = request.query_params.get('scope', 'pending')
        
        import uuid
        def _is_uuid(val):
            try:
                uuid.UUID(str(val))
                return True
            except (ValueError, AttributeError):
                return False

        # Auto-detect scope to avoid UI mismatch crashes
        if _is_uuid(pending_id):
            scope = 'live'
        else:
            scope = 'pending'
        supabase = tenant_aware_client()
        
        if scope == 'live':
            live_res = maybe_single_safe(supabase.table('graph_nodes').select('label, type, db_record_id').eq('id', pending_id))
            if not live_res or not live_res.data:
                return {"success": False, "message": "Live node not found"}
            label = live_res.data['label']
            
            # (migration 75: no mirror rows to clear — graph node is truth)
            supabase.table('graph_nodes').update({
                'canonical_id': None
            }).eq('canonical_id', pending_id).execute()
            
            # Cascade delete live edges
            supabase.table('graph_edges').delete().eq('source_node_id', pending_id).execute()
            supabase.table('graph_edges').delete().eq('target_node_id', pending_id).execute()
            
            # Reject pending edges referencing this deleted node label
            supabase.table('pending_graph_edges').update({'status': 'rejected'}).eq('source_label', label).execute()
            supabase.table('pending_graph_edges').update({'status': 'rejected'}).eq('target_label', label).execute()
            
            # Reject orphaned concept nodes
            orphaned = 0
            concepts_res = supabase.table('pending_nodes').select('id, eval_context').eq('node_type', 'concept').in_('status', ['pending', 'flagged']).execute()
            if concepts_res and concepts_res.data:
                for c in concepts_res.data:
                    ctx = c.get('eval_context') or {}
                    if ctx.get('linked_entity') == label:
                        supabase.table('pending_nodes').update({'status': 'rejected'}).eq('id', c['id']).execute()
                        orphaned += 1
                        
            supabase.table('graph_nodes').delete().eq('id', pending_id).execute()

            node_type = live_res.data.get('type', 'unknown')

            # Dedup guard: ensure a rejected pending_nodes row exists for this label
            existing_pn = maybe_single_safe(supabase.table('pending_nodes').select('id').ilike('label', label))
            if existing_pn and existing_pn.data:
                supabase.table('pending_nodes').update({'status': 'rejected'}).eq('id', existing_pn.data['id']).execute()
            else:
                supabase.table('pending_nodes').insert({
                    'label': label,
                    'node_type': node_type,
                    'status': 'rejected'
                }).execute()

            # Learner feedback
            try:
                record_decision(
                    decision_type="graph_node_deletion",
                    title=f"Deleted live {node_type}: {label}",
                    entity_type="graph_node",
                    entity_id=str(pending_id),
                    confidence=1.0,
                    source="web_ui",
                )
            except Exception:
                pass
            try:
                await emit_observation(
                    subsystem='entity_extraction',
                    event_type='deletion',
                    features={"node_type": node_type},
                    predicted=node_type,
                    actual='deleted',
                    outcome='rejected',
                    source='web_ui'
                )
            except Exception:
                pass

            return {"success": True, "message": f"Deleted live node '{label}', {orphaned} orphaned concepts, and rejected matching pending edges"}
        
        try:
            pending_id_int = int(pending_id)
        except ValueError:
            return {"success": False, "message": "Invalid pending ID"}
            
        pending_res = maybe_single_safe(supabase.table('pending_nodes').select('label, node_type').eq('id', pending_id_int))
        if not pending_res or not pending_res.data:
            return {"success": False, "message": "Pending node not found"}
            
        label = pending_res.data['label']
        
        # Reject the node
        supabase.table('pending_nodes').update({'status': 'rejected'}).eq('id', pending_id_int).execute()
        
        # Reject related edges
        supabase.table('pending_graph_edges').update({'status': 'rejected'}).eq('source_label', label).execute()
        supabase.table('pending_graph_edges').update({'status': 'rejected'}).eq('target_label', label).execute()
        
        # Reject orphaned concept nodes
        orphaned = 0
        concepts_res = supabase.table('pending_nodes').select('id, eval_context').eq('node_type', 'concept').in_('status', ['pending', 'flagged']).execute()
        if concepts_res and concepts_res.data:
            for c in concepts_res.data:
                ctx = c.get('eval_context') or {}
                if ctx.get('linked_entity') == label:
                    supabase.table('pending_nodes').update({'status': 'rejected'}).eq('id', c['id']).execute()
                    orphaned += 1
                    
        # --- Handle people table & live node cleanup (set deleted_at instead of text marker) ---
        live_res = maybe_single_safe(supabase.table('graph_nodes').select('id, type, db_record_id').eq('label', label).eq('is_current', True))
        if live_res and live_res.data:
            l_id = live_res.data['id']

            # (migration 75: no mirror rows to clear — graph node is truth)
            supabase.table('graph_nodes').update({
                'canonical_id': None
            }).eq('canonical_id', l_id).execute()

            supabase.table('graph_edges').delete().eq('source_node_id', l_id).execute()
            supabase.table('graph_edges').delete().eq('target_node_id', l_id).execute()
            supabase.table('graph_nodes').delete().eq('id', l_id).execute()

            # Learner feedback for pending deletion (which also cleaned up live node)
            p_node_type = pending_res.data.get('node_type', 'unknown')
            try:
                record_decision(
                    decision_type="graph_node_rejection",
                    title=f"Rejected {p_node_type}: {label}",
                    entity_type="graph_node",
                    entity_id=str(pending_id_int),
                    confidence=1.0,
                    source="web_ui",
                )
            except Exception:
                pass
            try:
                await emit_observation(
                    subsystem='entity_extraction',
                    event_type='rejection',
                    features={"node_type": p_node_type},
                    predicted=p_node_type,
                    actual='rejected',
                    outcome='rejected',
                    source='web_ui'
                )
            except Exception:
                pass

        return {"success": True, "message": f"Deleted node '{label}', rejected edges and {orphaned} orphaned concepts"}
    except Exception:
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail="Internal server error")

@app.post("/api/graph-node-merge")
async def graph_node_manual_merge_route(request: Request):
    require_api_auth(request)
    try:
        body = await request.json()
        pending_id = body.get('id')
        target_id = body.get('target_id')
        scope = body.get('scope', 'pending')
        
        if not pending_id or not target_id:
            raise HTTPException(status_code=400, detail="id and target_id required")
        supabase = tenant_aware_client()
        
        if scope == 'live':
            source_res = maybe_single_safe(supabase.table('graph_nodes').select('id, label, type').eq('id', pending_id))
            if not source_res or not source_res.data:
                return {"success": False, "message": "Source live node not found"}
            source_label = source_res.data['label']
            
            target_res = maybe_single_safe(supabase.table('graph_nodes').select('id, label').eq('id', target_id))
            if not target_res or not target_res.data:
                return {"success": False, "message": "Target live node not found"}
            target_label = target_res.data['label']
            
            if source_label == target_label:
                supabase.table('graph_nodes').delete().eq('id', pending_id).execute()
                return {"success": True, "message": "Nodes had same label. Source deleted."}
                
            loser_id = pending_id
            winner_id = target_id
            source_type = source_res.data['type']
            
            # --- Handle unique_edge constraint & performance timeout ---
            # Instead of looping through all of the winner's edges (which could be 800+ and cause a timeout),
            # we loop through the loser's edges (usually just a few) and safely move them.
            
            # 1. Source_node_id rewiring (loser -> winner)
            loser_out = supabase.table('graph_edges').select('id, target_node_id, relationship').eq('source_node_id', loser_id).execute()
            for l_edge in (loser_out.data or []):
                # Check if winner already has this edge
                w_edge = maybe_single_safe(supabase.table('graph_edges').select('id').eq('source_node_id', winner_id).eq('target_node_id', l_edge['target_node_id']).eq('relationship', l_edge['relationship']))
                if w_edge and w_edge.data:
                    # Duplicate exists! Just delete the loser's edge
                    supabase.table('graph_edges').delete().eq('id', l_edge['id']).execute()
                else:
                    # Safe to repoint
                    supabase.table('graph_edges').update({'source_node_id': winner_id}).eq('id', l_edge['id']).execute()
            
            # 2. Target_node_id rewiring (loser -> winner)
            loser_in = supabase.table('graph_edges').select('id, source_node_id, relationship').eq('target_node_id', loser_id).execute()
            for l_edge in (loser_in.data or []):
                w_edge = maybe_single_safe(supabase.table('graph_edges').select('id').eq('target_node_id', winner_id).eq('source_node_id', l_edge['source_node_id']).eq('relationship', l_edge['relationship']))
                if w_edge and w_edge.data:
                    supabase.table('graph_edges').delete().eq('id', l_edge['id']).execute()
                else:
                    supabase.table('graph_edges').update({'target_node_id': winner_id}).eq('id', l_edge['id']).execute()
            
            # (migration 75: no mirror rows to clean on merge — the loser node's
            # is_current=false + canonical_id handle archiving in the graph itself)
            # Canonicalise and rewire live edges
            # BUG FIX: Set is_current=False on the loser so it stops appearing in
            # the Live tab and all is_current=True queries. Previously only
            # canonical_id was set, causing merged entities to remain visible.
            supabase.table('graph_nodes').update({
                'canonical_id': winner_id,
                'is_current': False
            }).eq('id', loser_id).execute()
            
            # Repoint pending edges referencing the merged source label
            supabase.table('pending_graph_edges').update({'source_label': target_label}).eq('source_label', source_label).execute()
            supabase.table('pending_graph_edges').update({'target_label': target_label}).eq('target_label', source_label).execute()
            
            # Update concept nodes linked_entity
            concepts_res = supabase.table('pending_nodes').select('id, eval_context').eq('node_type', 'concept').execute()
            if concepts_res and concepts_res.data:
                for c in concepts_res.data:
                    ctx = c.get('eval_context') or {}
                    if ctx.get('linked_entity') == source_label:
                        ctx['linked_entity'] = target_label
                        supabase.table('pending_nodes').update({'eval_context': ctx}).eq('id', c['id']).execute()
            
            # Do NOT delete source live node, keep it as a canonical alias pointer

            # Learner feedback
            try:
                record_decision(
                    decision_type="graph_node_merge",
                    title=f"Merged live node '{source_label}' into '{target_label}'",
                    entity_type="graph_node",
                    entity_id=str(pending_id),
                    confidence=1.0,
                    source="web_ui",
                )
            except Exception:
                pass
            try:
                await emit_observation(
                    subsystem='entity_extraction',
                    event_type='correction',
                    features={"source_label": source_label, "target_label": target_label, "node_type": source_res.data.get('type', 'unknown')},
                    predicted=source_label,
                    actual=target_label,
                    outcome='corrected',
                    source='web_ui'
                )
            except Exception:
                pass

            return {"success": True, "message": f"Merged live '{source_label}' into '{target_label}'"}
            
        # Source node (pending)
        source_res = maybe_single_safe(supabase.table('pending_nodes').select('label, type:node_type').eq('id', pending_id))
        if not source_res or not source_res.data:
            return {"success": False, "message": "Source pending node not found"}
        source_label = source_res.data['label']
        source_type = source_res.data['type']
        
        # Target node - check if it's live graph_nodes or pending_nodes
        target_label = None
        
        import uuid
        def _is_uuid(val):
            try:
                uuid.UUID(str(val))
                return True
            except (ValueError, AttributeError):
                return False

        if _is_uuid(target_id):
            target_res = maybe_single_safe(supabase.table('graph_nodes').select('label').eq('id', target_id))
            if target_res and target_res.data:
                target_label = target_res.data['label']
                
        if not target_label:
            # Maybe it's a pending node ID?
            try:
                t_id = int(target_id)
                ptarget_res = maybe_single_safe(supabase.table('pending_nodes').select('label').eq('id', t_id))
                if ptarget_res and ptarget_res.data:
                    target_label = ptarget_res.data['label']
            except ValueError:
                pass
                
        if not target_label:
            return {"success": False, "message": "Target node not found"}
            
        # --- FIX: Check if pending source was already approved (has live graph_nodes entry) ---
        live_source = maybe_single_safe(supabase.table('graph_nodes').select('id').eq('label', source_label).eq('is_current', True))
        if live_source and live_source.data:
            s_live_id = live_source.data['id']
            if _is_uuid(target_id):
                # Clean conflicting edges before rewiring using loser-first logic
                loser_out = supabase.table('graph_edges').select('id, target_node_id, relationship').eq('source_node_id', s_live_id).execute()
                for l_edge in (loser_out.data or []):
                    w_edge = maybe_single_safe(supabase.table('graph_edges').select('id').eq('source_node_id', target_id).eq('target_node_id', l_edge['target_node_id']).eq('relationship', l_edge['relationship']))
                    if w_edge and w_edge.data:
                        supabase.table('graph_edges').delete().eq('id', l_edge['id']).execute()
                    else:
                        supabase.table('graph_edges').update({'source_node_id': target_id}).eq('id', l_edge['id']).execute()
                
                loser_in = supabase.table('graph_edges').select('id, source_node_id, relationship').eq('target_node_id', s_live_id).execute()
                for l_edge in (loser_in.data or []):
                    w_edge = maybe_single_safe(supabase.table('graph_edges').select('id').eq('target_node_id', target_id).eq('source_node_id', l_edge['source_node_id']).eq('relationship', l_edge['relationship']))
                    if w_edge and w_edge.data:
                        supabase.table('graph_edges').delete().eq('id', l_edge['id']).execute()
                    else:
                        supabase.table('graph_edges').update({'target_node_id': target_id}).eq('id', l_edge['id']).execute()
                
                # (migration 75: no mirror rows to clean on merge — the graph
                # node's is_current=false + canonical_id handle archiving)

                # Update as merged alias instead of deleting
                # BUG FIX: Set is_current=False on the loser so it stops appearing
                # in the Live tab was still is_current=True after a merge.
                supabase.table('graph_nodes').update({
                    'canonical_id': target_id,
                    'is_current': False
                }).eq('id', s_live_id).execute()
            else:
                # If target is not a live node, we can't set canonical_id yet. We just delete the live source to prevent orphans, 
                # but ideally we merge it into the new live target later.
                supabase.table('graph_nodes').delete().eq('id', s_live_id).execute()
        # --------------------------------------------------------------------------------------
            
        if source_label == target_label:
            # Mark source since it's already the same name
            supabase.table('pending_nodes').update({'status': 'merged'}).eq('id', pending_id).execute()
            return {"success": True, "message": "Nodes had same label. Source merged."}

        # Repoint pending edges
        supabase.table('pending_graph_edges').update({'source_label': target_label}).eq('source_label', source_label).execute()
        supabase.table('pending_graph_edges').update({'target_label': target_label}).eq('target_label', source_label).execute()
        
        # Update concept nodes
        concepts_res = supabase.table('pending_nodes').select('id, eval_context').eq('node_type', 'concept').execute()
        if concepts_res and concepts_res.data:
            for c in concepts_res.data:
                ctx = c.get('eval_context') or {}
                if ctx.get('linked_entity') == source_label:
                    ctx['linked_entity'] = target_label
                    supabase.table('pending_nodes').update({'eval_context': ctx}).eq('id', c['id']).execute()
                    
        # Mark source pending node as merged entirely
        supabase.table('pending_nodes').update({'status': 'merged'}).eq('id', pending_id).execute()

        # Learner feedback
        try:
            record_decision(
                decision_type="graph_node_merge",
                title=f"Merged pending '{source_label}' into '{target_label}'",
                entity_type="graph_node",
                entity_id=str(pending_id),
                confidence=1.0,
                source="web_ui",
            )
        except Exception:
            pass
        try:
            await emit_observation(
                subsystem='entity_extraction',
                event_type='correction',
                features={"source_label": source_label, "target_label": target_label, "node_type": source_type or 'unknown'},
                predicted=source_label,
                actual=target_label,
                outcome='corrected',
                source='web_ui'
            )
        except Exception:
            pass

        return {"success": True, "message": f"Merged '{source_label}' into '{target_label}'"}
        
    except Exception:
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail="Internal server error")

@app.get("/api/graph-nodes/search")
async def graph_nodes_search_route(request: Request):
    require_api_auth(request)
    q = request.query_params.get('q', '').strip()
    node_type = request.query_params.get('type')
    scope = request.query_params.get('scope', 'live')
    
    if not q or len(q) < 2:
        return []
    try:
        supabase = tenant_aware_client()
        table_name = 'pending_nodes' if scope == 'pending' else 'graph_nodes'
        select_cols = 'id, label, type:node_type' if scope == 'pending' else 'id, label, type'
        query = supabase.table(table_name).select(select_cols).ilike('label', f'%{q}%')
        if scope != 'pending':
            query = query.eq('is_current', True)
        if node_type:
            filter_col = 'node_type' if scope == 'pending' else 'type'
            query = query.eq(filter_col, node_type)
        res = query.limit(10).execute()
        return res.data or []
    except Exception:
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail="Internal server error")

@app.get("/api/graph-nodes/similar")
async def graph_nodes_similar_route(request: Request):
    require_api_auth(request)
    label = request.query_params.get('label', '').strip()
    node_type = request.query_params.get('type', '').strip()
    threshold = float(request.query_params.get('threshold', 0.80))
    if not label or not node_type:
        return []
    try:
        from core.lib.graph_rules import find_similar_node
        # find_similar_node returns [{'id': '...', 'label': '...', 'type': '...', 'score': 0.95}, ...]
        matches = find_similar_node(label, node_type, threshold)
        
        # Also check pending_nodes for exact/high matches
        supabase = tenant_aware_client()
        pending_res = supabase.table('pending_nodes').select('id, label, type:node_type').eq('node_type', node_type).execute()
        pending_nodes = pending_res.data or []
        import difflib
        target_lower = label.lower()
        for p in pending_nodes:
            if p.get('label', '').lower() == target_lower:
                continue # ignore exact self if it happens
            ratio = difflib.SequenceMatcher(None, target_lower, p.get('label', '').lower()).ratio()
            if ratio >= threshold:
                # Add a marker so the frontend knows it's pending
                matches.append({
                    'id': p['id'], 
                    'label': p['label'], 
                    'type': p['type'], 
                    'score': round(ratio, 3),
                    'is_pending': True
                })
                
        return sorted(matches, key=lambda x: x['score'], reverse=True)[:5]
    except Exception:
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail="Internal server error")

@app.get("/api/graph-edges/similar")
async def graph_edges_similar_route(request: Request):
    require_api_auth(request)
    source = request.query_params.get('source', '').strip()
    target = request.query_params.get('target', '').strip()
    rel = request.query_params.get('rel', '').strip()
    if not source or not target or not rel:
        return []
    try:
        supabase = tenant_aware_client()
        # Find node IDs for the labels to check live graph_edges
        src_res = supabase.table('graph_nodes').select('id').ilike('label', source).eq('is_current', True).execute()
        tgt_res = supabase.table('graph_nodes').select('id').ilike('label', target).eq('is_current', True).execute()
        
        matches = []
        if src_res.data and tgt_res.data:
            for src_node in src_res.data:
                for tgt_node in tgt_res.data:
                    edge_res = supabase.table('graph_edges').select('id').eq('source_node_id', src_node['id']).eq('target_node_id', tgt_node['id']).eq('relationship', rel).execute()
                    if edge_res.data:
                        matches.append({'id': edge_res.data[0]['id'], 'is_pending': False})
        
        # Check pending edges too
        pend_res = supabase.table('pending_graph_edges').select('id').ilike('source_label', source).ilike('target_label', target).eq('relationship', rel).execute()
        for p in (pend_res.data or []):
            matches.append({'id': p['id'], 'is_pending': True})
            
        return matches
    except Exception:
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail="Internal server error")

@app.post("/api/whatsapp-ingest")
async def whatsapp_ingest_route(request: Request):
    """Retired — MacroDroid WhatsApp ingest replaced by the Beeper bridge
    (Phase B1 cutover, Aug 12 2026).

    The bridge-agent (`/api/beeper-sync` + Modal scheduled function) is now
    the primary WhatsApp source: it syncs both directions and routes
    incoming through the same classification pipeline, with native Matrix
    event-id dedup. This route is a 410 so any straggler MacroDroid
    automation gets a clear "this endpoint is gone" signal instead of
    silently failing. Remove the MacroDroid automation on the phone to
    stop the calls entirely.
    """
    raise HTTPException(
        status_code=410,
        detail="WhatsApp ingest via MacroDroid is retired — the Beeper bridge is now the "
               "WhatsApp source (see /api/beeper-sync). Disable the MacroDroid "
               "automation on the phone.",
    )


# --- BEEPER BRIDGE (Phase B1 — Matrix stream sync) ---
@app.get("/api/beeper-sync")
@app.post("/api/beeper-sync")
async def beeper_sync_route(request: Request):
    """Trigger one Beeper bridge tick (fan out per active tenant).

    The bridge-agent normally runs as the Modal scheduled function
    (`beeper_bridge_sync`, every 60s). This route exists for cron-job.org
    fallback, manual verification, and health pings — same bearer gate as
    /api/sentinel. Returns a per-tenant summary of outgoing sends recorded
    and any errors.
    """
    auth_header = request.headers.get("Authorization", "")
    cron_secret = os.getenv("CRON_SECRET", os.getenv("PULSE_SECRET"))

    if not cron_secret:
        raise HTTPException(status_code=500, detail="CRON_SECRET missing")
    if auth_header != f"Bearer {cron_secret}" and request.headers.get("x-pulse-secret") != cron_secret:
        raise HTTPException(status_code=401, detail="Unauthorized")

    from core.skills.beeper_ingest import run_beeper_sync
    result = await run_beeper_sync()
    return {"success": True, "result": result}


# --- BEEPER SEND (Phase C — user-approved WhatsApp sends through Beeper) ---
@app.post("/api/beeper-send")
async def beeper_send_route(request: Request):
    """Send a USER-APPROVED WhatsApp message through Beeper.

    The app calls this only after the user taps approve on a drafted reply.
    Auth: per-user API key (require_api_auth) — NOT the cron bearer gate,
    because this is an action taken by the tenant's own app session, not a
    server-side scheduled job. The tenant context resolved here scopes the
    room-map lookup, the outgoing-message record, and the awaiting-reply
    tracker to the caller.

    Body: {"chat_id": <chat key or phone>, "message": <text>,
           "mark_awaiting": true}

    Returns the send status: sent (with event_id), no_room, no_token, or
    error. On success the send is recorded as an outgoing message (stale
    pending approvals in that chat auto-resolve) and the chat is marked
    awaiting-reply.
    """
    uid = require_api_auth(request)
    try:
        body = await request.json()
        chat_id = (body.get("chat_id") or "").strip()
        message = (body.get("message") or "").strip()
        if not chat_id:
            raise HTTPException(status_code=400, detail="chat_id required")
        if not message:
            raise HTTPException(status_code=400, detail="message required")
        # JSON booleans only — a "false" string would be truthy, so accept
        # the literal True and treat everything else as False.
        mark_awaiting = body.get("mark_awaiting") is True

        from core.skills.beeper_send import send_whatsapp_message
        result = await send_whatsapp_message(
            chat_id, message, uid=uid, mark_awaiting=mark_awaiting,
        )

        if result.get("status") == "sent":
            return {"success": True, **result}
        # Client-visible failures: no_room = the bridge hasn't seen this
        # chat yet (404 — the client asked for something that doesn't
        # exist); no_token = server-side config gap (503 — not the
        # client's fault); error = the send itself failed (502).
        if result.get("status") == "no_room":
            status = 404
        elif result.get("status") == "no_token":
            status = 503
        else:
            status = 502
        return JSONResponse({"success": False, **result}, status_code=status)
    except HTTPException:
        raise
    except Exception as e:
        print(f"Beeper send error: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


# --- APP VERSION CHECK (for in-app updates) ---
@app.get("/api/app-version")
async def app_version_route(request: Request):
    """Return the latest app version info from core_config.

    The CI workflow records version info to the `core_config` table
    after each successful build. This endpoint reads from there,
    removing the dependency on GitHub API tokens.
    """
    try:
        supabase = tenant_aware_client()
        res = supabase.table('core_config').select('content').eq('key', 'app_version').limit(1).execute()

        if not res.data or not res.data[0].get('content'):
            return {
                "version_code": 0,
                "version_name": "",
                "download_url": None,
                "release_notes": "",
                "found": False
            }

        content = json.loads(res.data[0]['content'])
        return {
            "version_code": content.get('version_code', 0),
            "version_name": content.get('version_name', ''),
            "download_url": content.get('download_url'),
            "release_notes": content.get('release_notes', ''),
            "found": True
        }
    except Exception as e:
        print(f"App version check error: {e}")
        return {
            "version_code": 0,
            "version_name": "",
            "download_url": None,
            "release_notes": "",
            "found": False
        }


# --- MULTIMODAL INPUT (Receives file uploads from Flutter app) ---
async def _classic_multimodal_flow(file_bytes, mime_type):
    """Classic extract → classify → route flow (for images, audio, Telegram, fallback)."""
    from core.webhook.multimodal import process_multimodal_content
    from core.actions import get_captured_response
    from core.services.db import tenant_aware_client
    from datetime import timezone, timedelta

    ist_offset = timezone(timedelta(hours=5, minutes=30))
    now = datetime.now(ist_offset)
    telegram_chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not telegram_chat_id:
        raise HTTPException(status_code=500, detail="TELEGRAM_CHAT_ID missing")

    await process_multimodal_content(
        file_bytes, mime_type, int(telegram_chat_id),
        ist_hour=now.hour
    )

    response_text = get_captured_response()

    try:
        from api.briefing import build_briefing
        briefing = await build_briefing(tenant_aware_client())
        briefing_update = json.loads(json.dumps(briefing, default=str))
    except Exception:
        briefing_update = None

    return {
        "success": True,
        "response": response_text,
        "briefing_update": briefing_update,
        "document_breakdown": None,
    }


@app.post("/api/multimodal-input")
async def multimodal_input_route(request: Request):
    """Accept file uploads (images, audio, documents) from the Flutter app.

    When source=app and the file is a document, attempts the document
    intelligence flow (extract → parse → breakdown). Falls back to the
    classic flow on any error.
    """
    owner_id = require_api_auth(request)
    try:
        form = await request.form()
        file = form.get("file")
        if not file:
            raise HTTPException(status_code=400, detail="file required")

        file_bytes = await file.read()
        mime_type = file.content_type or "application/octet-stream"
        source = form.get("source") or "app"
        filename = form.get("filename") or None
        # Flutter file_picker sends application/octet-stream for all file types.
        # Detect the real MIME type from the filename extension.
        if mime_type == "application/octet-stream" and filename:
            import mimetypes
            guessed, _ = mimetypes.guess_type(filename)
            if guessed:
                mime_type = guessed

        # Only attempt intelligence flow for app-uploaded documents
        document_types = (
            "application/pdf",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "application/vnd.openxmlformats-officedocument.presentationml.presentation",
            "text/plain",
        )
        is_document = mime_type in document_types

        if source == "app" and is_document:
            from core.webhook.multimodal import extract_text
            from core.webhook.document_parser import parse_document
            from core.services.db import tenant_aware_client

            extracted_text = extract_text(file_bytes, mime_type)
            if not extracted_text:
                return await _classic_multimodal_flow(file_bytes, mime_type)

            breakdown = await parse_document(extracted_text)
            if not breakdown:
                return await _classic_multimodal_flow(file_bytes, mime_type)

            # Always return the breakdown (even for simple docs).
            # The app uses the 'complex' flag to decide whether to show
            # checkboxes or auto-create items — but the breakdown itself
            # is always useful.

            # Store document and breakdown
            supabase = tenant_aware_client()
            doc_result = supabase.table("documents").insert({
                "owner_id": owner_id,
                "filename": filename,
                "mime_type": mime_type,
                "extracted_text": extracted_text[:10000],
                "parsed_breakdown": breakdown,
            }).execute()

            document_id = doc_result.data[0]["id"] if doc_result.data else None

            if document_id:
                try:
                    supabase.table("raw_dumps").insert({
                        "content": extracted_text[:10000],
                        "source": "document",
                        "status": "processed",
                        "is_processed": True,
                        "direction": "incoming",
                        "message_type": "document",
                        "sender": "app",
                        "owner_id": owner_id,
                        "metadata": {
                            "document_id": document_id,
                            "document_type": breakdown.get("document_type"),
                            "entity_context": breakdown.get("entity_context")
                        }
                    }).execute()
                except Exception as audit_e:
                    print(f"Failed to write raw_dumps for document {document_id}: {audit_e}")

            enriched_entities = []
            if breakdown and breakdown.get("suggested_entities"):
                from core.pulse.graph import match_existing_nodes
                enriched_entities = match_existing_nodes(breakdown.get("suggested_entities", []), owner_id)

            return {
                "success": True,
                "response": None,
                "briefing_update": None,
                "document_breakdown": {
                    "document_id": document_id,
                    "filename": filename,
                    "document_type": breakdown.get("document_type"),
                            "entity_context": breakdown.get("entity_context"),
                    "summary": breakdown.get("summary"),
                    "key_facts": breakdown.get("key_facts", {}),
                    "suggested_actions": breakdown.get("suggested_actions", []),
                    "suggested_entities": enriched_entities,
                },
            }
        else:
            return await _classic_multimodal_flow(file_bytes, mime_type)
    except HTTPException:
        raise
    except Exception as e:
        import traceback
        print(f"Multimodal input error: {e}")
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Internal server error: {type(e).__name__}: {e}")


# --- DOCUMENT INTELLIGENCE: CONFIRM AND CREATE ---
@app.post("/api/register-device")
async def register_device_route(request: Request):
    """Register a device FCM token for push notifications."""
    require_api_auth(request)
    try:
        body = await request.json()
        token = body.get("token")
        platform = body.get("platform", "android")
        
        if not token:
            raise HTTPException(status_code=400, detail="token required")
        
        supabase = tenant_aware_client()
        # Upsert: update existing token or insert new one
        supabase.table('device_tokens').upsert({
            'token': token,
            'platform': platform,
            'updated_at': datetime.utcnow().isoformat(),
        }, on_conflict='token').execute()
        
        return {"success": True}
    except Exception as e:
        print(f"Register device error: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


# --- DRIVE WEBHOOK (Receives Google Drive push notifications) ---
@app.post("/api/drive-webhook")
async def drive_webhook(request: Request):
    channel_id = request.headers.get("X-Goog-Channel-ID", "")
    resource_state = request.headers.get("X-Goog-Resource-State", "")
    resource_id = request.headers.get("X-Goog-Resource-ID", "")
    channel_token = request.headers.get("X-Goog-Channel-Token", "")

    expected_token = os.getenv("PULSE_SECRET")
    if not expected_token:
        # Fail closed (audit X5): an unset secret must reject, never leave the
        # endpoint open — unless ALLOW_DEV_AUTH=1 is explicitly set (local dev).
        if os.getenv("ALLOW_DEV_AUTH") != "1":
            raise HTTPException(status_code=503, detail="Drive webhook auth not configured")
    elif channel_token != expected_token:
        raise HTTPException(status_code=401, detail="Unauthorized")

    print(f"Drive webhook: channel={channel_id} state={resource_state} resource={resource_id}")

    if resource_state == "sync":
        return {"success": True}

    if resource_state == "change":
        try:
            from core.lib.constants import resolve_github_config
            github_token = os.getenv("GITHUB_TOKEN")
            owner, repo = resolve_github_config()
            if github_token and owner and repo:
                url = f"https://api.github.com/repos/{owner}/{repo}/actions/workflows/call_ingest.yml/dispatches"
                headers = {
                    "Authorization": f"token {github_token}",
                    "Accept": "application/vnd.github+json"
                }
                payload = {"ref": "main"}
                async with httpx.AsyncClient() as client:
                    resp = await client.post(url, json=payload, headers=headers, timeout=10)
                    if resp.status_code == 204:
                        print("Triggered call_ingest workflow via Drive webhook")
                    else:
                        print(f"GitHub dispatch failed: {resp.status_code}")
            else:
                print("Missing GITHUB_TOKEN, GITHUB_OWNER, or GITHUB_REPO — can't trigger workflow")
        except Exception as e:
            print(f"Drive webhook dispatch error: {e}")

    return {"success": True}
# --- PENDING NODES (listing for Inbox tab) ---
@app.get("/api/pending-graph-nodes")
async def pending_nodes_route(request: Request):
    """List all pending graph nodes awaiting approval."""
    require_api_auth(request)
    try:
        supabase = tenant_aware_client()
        res = supabase.table('pending_nodes') \
            .select('id, label, type:node_type, status, source_text, created_at, eval_context')
        # Pull pending + flagged items (skip approved/rejected/merged)
        res = res.in_('status', ['pending', 'flagged'])
        if _snooze_ok(supabase, 'pending_nodes'):
            res = res.or_('snoozed_until.is.null,snoozed_until.lt.now')
        res = res.order('created_at', desc=True).limit(100).execute()
        return {"data": res.data or []}
    except Exception:
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail="Internal server error")

# --- PENDING MERGE PROPOSALS (listing for Inbox tab) ---
@app.get("/api/pending-merges")
async def pending_merges_route(request: Request):
    """List all pending merge proposals awaiting approval."""
    require_api_auth(request)
    try:
        supabase = tenant_aware_client()
        res = supabase.table('merge_proposals') \
            .select('id, source_label, source_type, target_label, target_node_id, rationale, status') \
            .eq('status', 'proposed')
        if _snooze_ok(supabase, 'merge_proposals'):
            res = res.or_('snoozed_until.is.null,snoozed_until.lt.now')
        res = res.order('id', desc=True) \
            .limit(100) \
            .execute()
        return {"data": res.data or []}
    except Exception:
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail="Internal server error")


# --- PENDING GRAPH EDGES (listing for Inbox tab) ---
@app.get("/api/pending-graph-edges")
async def pending_graph_edges_route(request: Request):
    """List all pending graph edges awaiting approval."""
    require_api_auth(request)
    try:
        supabase = tenant_aware_client()
        res = supabase.table('pending_graph_edges') \
            .select('id, source_label, target_label, relationship, status, confidence, created_at')
        res = res.in_('status', ['pending', 'flagged'])
        if _snooze_ok(supabase, 'pending_graph_edges'):
            res = res.or_('snoozed_until.is.null,snoozed_until.lt.now')
        res = res.order('created_at', desc=True).limit(100).execute()
        return {"data": await enrich_pending_edges_with_conflicts(res.data or [])}
    except Exception:
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail="Internal server error")

@app.get("/api/inbox")
async def inbox_route(request: Request):
    """Collapsed Inbox payload: pending nodes/edges/merges + messages + the
    auto-decision count in ONE call.

    The Inbox screen previously fired 5 round-trips (pending nodes, edges,
    merges, messages, auto-decisions count). Mirroring the /api/home-feed
    pattern, all five are fetched in parallel server-side so the phone makes
    a single request — each sub-fetch fails open to [] so one table error
    can't 500 the whole Inbox.
    """
    require_api_auth(request)
    _t0 = time.perf_counter()
    _sub_ms = {}
    try:
        supabase = tenant_aware_client()

        # Supabase's client is SYNCHRONOUS — bare .execute() blocks the event
        # loop, so the parallel gather below would otherwise run serially and
        # stall concurrent requests. Offload to worker threads (exec_query).

        async def _nodes():
            try:
                q = supabase.table('pending_nodes') \
                    .select('id, label, type:node_type, status, source_text, created_at, eval_context') \
                    .in_('status', ['pending', 'flagged', 'awaiting_details'])
                if _snooze_ok(supabase, 'pending_nodes'):
                    q = q.or_('snoozed_until.is.null,snoozed_until.lt.now')
                res = await exec_query(q.order('created_at', desc=True).limit(100))
                return res.data or []
            except Exception:
                return []

        async def _edges():
            try:
                q = supabase.table('pending_graph_edges') \
                    .select('id, source_label, target_label, relationship, status, confidence, created_at') \
                    .in_('status', ['pending', 'flagged'])
                if _snooze_ok(supabase, 'pending_graph_edges'):
                    q = q.or_('snoozed_until.is.null,snoozed_until.lt.now')
                res = await exec_query(q.order('created_at', desc=True).limit(100))
                return await enrich_pending_edges_with_conflicts(res.data or [])
            except Exception:
                return []

        async def _merges():
            try:
                q = supabase.table('merge_proposals') \
                    .select('id, source_label, source_type, target_label, target_node_id, rationale, status') \
                    .eq('status', 'proposed')
                if _snooze_ok(supabase, 'merge_proposals'):
                    q = q.or_('snoozed_until.is.null,snoozed_until.lt.now')
                res = await exec_query(q.order('id', desc=True).limit(100))
                return res.data or []
            except Exception:
                return []

        async def _messages():
            try:
                res = await exec_query(
                    supabase.table('raw_dumps') \
                    .select('id, content, created_at, direction, sender, message_type, status, metadata, source') \
                    .order('created_at', desc=True) \
                    .limit(50)
                )
                return res.data or []
            except Exception:
                return []

        # Actionable, undecided channel items (email/whatsapp/call/teams) —
        # the Quick Confirmations feed the app parses into decision cards.
        async def _channel_messages():
            try:
                return await asyncio.to_thread(
                    fetch_pending_channel_messages, supabase, 50
                )
            except Exception:
                return []

        # Pending email reply drafts — the Inbox's "Email Drafts" section.
        async def _drafts():
            try:
                return await asyncio.to_thread(fetch_pending_drafts, supabase, 20)
            except Exception:
                return []

        # Undecided FYI items — the Inbox's "For your info" section.
        async def _fyi():
            try:
                return await asyncio.to_thread(fetch_fyi_messages, supabase, 100)
            except Exception:
                return []

        async def _auto_count():
            try:
                now = datetime.now(timezone.utc)
                cutoff = (now - timedelta(minutes=30)).isoformat()
                res = await exec_query(
                    supabase.table('decisions') \
                    .select('id') \
                    .eq('auto_decided', True) \
                    .eq('status', 'active') \
                    .is_('verified_at', None) \
                    .gte('decided_at', cutoff)
                )
                return len(res.data or [])
            except Exception:
                return 0

        async def _timed(label, coro):
            """Run a sub-fetch while recording its wall-clock ms."""
            _s = time.perf_counter()
            try:
                return await coro
            finally:
                _sub_ms[label] = round((time.perf_counter() - _s) * 1000, 1)

        (nodes, edges, merges, messages, channel_msgs, drafts, fyi, auto_count) = \
            await asyncio.gather(
                _timed('pending_nodes', _nodes()),
                _timed('pending_edges', _edges()),
                _timed('pending_merges', _merges()),
                _timed('messages', _messages()),
                _timed('channel_messages', _channel_messages()),
                _timed('drafts', _drafts()),
                _timed('fyi', _fyi()),
                _timed('auto_count', _auto_count()),
            )

        total_ms = round((time.perf_counter() - _t0) * 1000, 1)
        print(
            f"[TIMING] /api/inbox total={total_ms}ms "
            f"sub={_sub_ms} "
            f"rows={{nodes:{len(nodes)},edges:{len(edges)},merges:{len(merges)},messages:{len(messages)},channels:{len(channel_msgs)},drafts:{len(drafts)},fyi:{len(fyi)},auto:{auto_count}}}"
        )

        return {
            "pending_nodes": nodes,
            "pending_edges": edges,
            "pending_merges": merges,
            "pending_messages": messages,
            "pending_channel_messages": channel_msgs,
            "pending_drafts": drafts,
            "pending_fyi": fyi,
            "auto_decision_count": auto_count,
        }
    except Exception:
        import traceback
        traceback.print_exc()
        print(f"[TIMING] /api/inbox total={round((time.perf_counter() - _t0) * 1000, 1)}ms FAILED")
        raise HTTPException(status_code=500, detail="Internal server error")


@app.get("/api/graph-nodes/live")
async def graph_nodes_live_route(request: Request, limit: int = None, offset: int = 0, q: str = None):
    """Live graph entities, paginated + searchable.

    limit/offset/q bound the payload — the Entities screen pages through
    results instead of downloading the entire graph (this was a fixed
    5000-row dump with full metadata, multi-MB on mobile). Omitting `limit`
    preserves the legacy unbounded behavior for existing callers (web UI).
    """
    require_api_auth(request)
    _t0 = time.perf_counter()
    try:
        supabase = tenant_aware_client()

        # Bring key nodes and conceptual/structural entities (exclude system tasks/memories)
        entity_types = ['person', 'organization', 'concept', 'place', 'event', 'animal', 'emotional_state']
        query = supabase.table('graph_nodes') \
            .select('id, label, type, created_at, metadata') \
            .in_('type', entity_types) \
            .is_('canonical_id', 'null') \
            .eq('is_current', True)
        if q and q.strip():
            query = query.ilike('label', f'%{q.strip()}%')
        if limit is not None:
            query = query.order('created_at', desc=True) \
                .limit(min(int(limit), 500)) \
                .offset(int(offset or 0))
        else:
            query = query.order('created_at', desc=True).limit(5000)
        res = query.execute()
        rows = res.data or []
        total_ms = round((time.perf_counter() - _t0) * 1000, 1)
        print(
            f"[TIMING] /api/graph-nodes/live total={total_ms}ms "
            f"params={{limit:{limit},offset:{offset},q:{q.strip() if q else None}}} "
            f"rows={len(rows)}"
        )
        return {"data": rows}
    except Exception:
        import traceback
        traceback.print_exc()
        print(
            f"[TIMING] /api/graph-nodes/live total={round((time.perf_counter() - _t0) * 1000, 1)}ms "
            f"params={{limit:{limit},offset:{offset},q:{q.strip() if q else None}}} FAILED"
        )
        raise HTTPException(status_code=500, detail="Internal server error")


@app.patch("/api/graph-node/{node_id}/enrichment")
async def graph_node_enrichment_route(node_id: str, request: Request):
    """Update enrichment fields on a LIVE graph node.

    Consolidation (migrations 74-76): all person/org enrichment lives on
    graph_nodes.metadata.enrichment — role, strategic_weight, is_active,
    org_type, description, organization_name, last_interaction_date. This is
    the generic editing surface now that the separate People/Organizations
    tables and the web Organizations tab are gone.

    Only the allow-listed fields below are writable; anything else is ignored.
    """
    require_api_auth(request)
    try:
        body = await request.json()
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="JSON object required")

        allowed = {
            'role', 'strategic_weight', 'is_active', 'org_type',
            'description', 'organization_name', 'last_interaction_date',
        }
        updates = {k: v for k, v in body.items() if k in allowed}
        if not updates:
            return {"success": False, "message": "No editable enrichment fields provided"}

        # Validate strategic_weight range (None = clear the value)
        if 'strategic_weight' in updates:
            w = updates['strategic_weight']
            if w is not None:
                try:
                    w = int(w)
                except (TypeError, ValueError):
                    raise HTTPException(status_code=400, detail="strategic_weight must be an integer")
                if w < 1 or w > 10:
                    raise HTTPException(status_code=400, detail="strategic_weight must be between 1 and 10")
                updates['strategic_weight'] = w

        # Normalize booleans (clients may send true/false or "true"/"false")
        if 'is_active' in updates:
            v = updates['is_active']
            if isinstance(v, str):
                v = v.strip().lower() in ('1', 'true', 'yes', 'on')
            updates['is_active'] = bool(v)
        supabase = tenant_aware_client()

        node_res = maybe_single_safe(
            supabase.table('graph_nodes').select('id, label, type, metadata, db_record_id')
            .eq('id', node_id)
        )
        if not node_res or not node_res.data:
            raise HTTPException(status_code=404, detail="Live node not found")

        node = node_res.data
        meta = node.get('metadata') or {}
        if isinstance(meta, str):
            try:
                meta = json.loads(meta)
            except Exception:
                meta = {}
        enrich = dict(meta.get('enrichment') or {})
        enrich.update(updates)
        meta['enrichment'] = enrich

        supabase.table('graph_nodes').update({'metadata': meta}).eq('id', node_id).execute()

        # Learner feedback so the correction trains the system
        try:
            from core.pulse.decision_pulse import record_decision
            record_decision(
                decision_type="graph_node_enrichment",
                title=f"Updated details for {node.get('label', node_id)}",
                entity_type="graph_node",
                entity_id=str(node_id),
                confidence=1.0,
                source="web_ui",
            )
        except Exception:
            pass

        return {
            "success": True,
            "message": "Details updated",
            "enrichment": enrich,
        }
    except HTTPException:
        raise
    except Exception:
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail="Internal server error")


# ── Onboarding journey (M8) ────────────────────────────────────────────────
# In-app onboarding: status → (key | about-you | people | plate | areas |
# google) → complete. The journey answers are seeded via
# core/services/onboarding.run_onboarding (wrapping seed_world) and the first
# briefing is composed deterministically from the answers — no LLM call, so
# it is instant and free, and shaped exactly like /api/briefing so the app's
# existing BriefingResponse UI renders it.

@app.get("/api/onboarding/presets")
async def onboarding_presets():
    """The briefing-schedule presets for the onboarding picker (M9.8).

    Static config — no auth needed. The app renders THE SERVER's times
    (single source of truth), so the picker can never drift from the
    heartbeat gate's PRESETS.
    """
    from core.services.briefing_schedule import presets_payload
    return presets_payload()


@app.get("/api/onboarding/status")
async def onboarding_status(request: Request):
    """The tenant's onboarding state: new | in_progress | seeded.

    Requires a per-user API key (the journey starts with the key step).
    """
    uid = require_api_auth(request)
    if not uid:
        raise HTTPException(status_code=401, detail="A per-user API key is required")
    from core.services.onboarding import fetch_status

    return fetch_status(tenant_aware_client(), uid)


@app.post("/api/onboarding/complete")
async def onboarding_complete(request: Request):
    """Finish the journey: seed the tenant's world + return the first briefing.

    Payload (all optional except context — fail-open per section):
      context, people [{name, role}], organizations [{name, context}],
      tasks [{title, priority}], domains [{name, keywords}],
      personal_orgs, root_label, timezone.
    """
    uid = require_api_auth(request)
    if not uid:
        raise HTTPException(status_code=401, detail="A per-user API key is required")
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Expected a JSON object")

    from core.services.onboarding import run_onboarding, welcome_briefing
    from core.services.user_settings import resolve_user_name, clear_cache

    # The journey's step 2 asks the user their real name — persist it to
    # users.name so Rhodey greets them by what they typed, not the
    # admin-set account placeholder (e.g. "Test"). Fail-open: a payload
    # without a name (old client / skipped) keeps the existing account name.
    typed_name = (payload.get("name") or "").strip()
    if typed_name:
        try:
            tenant_aware_client().table("users").update({"name": typed_name}).eq("id", uid).execute()
            clear_cache(uid)
        except Exception:
            pass

    result = await run_onboarding(tenant_aware_client(), uid, payload)
    name = resolve_user_name(uid) or ""
    briefing = welcome_briefing(name, result["world"], result["summary"])
    return {"status": "seeded", "summary": result["summary"], "briefing": briefing}


# ── M11 sign-in (Google + email/OTP) ──────────────────────────────────────
# Keyless by design — these run BEFORE the user has an API key. The key is
# issued at first successful sign-in and returned once; the app stores it
# silently (SharedPreferences) and never asks the user to paste it.
# Invite model: the address must be provisioned (bootstrap --email) for the
# sign-in to succeed; uninvited emails get a generic "not invited" answer.


@app.post("/api/auth/otp/send")
async def auth_otp_send(request: Request):
    """Request a 6-digit sign-in code for an email (rate-limited)."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    from core.services.auth import send_otp

    return send_otp((body or {}).get("email", ""))


@app.post("/api/auth/otp/verify")
async def auth_otp_verify(request: Request):
    """Validate the code, mark it consumed, and issue the tenant's API key."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    from core.services.auth import verify_otp

    # 200 with an ok flag (not HTTP 4xx): the app surfaces the `message`
    # string directly in the sign-in UI without parsing error bodies.
    return verify_otp(
        (body or {}).get("email", ""), (body or {}).get("code", "")
    )


@app.get("/api/auth/google/start")
async def auth_google_start():
    """Start identity-only Google sign-in: consent URL + state token.

    Reuses the registered /api/oauth/callback redirect URI (Google Cloud
    Console only accepts http(s) URIs) and the shared state store, tagged
    with an "identity" sentinel so the service-connect exchange can never
    collide with the sign-in exchange.
    """
    client_id = os.environ.get("GOOGLE_CLIENT_ID", "")
    if not client_id:
        raise HTTPException(status_code=503, detail="Google sign-in is not configured")
    state = uuid.uuid4().hex
    _oauth_state_store(state, "identity")
    from core.services.auth import build_google_identity_url

    return {
        "url": build_google_identity_url(client_id, _OAUTH_REDIRECT_URI, state),
        "state": state,
    }


@app.post("/api/auth/google/exchange")
async def auth_google_exchange(request: Request):
    """Exchange the identity code for the tenant's API key (keyless)."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    code = (body or {}).get("code")
    state = (body or {}).get("state")
    if not code or not state:
        raise HTTPException(status_code=400, detail="code and state are required")

    stored = _oauth_state_pop(state)
    if not stored or stored[0] != "identity" or time.time() > stored[1]:
        raise HTTPException(
            status_code=400, detail="Sign-in link expired — please try again"
        )

    client_id = os.environ.get("GOOGLE_CLIENT_ID", "")
    client_secret = os.environ.get("GOOGLE_CLIENT_SECRET", "")
    if not client_id or not client_secret:
        raise HTTPException(status_code=503, detail="Google sign-in is not configured")

    from core.services.auth import exchange_google_identity, signin_by_google_identity

    identity = await exchange_google_identity(
        code, client_id, client_secret, _OAUTH_REDIRECT_URI
    )
    if not identity:
        raise HTTPException(status_code=401, detail="Could not verify your Google account")
    return signin_by_google_identity(identity)


# ── In-app Google connect (M8) ────────────────────────────────────────────
# The app opens the consent URL in an in-app browser (flutter_web_auth);
# Google redirects to the registered HTTPS callback (this endpoint), which
# validates the state token and JS-bridges the code back to
# rhodey://oauth2/callback?code=..&state=.. — the custom scheme the app's
# flutter_web_auth_2 already captures. The app then POSTs code+state back
# here for exchange. The refresh token is stored per-user in
# user_oauth_tokens — never in env.
#
# Google Cloud Console only accepts http(s) redirect URIs on this Web-app
# OAuth client (custom schemes are rejected), so the consent URL points at
# the Modal HTTPS endpoint below instead of the raw scheme.

_OAUTH_REDIRECT_URI = os.getenv(
    "GOOGLE_OAUTH_REDIRECT_URI",
    "https://danielyashwant--rhodey-os-web-endpoint.modal.run/api/oauth/callback",
)
_OAUTH_STATE_TTL = 15 * 60
_OAUTH_STATES: dict[str, tuple[str, float]] = {}  # state -> (uid, expires_at)


def _oauth_state_store(state: str, uid: str) -> None:
    """Persist an OAuth state token (memory + Redis for cross-container safety).

    Modal can serve /api/oauth/start and the browser callback from different
    containers (or a cold start in between), so the in-memory dict alone
    would lose the state. Redis (fail-open) is the durable cross-container
    copy; the dict is the fast path.
    """
    expires = time.time() + _OAUTH_STATE_TTL
    _OAUTH_STATES[state] = (uid, expires)
    try:
        from core.lib.redis_cache import cache_set
        cache_set(f"oauth:state:{state}", [uid, expires], ttl=_OAUTH_STATE_TTL)
    except Exception:
        pass


def _oauth_state_peek(state: str) -> tuple[str, float] | None:
    """Read an OAuth state without consuming it (callback validation).

    Returns None for missing OR expired states so the callback page can
    show a clear "sign-in link expired" message instead of letting the
    app fail at the exchange step."""
    entry = _OAUTH_STATES.get(state)
    if entry:
        return entry if time.time() <= entry[1] else None
    try:
        from core.lib.redis_cache import cache_get
        data = cache_get(f"oauth:state:{state}")
        if isinstance(data, list) and len(data) == 2:
            expires = float(data[1])
            if time.time() <= expires:
                return str(data[0]), expires
    except Exception:
        pass
    return None


def _oauth_state_pop(state: str) -> tuple[str, float] | None:
    """Consume an OAuth state once (exchange)."""
    entry = _OAUTH_STATES.pop(state, None)
    if entry:
        # Memory hit — also drop any durable copy so the state can't be
        # replayed through the Redis fallback on another container.
        try:
            from core.lib.redis_cache import cache_delete
            cache_delete(f"oauth:state:{state}")
        except Exception:
            pass
        return entry
    try:
        from core.lib.redis_cache import cache_get, cache_delete
        data = cache_get(f"oauth:state:{state}")
        if isinstance(data, list) and len(data) == 2:
            cache_delete(f"oauth:state:{state}")
            return str(data[0]), float(data[1])
    except Exception:
        pass
    return None


def _google_scopes() -> str:
    return (
        "https://www.googleapis.com/auth/calendar "
        "https://www.googleapis.com/auth/tasks "
        "https://www.googleapis.com/auth/gmail.modify "
        "https://www.googleapis.com/auth/drive.file "
        "https://www.googleapis.com/auth/documents "
        "https://www.googleapis.com/auth/spreadsheets.readonly"
    )


@app.get("/api/oauth/start")
async def oauth_start(request: Request):
    """Start in-app Google OAuth: returns the consent URL + state token."""
    uid = require_api_auth(request)
    if not uid:
        raise HTTPException(status_code=401, detail="A per-user API key is required")
    client_id = os.environ.get("GOOGLE_CLIENT_ID", "")
    if not client_id:
        raise HTTPException(status_code=503, detail="Google sign-in is not configured")
    state = uuid.uuid4().hex
    _oauth_state_store(state, uid)
    params = {
        "client_id": client_id,
        "redirect_uri": _OAUTH_REDIRECT_URI,
        "response_type": "code",
        "scope": _google_scopes(),
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
    }
    return {
        "url": "https://accounts.google.com/o/oauth2/auth?" + urlencode(params),
        "state": state,
    }


@app.get("/api/oauth/callback")
async def oauth_callback(request: Request):
    """Google's post-consent redirect lands here (registered HTTPS URI).

    Google Cloud Console only accepts http(s) redirect URIs on this OAuth
    client, so the consent URL points at this endpoint. The page validates
    the state token (without consuming it), then JS-bridges the
    authorization code back to the app's custom scheme:

        rhodey://oauth2/callback?code=..&state=..

    which the app's flutter_web_auth_2 (callbackUrlScheme: 'rhodey')
    captures — the app flow (start → callback → exchange) is unchanged.
    Returns a tiny HTML page; no auth header is present on a browser
    redirect, so the state token is the security check here (the exchange
    endpoint re-validates + consumes it authoritatively).
    """
    qp = request.query_params
    code = qp.get("code", "")
    state = qp.get("state", "")
    error = qp.get("error", "")

    if error:
        # User denied or Google returned an error — bridge it so the app
        # surfaces "Google sign-in was interrupted" instead of hanging.
        target = f"rhodey://oauth2/callback?error={quote(error)}&state={quote(state)}"
    elif code and state and _oauth_state_peek(state):
        target = f"rhodey://oauth2/callback?code={quote(code)}&state={quote(state)}"
    elif state:
        # State exists but is invalid/expired — tell the app to retry.
        target = f"rhodey://oauth2/callback?error=expired&state={quote(state)}"
    else:
        target = "rhodey://oauth2/callback?error=invalid_state"

    # Why the page needs a BUTTON, not just an auto-redirect:
    # Chrome (since ~73) silently DROPS script-initiated navigations to
    # custom schemes (rhodey://) — they're not a user gesture, and the OAuth
    # flow's original tap is long gone by the time this page loads. The
    # result is the classic "stuck on 'Returning to Rhodey…'" page: the tab
    # never hands off to the app. flutter_web_auth_2's own troubleshooting
    # documents the reliable pattern: a plain <a href="scheme://…"> link —
    # a link click IS a gesture, so Chrome delivers the custom scheme to
    # the app's CallbackActivity. So: render the button always (the
    # guaranteed path), and fire the auto-redirect as best-effort (covers
    # iOS + Android browsers that do allow it).
    #
    # json.dumps makes the target safe to embed as a JS string literal;
    # html.escape makes it safe as an href attribute.
    js_target = json.dumps(target)
    href_target = html.escape(target, quote=True)
    page = (
        "<!DOCTYPE html><html><head><meta charset='utf-8'>"
        "<meta http-equiv='Content-Security-Policy' "
        "content='default-src \"none\"; script-src \"unsafe-inline\"'>"
        "<title>Returning to Rhodey…</title></head>"
        "<body style='font-family:system-ui;display:flex;flex-direction:column;"
        "align-items:center;justify-content:center;height:100vh;margin:0;"
        "background:#f5f2ec;color:#3a362f;text-align:center'>"
        "<p style='margin:0;font-size:17px'>Returning to Rhodey…</p>"
        "<p style='margin:16px 0 0;font-size:13px;color:#7a7468'>"
        "If Rhodey doesn't open automatically, tap the button below.</p>"
        "<a href='" + href_target + "' "
        "style='display:inline-block;margin-top:28px;padding:14px 34px;"
        "background:#3a362f;color:#ffffff;text-decoration:none;"
        "border-radius:999px;font-weight:600;font-size:16px'>"
        "Open Rhodey</a>"
        "<script>try { window.location.href = " + js_target + "; } catch (e) {}</script>"
        "</body></html>"
    )
    return HTMLResponse(page)


@app.post("/api/oauth/exchange")
async def oauth_exchange(request: Request):
    """Exchange the authorization code for tokens and store them per-user."""
    uid = require_api_auth(request)
    if not uid:
        raise HTTPException(status_code=401, detail="A per-user API key is required")
    try:
        body = await request.json()
    except Exception:
        body = {}
    code = (body or {}).get("code")
    state = (body or {}).get("state")
    if not code or not state:
        raise HTTPException(status_code=400, detail="code and state are required")

    stored = _oauth_state_pop(state)
    if not stored or stored[0] != uid or time.time() > stored[1]:
        raise HTTPException(
            status_code=400, detail="OAuth state invalid or expired — please try again"
        )

    client_id = os.environ.get("GOOGLE_CLIENT_ID", "")
    client_secret = os.environ.get("GOOGLE_CLIENT_SECRET", "")
    if not client_id or not client_secret:
        raise HTTPException(status_code=503, detail="Google sign-in is not configured")

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                "https://oauth2.googleapis.com/token",
                data={
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "code": code,
                    "grant_type": "authorization_code",
                    "redirect_uri": _OAUTH_REDIRECT_URI,
                },
            )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Google token exchange failed: {e}")
    if resp.status_code != 200:
        raise HTTPException(
            status_code=502, detail="Google rejected the authorization code — please retry"
        )

    tokens = resp.json()
    refresh_token = tokens.get("refresh_token")
    scopes = tokens.get("scope") or ""
    if not refresh_token:
        raise HTTPException(
            status_code=400,
            detail="No refresh token returned — Google sign-in must grant offline access",
        )

    supabase = tenant_aware_client()
    supabase.table("user_oauth_tokens").upsert(
        {
            "user_id": uid,
            "provider": "google",
            "refresh_token": refresh_token,
            "scopes": scopes,
        },
        on_conflict="user_id,provider",
    ).execute()
    supabase.table("users").update({"google_connected": True}).eq("id", uid).execute()
    try:
        from core.services.google_service import clear_google_creds_cache

        clear_google_creds_cache(uid)
    except Exception:
        pass
    return {"connected": True, "scopes": scopes}
# --- UNIFIED SUGGESTIONS: CONFIRM AND CREATE ---
@app.post("/api/suggestions/confirm")
async def suggestions_confirm_route(request: Request):
    """Confirm selected tasks and entities from a suggestion card.
    
    Body: {
        "source_type": "document" | "message",
        "source_id": int | str,
        "selected_tasks": [...],
        "selected_entities": [...]
    }
    """
    owner_id = require_api_auth(request)
    try:
        body = await request.json()
        source_type = body.get("source_type")
        source_id = body.get("source_id")
        selected_tasks = body.get("selected_tasks", [])
        selected_entities = body.get("selected_entities", [])
        
        if not source_type or not source_id:
            raise HTTPException(status_code=400, detail="source_type and source_id required")
            
        supabase = tenant_aware_client()
        extracted_text = ""
        
        if source_type == "document":
            doc_res = supabase.table("documents").select("owner_id, extracted_text, parsed_breakdown").eq("id", source_id).limit(1).execute()
            if not doc_res.data:
                raise HTTPException(status_code=404, detail="Document not found")
            if doc_res.data[0].get("owner_id") != owner_id:
                raise HTTPException(status_code=403, detail="Not authorized")
            extracted_text = doc_res.data[0].get("extracted_text", "")
            stored_ctx_dict = (doc_res.data[0].get("parsed_breakdown") or {}).get("entity_context")
        elif source_type == "message":
            msg_res = supabase.table("raw_dumps").select("owner_id, content, metadata").eq("id", source_id).limit(1).execute()
            if not msg_res.data:
                raise HTTPException(status_code=404, detail="Message not found")
            if msg_res.data[0].get("owner_id") != owner_id:
                raise HTTPException(status_code=403, detail="Not authorized")
            extracted_text = msg_res.data[0].get("content", "")
            stored_ctx_dict = (msg_res.data[0].get("metadata") or {}).get("entity_context")
            
        from core.lib.entity_context import EntityContext
        entity_context_obj = EntityContext.from_dict(stored_ctx_dict) if stored_ctx_dict else None
            
        created_items = []
        created_entity_refs = []
        
        try:
            from core.pulse.graph import create_graph_node_with_db_record
            from core.pulse.tools import create_task_direct
            from core.lib.ingest import ingest
            from core.lib.enrichment_queue import enqueue_enrichment
            
            # 1. Create entities first so they are available for task resolution
            for entity in selected_entities:
                label = entity.get("label", "")
                node_type = entity.get("type", "concept")
                if not label:
                    continue
                
                merge_with = entity.get("merge_with")
                if merge_with and merge_with.get("id"):
                    # User explicitly chose to merge with an existing node
                    # The node already exists, so we just acknowledge it
                    created_items.append({"type": node_type, "title": label, "entity_id": merge_with.get("id")})
                    continue

                res = await create_graph_node_with_db_record(
                    label=label, 
                    node_type=node_type, 
                    source_text=extracted_text[:500],
                    source_tag="suggestion_confirm",
                    force=True
                )
                if res and res.get('success'):
                    created_items.append({"type": node_type, "title": label, "entity_id": res.get("node_id")})
                    

            # 1b. Merge user-selected entities into the metadata EntityContext
            if entity_context_obj:
                for item in created_items:
                    if item["type"] == "organization" and not entity_context_obj.organization_id:
                        entity_context_obj.organization_id = item["entity_id"]
                        entity_context_obj.organization_name = item["title"]
                    elif item["type"] == "person" and item["entity_id"] not in entity_context_obj.person_ids:
                        if item["entity_id"]:
                            entity_context_obj.person_ids.append(item["entity_id"])
                            entity_context_obj.person_names.append(item["title"])
                
                # Apply Personal fallback if still no org
                if not entity_context_obj.organization_id and not entity_context_obj.pending_org_id:
                    personal_res = supabase.table('graph_nodes').select('id, label').ilike('label', 'Personal').eq('type', 'organization').eq('is_current', True).eq('owner_id', owner_id).limit(1).execute()
                    if personal_res.data:
                        entity_context_obj.organization_id = personal_res.data[0]['id']
                        entity_context_obj.organization_name = personal_res.data[0]['label']

            # 2. Create tasks
            for item in selected_tasks:
                item_type = item.get("type", "task")
                title = item.get("title", "")
                deadline = item.get("deadline")
                date = item.get("date")
                
                description = item.get("description", "")
                
                entity_id = None
                
                if item_type == "task":
                    result = await create_task_direct(
                        title=title,
                        entity_context=entity_context_obj,
                        deadline=deadline,
                        notes=description,
                    )
                    entity_id = result.get("task_id") if result else None
                    if entity_id:
                        created_entity_refs.append(("tasks", entity_id))
                        
                elif item_type == "event":
                    result = await create_task_direct(
                        title=title,
                        entity_context=entity_context_obj,
                        reminder_at=date,
                        notes=description,
                    )
                    entity_id = result.get("task_id") if result else None
                    if entity_id:
                        created_entity_refs.append(("tasks", entity_id))
                        
                elif item_type == "note":
                    result = await ingest(
                        text=f"{title}. {description}" if description else title,
                        source="suggestion_confirm",
                        classification="note",
                        has_memory_value=True,
                    )
                    entity_id = result.get("message_id") if result else None
                    if entity_id:
                        created_entity_refs.append(("memories", entity_id))
                        
                if source_type == "document":
                    supabase.table("document_items").insert({
                        "owner_id": owner_id,
                        "document_id": source_id,
                        "item_type": item_type,
                        "item_data": item,
                        "created_entity_id": str(entity_id) if entity_id else None,
                    }).execute()
                    
                created_items.append({
                    "type": item_type,
                    "title": title,
                    "entity_id": entity_id,
                })
                
            # 3. Enqueue enrichment to catch edges
            if source_type == "document" and extracted_text:
                enqueue_enrichment(
                    job_type="doc_enrich",
                    target_type="document",
                    target_id=source_id,
                    content=extracted_text
                )
                
        except Exception:
            import traceback
            traceback.print_exc()
            for table, eid in created_entity_refs:
                try:
                    supabase.table(table).delete().eq("id", eid).execute()
                except Exception:
                    pass
            raise HTTPException(status_code=500, detail="Failed to create all items")
            
        return {
            "success": True,
            "created_items": created_items,
            "count": len(created_items),
        }
    except HTTPException:
        raise
    except Exception:
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail="Internal server error")
