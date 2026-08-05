import os
import hmac
import hashlib
import time
import httpx
import json
import uuid
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone, timedelta
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from core.lib.audit_logger import trace_id_var
from core.lib.telemetry import emit_observation
from core.decisions import record_decision
from core.actions import begin_action_context, clear_action_context

from core.webhook import (
    process_channel_pending_decision,
    process_webhook,
    send_draft_reply,
    process_email_pending_decision,
)
from core.pulse.graph import process_pending_edge_decision
from api.briefing import _snooze_ok, _notes_ok
from core.skills.whatsapp_ingest import process_whatsapp_message
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
    get_google_creds,
    format_rfc3339,
)
from core.pulse.tools import skip_recurring_instance
from core.pulse.pipeline import run_full_health_check
from core.services.db import get_supabase, maybe_single_safe, exec_query


@asynccontextmanager
async def lifespan(app):
    """FastAPI lifespan: initializes asyncpg pool on startup, closes on shutdown.

    Also upgrades the thread pool from default (min(32, 6)=6) to 16 workers
    because interrogate_brain fires 17+ sync Supabase calls via asyncio.to_thread().
    """
    # Startup: initialize asyncpg pool (hot-path reads only)
    try:
        from core.services.async_db import init_pool
        await init_pool()
        print("✅ asyncpg pool initialized successfully")
    except Exception as e:
        # Fail-open: if asyncpg fails, PostgREST still works
        print(f"⚠️ asyncpg pool init failed (non-fatal): {e}")

    yield

    # Shutdown: close asyncpg pool
    try:
        from core.services.async_db import close_pool
        await close_pool()
        print("✅ asyncpg pool closed")
    except Exception as e:
        print(f"⚠️ asyncpg pool close error (non-fatal): {e}")


app = FastAPI(title="Integrated-OS", lifespan=lifespan)

# CORS setup for future dashboard scalability
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
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

def require_api_auth(request: Request):
    api_key = request.headers.get("X-API-Key")
    expected = os.getenv("API_SECRET_KEY")
    if not expected:
        return
    if not api_key or not hmac.compare_digest(api_key, expected):
        raise HTTPException(status_code=401, detail="Unauthorized")

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

# --- THE SENTINEL WATCHER (Vercel Cron) ---
@app.api_route("/api/sentinel", methods=["GET", "POST"])
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
@app.api_route("/api/decision-pulse", methods=["GET", "POST"])
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
@app.api_route("/api/maintenance", methods=["GET", "POST"])
async def maintenance_redirect_route(request: Request):
    """Redirect to /api/health. Old route kept for backward compat."""
    return await health_check_route(request)

# --- HEALTH CHECK (replaces old /api/maintenance) ---
@app.api_route("/api/health", methods=["GET", "POST"])
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
        supabase = get_supabase()
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
            query = query.eq('status', status)
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
        supabase = get_supabase()
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
        supabase = get_supabase()
        briefing = await build_briefing(supabase)
        # Deep-serialize through JSON to strip ALL nested TypedDict subclasses
        # FastAPI's jsonable_encoder chokes on TypedDict subclasses on Vercel
        return json.loads(json.dumps(briefing, default=str))
    except Exception as e:
        print(f"Briefing error: {e}")
        import traceback
        traceback.print_exc()
        return {
            "greeting": "Hey, Danny.",
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
      tasks             → same rows as /api/tasks?status=todo (limit 200)
    """
    require_api_auth(request)
    try:
        supabase = get_supabase()

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
                    .select('id, source_label, target_label, relationship, status, context, confidence, created_at') \
                    .in_('status', ['pending', 'flagged'])
                if _snooze_ok(supabase, 'pending_graph_edges'):
                    q = q.or_('snoozed_until.is.null,snoozed_until.lt.now')
                res = await exec_query(q.order('created_at', desc=True).limit(100))
                return res.data or []
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

        # ── Active tasks (mirror /api/tasks default: status=todo, limit 200) ──
        async def _tasks():
            try:
                # NOTE: requires migration 75 (tasks.organization_id -> graph_nodes).
                select_cols = ('id, title, status, priority, deadline, created_at, '
                               'organization_id, direction, committed_to, recurrence, '
                               'graph_nodes(label)')
                if _notes_ok(supabase, 'tasks'):
                    select_cols += ', notes'
                q = supabase.table('tasks') \
                    .select(select_cols) \
                    .eq('is_current', True) \
                    .in_('status', ['todo'])
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

        nodes_fut = asyncio.ensure_future(_nodes())
        edges_fut = asyncio.ensure_future(_edges())
        merges_fut = asyncio.ensure_future(_merges())
        msgs_fut = asyncio.ensure_future(_messages())
        tasks_fut = asyncio.ensure_future(_tasks())

        briefing, nodes, edges, merges, messages, tasks = await asyncio.gather(
            briefing_fut, nodes_fut, edges_fut, merges_fut, msgs_fut, tasks_fut)

        return {
            "briefing": briefing,
            "pending_nodes": nodes,
            "pending_edges": edges,
            "pending_merges": merges,
            "pending_messages": messages,
            "tasks": tasks,
        }
    except Exception as e:
        print(f"Home feed error: {e}")
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail="Internal server error")


_HOME_FEED_BRIEFING_CACHE_KEY = "rhodey:briefing:home_feed:v1"


async def _home_feed_briefing():
    """Build the briefing payload for /api/home-feed (defensive wrapper).

    Read-through Redis cache (2 min TTL): the briefing is a generated
    artifact (LLM + 9 data sources) that takes 9-16s to rebuild and was
    blocking the event loop for every request queued behind it. Within the
    TTL window home-feed serves the cached payload in ~1s — Pulse
    regenerates the underlying data on its own schedule, so staleness is
    bounded and deliberate. Cache failures fail open to a live build.
    """
    from api.briefing import build_briefing
    from core.lib.redis_cache import cache_get, cache_set

    try:
        cached = cache_get(_HOME_FEED_BRIEFING_CACHE_KEY)
        if cached is not None and isinstance(cached, dict):
            return cached
    except Exception as e:
        print(f"[Briefing] Cache read failed (non-fatal): {e}")

    supabase = get_supabase()
    briefing = await build_briefing(supabase)
    payload = json.loads(json.dumps(briefing, default=str))

    try:
        cache_set(_HOME_FEED_BRIEFING_CACHE_KEY, payload, ttl=120)
    except Exception as e:
        print(f"[Briefing] Cache write failed (non-fatal): {e}")
    return payload

# --- EVENING ROUNDUP ---
@app.api_route("/api/roundup", methods=["GET", "POST"])
async def roundup_route(request: Request):
    """Triggered by cron-job.org — evening roundup prompt."""
    auth_header = request.headers.get("Authorization", "")
    cron_secret = os.getenv("CRON_SECRET", os.getenv("PULSE_SECRET"))

    if not cron_secret:
        raise HTTPException(status_code=500, detail="CRON_SECRET missing")

    if auth_header != f"Bearer {cron_secret}" and request.headers.get("x-pulse-secret") != cron_secret:
        raise HTTPException(status_code=401, detail="Unauthorized")

    try:
        from core.services.db import get_supabase
        from datetime import datetime, timezone, timedelta
        from core.webhook.telegram import send_telegram

        supabase = get_supabase()
        
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
                return {"success": True, "message": "Already captured enough notes today. Skipping prompt."}
            
        chat_id = os.getenv("TELEGRAM_CHAT_ID")
        if not chat_id:
            raise HTTPException(status_code=500, detail="TELEGRAM_CHAT_ID missing")
            
        await send_telegram(int(chat_id), "🌆 Evening roundup — any meeting notes, ideas, or project updates from today?")
        
        return {"success": True, "message": "Roundup prompt sent"}
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
        try:
            supabase = get_supabase()
            from api.briefing import build_briefing
            briefing = await build_briefing(supabase)
            briefing_update = json.loads(json.dumps(briefing, default=str))

            from core.services.push_notification import send_silent_push
            push_payload = {"type": "briefing_refresh"}

            headline = briefing_update.get('voice_line')
            if not headline:
                headline = briefing_update.get('context_bar')
            if not headline:
                headline = briefing_update.get('greeting', '')

            mode = briefing_update.get('home_mode', 'proceed')
            insights_list = []

            if mode == 'sprint':
                nxt = briefing_update.get('next_event')
                if nxt:
                    insights_list.append({"text": f"🎯 Sprinting: {nxt}", "link": "rhodey://today"})
                v_urg = briefing_update.get('vaulted_urgent_count', 0)
                if v_urg > 0:
                    insights_list.append({"text": f"🔴 {v_urg} urgent", "link": "rhodey://surface"})
            elif mode == 'decide':
                pend = briefing_update.get('pending_count', 0)
                if pend > 0:
                    insights_list.append({"text": f"⚖️ {pend} pending decisions", "link": "rhodey://inbox"})
            else:
                for ins in briefing_update.get('insights', []):
                    insights_list.append({"text": ins, "link": "rhodey://surface"})

            push_payload['headline'] = headline
            push_payload['insights_json'] = json.dumps(insights_list)
            await send_silent_push(push_payload)
        except Exception as brief_err:
            print(f"Send-message briefing/push error (non-critical): {brief_err}")

        return response_text, resulting_session_id
    finally:
        clear_action_context()


@app.post("/api/send-message")
async def send_message_route(request: Request):
    require_api_auth(request)
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
        
        supabase = get_supabase()

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

        # Fallback (local dev / non-Modal): run the full pipeline inline
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

# --- GET MESSAGE HISTORY ---
@app.get("/api/messages")
async def get_messages_route(request: Request, limit: int = 50, offset: int = 0):
    require_api_auth(request)
    try:
        supabase = get_supabase()
        result = supabase.table('raw_dumps')\
            .select('id, content, created_at, direction, sender, message_type, status, metadata, source')\
            .order('created_at', desc=True)\
            .limit(limit)\
            .offset(offset)\
            .execute()
        return {"messages": result.data or []}
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
        supabase = get_supabase()
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
            entries.append({
                'id': f"r{row['id']}",
                'role': 'bot',
                'intent': 'BRIEFING' if source in ('pulse', 'pulse_engine') else 'RESPONSE',
                'content': content,
                'created_at': created,
                'session_id': None,
                'metadata': row.get('metadata'),
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
        from googleapiclient.discovery import build

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
        # fetches live, exactly as before.
        cache_key = f"rhodey:calendar:{date or (f'{start}|{end}' if start and end else 'today')}"
        from core.lib.redis_cache import cache_get, cache_set
        cached = cache_get(cache_key)
        if cached is not None and isinstance(cached, list):
            return {"events": cached}

        simplified = []

        # Google + Outlook calls are blocking network I/O — run them in a
        # worker thread so a slow provider can't stall the event loop (and
        # every other request queued behind this one).
        def _fetch_google():
            service = build('calendar', 'v3', credentials=get_google_creds())
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
    supabase = get_supabase()

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
    try:
        from core.lib.redis_cache import cache_delete
        cache_delete(_HOME_FEED_BRIEFING_CACHE_KEY)
    except Exception:
        pass

    new_task_res = supabase.table('tasks').select('*').eq('supersedes_id', task_id).eq('is_current', True).single().execute()
    new_task = new_task_res.data if new_task_res.data else task

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
        supabase = get_supabase()
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
    supabase = get_supabase()
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

        supabase = get_supabase()
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

        supabase = get_supabase()
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
        supabase = get_supabase()

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

    return {"success": True, "message": f"Merged '{pending_label}' into canonical node."}


# --- FOCAL ITEM ACTION (Phase 2 v2: done/snooze/correct/reject) ---
@app.post("/api/focal-action")
async def focal_action_route(request: Request):
    """Handle the three-button focal card actions.

    Body: {
        "action": "done",       // "done", "snooze", "correct"
        "item_type": "task",    // "task", "graph_node", "graph_edge", "merge"
        "item_id": "123",       // Task ID or pending item ID
        "title": "Fill DBS forms",
        "reason": "Blocking Qhord"   // Original LLM reason (for learning)
    }

    - "done":    Marks the task/decision as completed
    - "snooze":  Persists a 7-day deferral (snoozed_until) so the item does
                  NOT resurface on the next load. No learning signal.
    - "correct": Same deferral + correction signal to the learning loop.
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

        # Shared: persist a 7-day deferral so a snoozed/corrected item stops
        # resurfacing until the deferral expires (see db/72_focal_snooze.sql).
        # Returns True only if the row was actually updated — the caller must
        # surface a failure instead of telling the user the item was dismissed.
        async def _persist_deferral() -> bool:
            try:
                from core.services.db import get_supabase
                supabase = get_supabase()
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
                from core.services.db import get_supabase
                supabase = get_supabase()
                return await _accept_merge_proposal(supabase, iid)
            elif item_type in ("graph_node", "graph_edge"):
                iid = _item_int()
                if iid is None:
                    return _unactionable
                from core.pulse.graph import process_pending_edge_decision, process_graph_pending_decision
                from core.services.db import get_supabase
                supabase = get_supabase()
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
            return {"success": True, "message": "Action completed"}

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
                from core.services.db import get_supabase
                supabase = get_supabase()
                return await _reject_merge_proposal(supabase, iid)
            return {"success": True, "message": "Action completed"}

        elif action == "snooze":
            # Persist a real deferral — the item must NOT resurface on the
            # next load. No learning signal (defer ≠ reject).
            persisted = await _persist_deferral()
            if not persisted:
                return {"success": False, "message": "Couldn't defer this item — please try again."}
            return {"success": True, "message": "Dismissed — I'll keep it off your board for now"}

        elif action == "correct":
            # Defer the item too (so a corrected focal pick doesn't resurface)
            # AND emit a correction signal for the learning loop. If the
            # deferral fails, still record the correction but be honest about it.
            persisted = await _persist_deferral()
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
                return {"success": False, "message": "Correction noted, but I couldn't defer this item — it may resurface."}
            return {"success": True, "message": "Correction recorded — Rhodey will learn from this."}

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

        supabase = get_supabase()
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

        result = await process_email_pending_decision(int(pending_id), action)

        if result['success']:
            return {"success": True, "message": result['message'], "action": result['action']}
        else:
            return {"success": False, "message": result['message'], "action": result['action']}

    except Exception as e:
        print(f"Email action error: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


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
            supabase = get_supabase()
            pending_res = supabase.table('messages') \
                .select('id') \
                .is_('danny_decision', 'null') \
                .eq('channel', 'email') \
                .eq('classification', 'actionable') \
                .execute()
            ids = [r['id'] for r in (pending_res.data or [])]
            if not ids:
                return {"success": True, "processed": 0, "failed": 0}

        processed, failed = 0, 0
        for i in range(0, len(ids), 100):
            for pending_id in ids[i:i+100]:
                try:
                    await process_email_pending_decision(int(pending_id), action)
                    processed += 1
                except Exception:
                    failed += 1
        return {"success": True, "processed": processed, "failed": failed}
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
        supabase = get_supabase()
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
        supabase = get_supabase()
        now = datetime.now(timezone.utc)
        cutoff = (now - timedelta(minutes=30)).isoformat()

        decision_res = supabase.table('decisions') \
            .select('id, decision_type') \
            .eq('auto_decided', True) \
            .eq('status', 'active') \
            .is_('verified_at', None) \
            .gte('decided_at', cutoff) \
            .execute()

        confirmed_count = 0
        for row in (decision_res.data or []):
            supabase.table('decisions').update({
                'verified_at': now.isoformat(),
            }).eq('id', row['id']).execute()
            confirmed_count += 1

        if confirmed_count > 0:
            await emit_observation(
                subsystem='auto_decisions',
                event_type='verification',
                outcome='confirmed',
                predicted='auto_approve',
                actual='verified',
                features={'count': confirmed_count}
            )
            print(f"User confirmed {confirmed_count} auto-decisions via app")

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
        supabase = get_supabase()
        now = datetime.now(timezone.utc)
        cutoff = (now - timedelta(minutes=30)).isoformat()

        decision_type = {
            'channels': 'channel_approval',
            'graph': 'graph_node_approval',
            'edge': 'graph_edge_approval',
        }[undo_target]

        decision_res = supabase.table('decisions').select('id, entity_id, decision_type') \
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
        processed, failed = 0, 0
        for i in range(0, len(ids), 100):
            for pending_id in ids[i:i+100]:
                try:
                    await process_channel_pending_decision('call', int(pending_id), action)
                    processed += 1
                except Exception:
                    failed += 1
        return {"success": True, "processed": processed, "failed": failed}
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
        processed, failed = 0, 0
        for i in range(0, len(ids), 100):
            for pending_id in ids[i:i+100]:
                try:
                    await process_channel_pending_decision('whatsapp', int(pending_id), action)
                    processed += 1
                except Exception:
                    failed += 1
        return {"success": True, "processed": processed, "failed": failed}
    except HTTPException:
        raise
    except Exception as e:
        print(f"WhatsApp batch action error: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


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
            supabase = get_supabase()
            pending_res = supabase.table('pending_graph_edges') \
                .select('id') \
                .eq('status', 'pending') \
                .execute()
            ids = [r['id'] for r in (pending_res.data or [])]
            if not ids:
                return {"success": True, "processed": 0, "failed": 0}

        processed, failed = 0, 0
        for i in range(0, len(ids), 100):
            for pending_id in ids[i:i+100]:
                try:
                    await process_pending_edge_decision(pending_id=int(pending_id), decision=action)
                    processed += 1
                except Exception:
                    failed += 1
        return {"success": True, "processed": processed, "failed": failed}
    except HTTPException:
        raise
    except Exception:
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail="Internal server error")


@app.post("/api/clarification")
async def clarification_action_route(request: Request):
    """Handle clarification responses."""
    require_api_auth(request)
    try:
        body = await request.json()
        shortcode = body.get("shortcode")
        answer = body.get("answer")
        
        if not shortcode or not answer:
            raise HTTPException(status_code=400, detail="Missing shortcode or answer")
            
        from core.clarifier import handle_response
        result = handle_response(shortcode, answer)
        return result
    except HTTPException:
        raise
    except Exception as e:
        import logging
        logging.error(f"Clarification API error: {e}", exc_info=True)
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

        from core.services.db import get_supabase
        supabase = get_supabase()

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
            supabase = get_supabase()
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
                return {"success": True, "processed": 0, "failed": 0}

        from core.pulse.graph import process_graph_pending_decision
        processed, failed = 0, 0
        for i in range(0, len(ids), 100):
            for pending_id in ids[i:i+100]:
                try:
                    await process_graph_pending_decision(int(pending_id), action)
                    processed += 1
                except Exception:
                    failed += 1
        return {"success": True, "processed": processed, "failed": failed}
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
        
        from core.services.db import get_supabase, maybe_single_safe
        supabase = get_supabase()
        
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
            
        from core.services.db import get_supabase, maybe_single_safe
        supabase = get_supabase()
        
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

        from core.services.db import get_supabase, maybe_single_safe
        supabase = get_supabase()
        
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
            
        from core.services.db import get_supabase, maybe_single_safe
        supabase = get_supabase()
        
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
        supabase = get_supabase()
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
        supabase = get_supabase()
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
        supabase = get_supabase()
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
    trace_id_var.set(f"wa_{uuid.uuid4().hex[:8]}")
    expected_secret = os.getenv("WHATSAPP_INGEST_SECRET")
    if expected_secret:
        provided = request.headers.get("X-Ingest-Secret", "")
        if not hmac.compare_digest(provided, expected_secret):
            raise HTTPException(status_code=401, detail="Unauthorized")

    begin_action_context()
    try:
        try:
            body = await request.json()
        except json.JSONDecodeError:
            # MacroDroid sometimes sends payload with unescaped control characters (like newlines in text)
            raw_bytes = await request.body()
            body = json.loads(raw_bytes, strict=False)
            
        sender_name = body.get("sender", "") or body.get("sender_name", "")
        sender_phone = body.get("phone", "") or body.get("sender_phone", "")
        message_text = body.get("text", "") or body.get("body", "") or body.get("message", "")
        received_at = body.get("received_at") or body.get("timestamp")

        identifier = sender_phone or sender_name

        if not identifier or not message_text:
            raise HTTPException(status_code=400, detail="sender/phone and message required")

        # Fallback to name if phone is missing so downstream logic doesn't break
        if not sender_phone:
            sender_phone = sender_name

        result = await process_whatsapp_message(sender_name, sender_phone, message_text, received_at)
        return {"success": True, "result": result}

    except HTTPException:
        raise
    except Exception as e:
        print(f"WhatsApp ingest error: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")
    finally:
        clear_action_context()


# --- APP VERSION CHECK (for in-app updates) ---
@app.get("/api/app-version")
async def app_version_route(request: Request):
    """Return the latest app version info from core_config.

    The CI workflow records version info to the `core_config` table
    after each successful build. This endpoint reads from there,
    removing the dependency on GitHub API tokens.
    """
    try:
        supabase = get_supabase()
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
@app.post("/api/multimodal-input")
async def multimodal_input_route(request: Request):
    """Accept file uploads (images, audio, documents) from the Flutter app.

    Sends the file through the multimodal processing pipeline (same as Telegram).
    Returns the captured response text and updated briefing.
    """
    require_api_auth(request)
    try:
        form = await request.form()
        file = form.get("file")
        if not file:
            raise HTTPException(status_code=400, detail="file required")

        file_bytes = await file.read()
        mime_type = file.content_type or "application/octet-stream"

        from core.webhook.multimodal import process_multimodal_content
        from core.actions import get_captured_response
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
            briefing = await build_briefing(get_supabase())
            briefing_update = json.loads(json.dumps(briefing, default=str))
        except Exception:
            briefing_update = None

        return {
            "success": True,
            "response": response_text,
            "briefing_update": briefing_update,
        }
    except Exception as e:
        print(f"Multimodal input error: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


# --- REGISTER DEVICE TOKEN (for push notifications) ---
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
        
        supabase = get_supabase()
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
    if expected_token and channel_token != expected_token:
        raise HTTPException(status_code=401, detail="Unauthorized")

    print(f"Drive webhook: channel={channel_id} state={resource_state} resource={resource_id}")

    if resource_state == "sync":
        return {"success": True}

    if resource_state == "change":
        try:
            github_token = os.getenv("GITHUB_TOKEN")
            owner = os.getenv("GITHUB_OWNER", "Crayon-Biz-LLP")
            repo = os.getenv("GITHUB_REPO", "integrated-os")
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
        supabase = get_supabase()
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
        supabase = get_supabase()
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
        supabase = get_supabase()
        res = supabase.table('pending_graph_edges') \
            .select('id, source_label, target_label, relationship, status, context, confidence, created_at')
        res = res.in_('status', ['pending', 'flagged'])
        if _snooze_ok(supabase, 'pending_graph_edges'):
            res = res.or_('snoozed_until.is.null,snoozed_until.lt.now')
        res = res.order('created_at', desc=True).limit(100).execute()
        return {"data": res.data or []}
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
        supabase = get_supabase()

        # Supabase's client is SYNCHRONOUS — bare .execute() blocks the event
        # loop, so the parallel gather below would otherwise run serially and
        # stall concurrent requests. Offload to worker threads (exec_query).

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

        async def _edges():
            try:
                q = supabase.table('pending_graph_edges') \
                    .select('id, source_label, target_label, relationship, status, context, confidence, created_at') \
                    .in_('status', ['pending', 'flagged'])
                if _snooze_ok(supabase, 'pending_graph_edges'):
                    q = q.or_('snoozed_until.is.null,snoozed_until.lt.now')
                res = await exec_query(q.order('created_at', desc=True).limit(100))
                return res.data or []
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

        (nodes, edges, merges, messages, auto_count) = await asyncio.gather(
            _timed('pending_nodes', _nodes()),
            _timed('pending_edges', _edges()),
            _timed('pending_merges', _merges()),
            _timed('messages', _messages()),
            _timed('auto_count', _auto_count()),
        )

        total_ms = round((time.perf_counter() - _t0) * 1000, 1)
        print(
            f"[TIMING] /api/inbox total={total_ms}ms "
            f"sub={_sub_ms} "
            f"rows={{nodes:{len(nodes)},edges:{len(edges)},merges:{len(merges)},messages:{len(messages)},auto_count:{auto_count}}}"
        )

        return {
            "pending_nodes": nodes,
            "pending_edges": edges,
            "pending_merges": merges,
            "pending_messages": messages,
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
        from core.services.db import get_supabase
        supabase = get_supabase()

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

        from core.services.db import get_supabase, maybe_single_safe
        supabase = get_supabase()

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
