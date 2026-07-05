import os
import hmac
import hashlib
import time
import httpx
import json
import uuid
from datetime import datetime
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from core.lib.audit_logger import trace_id_var
from core.actions import begin_action_context, clear_action_context

from core.webhook import (
    process_channel_pending_decision,
    process_webhook,
    send_draft_reply,
    process_email_pending_decision,
    
    
)
from core.pulse.graph import process_pending_edge_decision
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
from core.pulse.maintenance import process_maintenance
from core.services.db import get_supabase, maybe_single_safe

app = FastAPI(title="Integrated-OS")

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

# --- TELEGRAM INTAKE (Routes to webhook.py) ---
@app.post("/api/webhook")
async def webhook_route(request: Request):
    update = await request.json()
    req_id = f"tg_{update.get('update_id', uuid.uuid4().hex[:8])}"
    trace_id_var.set(req_id)
    begin_action_context()
    try:
        await process_webhook(update)
        return {"success": True}
    except Exception as e:
        print(f"Webhook route error: {e}")
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

# --- MAINTENANCE RUNNER (Independent of Sentinel) ---
@app.api_route("/api/maintenance", methods=["GET", "POST"])
async def maintenance_route(request: Request):
    """Triggered by cron-job.org or GitHub Actions — runs maintenance tasks.

    Independent of the sentinel process. If sentinel fails, maintenance still runs.
    Supports query param ?mode=standard|daily|weekly.
    """
    auth_header = request.headers.get("Authorization", "")
    cron_secret = os.getenv("CRON_SECRET", os.getenv("PULSE_SECRET"))

    if not cron_secret:
        raise HTTPException(status_code=500, detail="CRON_SECRET missing")

    if auth_header != f"Bearer {cron_secret}" and request.headers.get("x-pulse-secret") != cron_secret:
        raise HTTPException(status_code=401, detail="Unauthorized")

    mode = request.query_params.get("mode", "standard")
    if mode not in ("standard", "daily", "weekly"):
        raise HTTPException(status_code=400, detail="mode must be standard, daily, or weekly")

    result = await process_maintenance(mode=mode)
    return result


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
@app.post("/api/send-message")
async def send_message_route(request: Request):
    require_api_auth(request)
    try:
        body = await request.json()
        message_text = body.get("message")
        if not message_text:
            raise HTTPException(status_code=400, detail="message required")
        
        # Validate Telegram credentials
        telegram_bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
        telegram_chat_id = os.getenv("TELEGRAM_CHAT_ID")
        
        if not telegram_bot_token or not telegram_chat_id:
            raise HTTPException(status_code=500, detail="Telegram credentials not configured")
        
        # Create a fake Telegram update object (mirrors what Telegram sends)
        # Prefix update_id with "web_" to identify web UI messages
        fake_update = {
            "update_id": f"web_{int(time.time() * 1000)}",
            "message": {
                "chat": {"id": int(telegram_chat_id)},
                "text": message_text,
                "date": int(time.time())
            }
        }
        
        # Process exactly like Telegram webhook
        print(f"🧪 Processing web message as Telegram update: {fake_update}")
        result = await process_webhook(fake_update)
        print(f"🧪 Webhook result: {result}")
        
        return {"success": True, "message": "Message processed"}
    
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

        simplified = []

        service = build('calendar', 'v3', credentials=get_google_creds())
        events_res = service.events().list(
            calendarId='primary',
            timeMin=rfc_start,
            timeMax=rfc_end,
            singleEvents=True,
            orderBy='startTime',
            maxResults=50
        ).execute()
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
            outlook_events = get_outlook_calendar_events_range(start_dt, end_dt) \
                if start and end else get_outlook_calendar_events(start_dt)
            for e in outlook_events:
                simplified.append({
                    'id': e.get('id'),
                    'summary': e.get('title'),
                    'start': {'dateTime': e['time']},
                    'source': 'outlook',
                })
        except Exception as ol_err:
            print(f"Outlook calendar events error: {ol_err}")

        return {"events": simplified}
    except Exception as e:
        print(f"Calendar events error: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")

# --- UPDATE TASK STATUS (Mark Done) ---
@app.patch("/api/tasks/{task_id}/status")
async def update_task_status(request: Request, task_id: int):
    require_api_auth(request)
    try:
        body = await request.json()
        new_status = body.get('status', 'done')

        supabase = get_supabase()

        task_res = supabase.table('tasks').select('*').eq('id', task_id).eq('is_current', True).single().execute()
        if not task_res.data:
            raise HTTPException(status_code=404, detail="Task not found")

        task = task_res.data
        current_status = task.get('status')
        if current_status in ['done', 'cancelled']:
            return {"success": True, "task": task, "message": f"Task already {current_status}"}

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
            proj_name = None
            proj_id = task.get('project_id')
            if proj_id:
                proj_lookup = maybe_single_safe(supabase.table('projects').select('name').eq('id', proj_id))
                proj_name = proj_lookup.data['name'] if proj_lookup.data else None
            await write_outcome_memory(task_title, proj_name)

        new_task_res = supabase.table('tasks').select('*').eq('supersedes_id', task_id).eq('is_current', True).single().execute()
        new_task = new_task_res.data if new_task_res.data else task

        return {"success": True, "task": new_task}

    except HTTPException:
        raise
    except Exception as e:
        print(f"Update task status error: {e}")
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
        pending_id = body.get('id')
        action = body.get('action', '')

        if not pending_id or action not in ('accept', 'reject'):
            raise HTTPException(status_code=400, detail="id and valid action (accept/reject) required")

        from core.services.db import get_supabase, maybe_single_safe
        supabase = get_supabase()

        pending_row = maybe_single_safe(supabase.table('pending_graph_nodes').select('*').eq('id', int(pending_id)))
        if not pending_row or not pending_row.data:
            return {"success": False, "message": "Merge proposal not found."}

        pr = pending_row.data
        if pr.get('status') != 'merge_proposed':
            return {"success": False, "message": "Merge proposal already processed."}

        if action == 'reject':
            from core.pulse.graph import create_graph_node_with_db_record
            result = await create_graph_node_with_db_record(
                label=pr['label'],
                node_type=pr['type'],
                source_text=pr.get('source_text', ''),
                source_tag='pending_approval',
                force=True
            )
            if result.get('success'):
                supabase.table('pending_graph_nodes').update({
                    'status': 'approved',
                    'merge_candidate_id': None
                }).eq('id', int(pending_id)).execute()
                return {"success": True, "message": f"Keep both — approved '{pr['label']}' as separate node."}
            return {"success": False, "message": result.get('message', 'Failed to approve node')}

        target_id = pr.get('merge_candidate_id')
        if not target_id:
            return {"success": False, "message": "Merge candidate not found in proposal."}
            
        swap = body.get('swap', False)
        
        from core.lib.graph_rules import get_canonical_id
        
        source_node_res = maybe_single_safe(supabase.table('graph_nodes').select('id, label').eq('label', pr['label']))
        source_node_id = source_node_res.data['id'] if source_node_res and source_node_res.data else None
        
        target_canonical = get_canonical_id(target_id)
        
        if not source_node_id:
            # The pending label was merged before it was ever created as a graph node.
            # No graph_nodes to rewire, just mark the pending proposal as approved (resolved).
            supabase.table('pending_graph_nodes').update({'status': 'approved'}).eq('id', int(pending_id)).execute()
            return {"success": True, "message": f"Pending label '{pr['label']}' is now aliased to the target node."}
            
        if swap:
            # Swap direction: the original target becomes the loser, the original source becomes the canonical winner
            loser_id = target_canonical
            winner_id = source_node_id
        else:
            # Normal direction
            loser_id = source_node_id
            winner_id = target_canonical

        from core.lib.graph_rules import execute_graph_node_merge
        execute_graph_node_merge(loser_id, winner_id, "ui_merge_accept")
        
        supabase.table('pending_graph_nodes').update({'status': 'approved'}).eq('id', int(pending_id)).execute()

        return {"success": True, "message": f"Merged '{pr['label']}' into canonical node."}

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
            concepts_res = supabase.table('pending_graph_nodes').select('id, eval_context').eq('type', 'concept').execute()
            if concepts_res and concepts_res.data:
                for c in concepts_res.data:
                    ctx = c.get('eval_context') or {}
                    if ctx.get('linked_entity') == old_label:
                        ctx['linked_entity'] = new_label
                        supabase.table('pending_graph_nodes').update({'eval_context': ctx}).eq('id', c['id']).execute()
            
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
            
            return {"success": True, "message": "Renamed live node"}

        if not new_label or not new_label.strip():
            raise HTTPException(status_code=400, detail="label required")
        
        new_label = new_label.strip()
        
        try:
            pending_id_int = int(pending_id)
        except ValueError:
            return {"success": False, "message": "Invalid pending ID"}
            
        pending_res = maybe_single_safe(supabase.table('pending_graph_nodes').select('label, type').eq('id', pending_id_int))
        if not pending_res or not pending_res.data:
            return {"success": False, "message": "Pending node not found"}
            
        old_label = pending_res.data['label']
        if old_label == new_label:
            return {"success": True, "message": "Label unchanged"}

        supabase.table('pending_graph_nodes').update({'label': new_label}).eq('id', pending_id_int).execute()
        
        supabase.table('pending_graph_edges').update({'source_label': new_label}).eq('source_label', old_label).execute()
        supabase.table('pending_graph_edges').update({'target_label': new_label}).eq('target_label', old_label).execute()
        
        # Also update linked_entity in concepts
        concepts_res = supabase.table('pending_graph_nodes').select('id, eval_context').eq('type', 'concept').execute()
        if concepts_res and concepts_res.data:
            for c in concepts_res.data:
                ctx = c.get('eval_context') or {}
                if ctx.get('linked_entity') == old_label:
                    ctx['linked_entity'] = new_label
                    supabase.table('pending_graph_nodes').update({'eval_context': ctx}).eq('id', c['id']).execute()

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
        
        if not new_type or new_type not in ['person', 'project', 'organization', 'concept', 'place', 'event', 'animal', 'emotional_state']:
            raise HTTPException(status_code=400, detail="valid type required")
            
        from core.services.db import get_supabase, maybe_single_safe
        supabase = get_supabase()
        
        if scope == 'live':
            live_res = maybe_single_safe(supabase.table('graph_nodes').select('id, label, type, db_record_id').eq('id', pending_id))
            if not live_res or not live_res.data:
                return {"success": False, "message": "Live node not found"}
            label = live_res.data['label']
            old_type = live_res.data.get('type')
            
            # --- Handle people table change type ---
            if old_type == 'person' and new_type != 'person':
                p_id = live_res.data.get('db_record_id')
                if p_id:
                    p_res = maybe_single_safe(supabase.table('people').select('role').eq('id', p_id))
                    old_role = p_res.data.get('role') if p_res and p_res.data else ""
                    old_role = old_role or ""
                    supabase.table('people').update({'role': f"{old_role} [CHANGED TO {new_type.upper()}]".strip(), 'strategic_weight': 0}).eq('id', p_id).execute()
            
            supabase.table('graph_nodes').update({'type': new_type}).eq('id', pending_id).execute()
            supabase.table('graph_type_overrides').upsert({'label': label, 'node_type': new_type}).execute()
            return {"success": True, "message": f"Changed type to {new_type}"}
            
        try:
            pending_id_int = int(pending_id)
        except ValueError:
            return {"success": False, "message": "Invalid pending ID"}
            
        pending_res = maybe_single_safe(supabase.table('pending_graph_nodes').select('id, label, type').eq('id', pending_id_int))
        if not pending_res or not pending_res.data:
            return {"success": False, "message": "Pending node not found"}
            
        label = pending_res.data['label']
        old_type = pending_res.data.get('type')
        
        # --- Handle people table change type for pending ---
        if old_type == 'person' and new_type != 'person':
            live_node = maybe_single_safe(supabase.table('graph_nodes').select('db_record_id').eq('label', label))
            if live_node and live_node.data:
                p_id = live_node.data.get('db_record_id')
                if p_id:
                    p_res = maybe_single_safe(supabase.table('people').select('role').eq('id', p_id))
                    old_role = p_res.data.get('role') if p_res and p_res.data else ""
                    old_role = old_role or ""
                    supabase.table('people').update({'role': f"{old_role} [CHANGED TO {new_type.upper()}]".strip(), 'strategic_weight': 0}).eq('id', p_id).execute()

        supabase.table('pending_graph_nodes').update({'type': new_type}).eq('id', pending_id_int).execute()
        supabase.table('graph_type_overrides').upsert({'label': label, 'node_type': new_type}).execute()
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
            
            # --- Handle people table delete ---
            if live_res.data.get('type') == 'person':
                p_id = live_res.data.get('db_record_id')
                if p_id:
                    p_res = maybe_single_safe(supabase.table('people').select('role').eq('id', p_id))
                    old_role = p_res.data.get('role') if p_res and p_res.data else ""
                    old_role = old_role or ""
                    supabase.table('people').update({'role': f"{old_role} [DELETED]".strip(), 'strategic_weight': 0}).eq('id', p_id).execute()
            
            # Cascade delete live edges
            supabase.table('graph_edges').delete().eq('source_node_id', pending_id).execute()
            supabase.table('graph_edges').delete().eq('target_node_id', pending_id).execute()
            
            # Reject pending edges referencing this deleted node label
            supabase.table('pending_graph_edges').update({'status': 'rejected'}).eq('source_label', label).execute()
            supabase.table('pending_graph_edges').update({'status': 'rejected'}).eq('target_label', label).execute()
            
            # Reject orphaned concept nodes
            orphaned = 0
            concepts_res = supabase.table('pending_graph_nodes').select('id, eval_context').eq('type', 'concept').in_('status', ['pending', 'flagged']).execute()
            if concepts_res and concepts_res.data:
                for c in concepts_res.data:
                    ctx = c.get('eval_context') or {}
                    if ctx.get('linked_entity') == label:
                        supabase.table('pending_graph_nodes').update({'status': 'rejected'}).eq('id', c['id']).execute()
                        orphaned += 1
                        
            supabase.table('graph_nodes').delete().eq('id', pending_id).execute()
            return {"success": True, "message": f"Deleted live node '{label}', {orphaned} orphaned concepts, and rejected matching pending edges"}
        
        try:
            pending_id_int = int(pending_id)
        except ValueError:
            return {"success": False, "message": "Invalid pending ID"}
            
        pending_res = maybe_single_safe(supabase.table('pending_graph_nodes').select('label, type').eq('id', pending_id_int))
        if not pending_res or not pending_res.data:
            return {"success": False, "message": "Pending node not found"}
            
        label = pending_res.data['label']
        
        # Reject the node
        supabase.table('pending_graph_nodes').update({'status': 'rejected'}).eq('id', pending_id_int).execute()
        
        # Reject related edges
        supabase.table('pending_graph_edges').update({'status': 'rejected'}).eq('source_label', label).execute()
        supabase.table('pending_graph_edges').update({'status': 'rejected'}).eq('target_label', label).execute()
        
        # Reject orphaned concept nodes
        orphaned = 0
        concepts_res = supabase.table('pending_graph_nodes').select('id, eval_context').eq('type', 'concept').in_('status', ['pending', 'flagged']).execute()
        if concepts_res and concepts_res.data:
            for c in concepts_res.data:
                ctx = c.get('eval_context') or {}
                if ctx.get('linked_entity') == label:
                    supabase.table('pending_graph_nodes').update({'status': 'rejected'}).eq('id', c['id']).execute()
                    orphaned += 1
                    
        # --- Handle people table & live node cleanup ---
        live_res = maybe_single_safe(supabase.table('graph_nodes').select('id, type, db_record_id').eq('label', label))
        if live_res and live_res.data:
            l_id = live_res.data['id']
            if live_res.data.get('type') == 'person':
                p_id = live_res.data.get('db_record_id')
                if p_id:
                    p_res = maybe_single_safe(supabase.table('people').select('role').eq('id', p_id))
                    old_role = p_res.data.get('role') if p_res and p_res.data else ""
                    old_role = old_role or ""
                    supabase.table('people').update({'role': f"{old_role} [DELETED]".strip(), 'strategic_weight': 0}).eq('id', p_id).execute()
                    
            supabase.table('graph_edges').delete().eq('source_node_id', l_id).execute()
            supabase.table('graph_edges').delete().eq('target_node_id', l_id).execute()
            supabase.table('graph_nodes').delete().eq('id', l_id).execute()
                    
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
            
            # --- Handle people table merge ---
            if source_type == 'person':
                s_meta = maybe_single_safe(supabase.table('graph_nodes').select('metadata').eq('id', loser_id))
                s_people_id = s_meta.data.get('metadata', {}).get('people_id') if s_meta and s_meta.data else None
                
                if s_people_id:
                    p_res = maybe_single_safe(supabase.table('people').select('role').eq('id', s_people_id))
                    old_role = p_res.data.get('role') if p_res and p_res.data else ""
                    old_role = old_role or ""
                    new_role = f"{old_role} [MERGED INTO: {target_label}]".strip()
                    supabase.table('people').update({'role': new_role, 'strategic_weight': 0}).eq('id', s_people_id).execute()
            
            # Canonicalise and rewire live edges
            supabase.table('graph_nodes').update({'canonical_id': winner_id}).eq('id', loser_id).execute()
            
            # Repoint pending edges referencing the merged source label
            supabase.table('pending_graph_edges').update({'source_label': target_label}).eq('source_label', source_label).execute()
            supabase.table('pending_graph_edges').update({'target_label': target_label}).eq('target_label', source_label).execute()
            
            # Update concept nodes linked_entity
            concepts_res = supabase.table('pending_graph_nodes').select('id, eval_context').eq('type', 'concept').execute()
            if concepts_res and concepts_res.data:
                for c in concepts_res.data:
                    ctx = c.get('eval_context') or {}
                    if ctx.get('linked_entity') == source_label:
                        ctx['linked_entity'] = target_label
                        supabase.table('pending_graph_nodes').update({'eval_context': ctx}).eq('id', c['id']).execute()
            
            # Do NOT delete source live node, keep it as a canonical alias pointer
            return {"success": True, "message": f"Merged live '{source_label}' into '{target_label}'"}
            
        # Source node (pending)
        source_res = maybe_single_safe(supabase.table('pending_graph_nodes').select('label, type').eq('id', pending_id))
        if not source_res or not source_res.data:
            return {"success": False, "message": "Source pending node not found"}
        source_label = source_res.data['label']
        source_type = source_res.data['type']
        
        # Target node - check if it's live graph_nodes or pending_graph_nodes
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
                ptarget_res = maybe_single_safe(supabase.table('pending_graph_nodes').select('label').eq('id', t_id))
                if ptarget_res and ptarget_res.data:
                    target_label = ptarget_res.data['label']
            except ValueError:
                pass
                
        if not target_label:
            return {"success": False, "message": "Target node not found"}
            
        # --- FIX: Check if pending source was already approved (has live graph_nodes entry) ---
        live_source = maybe_single_safe(supabase.table('graph_nodes').select('id').eq('label', source_label))
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
                
                # Handle people table merge
                if source_type == 'person':
                    s_node = maybe_single_safe(supabase.table('graph_nodes').select('db_record_id').eq('id', s_live_id))
                    s_pid = s_node.data.get('db_record_id') if s_node and s_node.data else None
                    if s_pid:
                        p_res = maybe_single_safe(supabase.table('people').select('role').eq('id', s_pid))
                        old_role = p_res.data.get('role') or ""
                        supabase.table('people').update({'role': f"{old_role} [MERGED INTO: {target_label}]".strip(), 'strategic_weight': 0}).eq('id', s_pid).execute()
                        
                # Update as merged alias instead of deleting
                supabase.table('graph_nodes').update({'canonical_id': target_id}).eq('id', s_live_id).execute()
            else:
                # If target is not a live node, we can't set canonical_id yet. We just delete the live source to prevent orphans, 
                # but ideally we merge it into the new live target later.
                supabase.table('graph_nodes').delete().eq('id', s_live_id).execute()
        # --------------------------------------------------------------------------------------
            
        if source_label == target_label:
            # Mark source since it's already the same name
            supabase.table('pending_graph_nodes').update({'status': 'merged'}).eq('id', pending_id).execute()
            return {"success": True, "message": "Nodes had same label. Source merged."}

        # Repoint pending edges
        supabase.table('pending_graph_edges').update({'source_label': target_label}).eq('source_label', source_label).execute()
        supabase.table('pending_graph_edges').update({'target_label': target_label}).eq('target_label', source_label).execute()
        
        # Update concept nodes
        concepts_res = supabase.table('pending_graph_nodes').select('id, eval_context').eq('type', 'concept').execute()
        if concepts_res and concepts_res.data:
            for c in concepts_res.data:
                ctx = c.get('eval_context') or {}
                if ctx.get('linked_entity') == source_label:
                    ctx['linked_entity'] = target_label
                    supabase.table('pending_graph_nodes').update({'eval_context': ctx}).eq('id', c['id']).execute()
                    
        # Mark source pending node as merged entirely
        supabase.table('pending_graph_nodes').update({'status': 'merged'}).eq('id', pending_id).execute()
        
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
        table_name = 'pending_graph_nodes' if scope == 'pending' else 'graph_nodes'
        query = supabase.table(table_name).select('id, label, type').ilike('label', f'%{q}%')
        if node_type:
            query = query.eq('type', node_type)
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
        
        # Also check pending_graph_nodes for exact/high matches
        supabase = get_supabase()
        pending_res = supabase.table('pending_graph_nodes').select('id, label, type').eq('type', node_type).execute()
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
        src_res = supabase.table('graph_nodes').select('id').ilike('label', source).execute()
        tgt_res = supabase.table('graph_nodes').select('id').ilike('label', target).execute()
        
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
@app.get("/api/graph-nodes/live")
async def graph_nodes_live_route(request: Request):
    require_api_auth(request)
    try:
        from core.services.db import get_supabase
        supabase = get_supabase()
        
        # Bring key nodes and conceptual/structural entities (exclude system tasks/memories)
        entity_types = ['person', 'project', 'organization', 'concept', 'place', 'event', 'animal', 'emotional_state']
        res = supabase.table('graph_nodes') \
            .select('id, label, type, created_at') \
            .in_('type', entity_types) \
            .is_('canonical_id', 'null') \
            .order('created_at', desc=True) \
            .limit(5000) \
            .execute()
        return {"data": res.data or []}
    except Exception:
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail="Internal server error")
