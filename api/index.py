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

from core.webhook import (
    process_webhook,
    send_draft_reply,
    process_email_pending_decision,
    process_call_pending_decision,
    process_whatsapp_pending_decision,
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
    versioned_update,
    write_outcome_memory,
    get_outlook_calendar_events,
    get_outlook_calendar_events_range,
    get_google_creds,
    format_rfc3339,
)
from core.pulse.tools import skip_recurring_instance
from core.services.db import get_supabase

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
    try:
        await process_webhook(update)
        return {"success": True}
    except Exception as e:
        print(f"Webhook route error: {e}")
        raise HTTPException(status_code=500, detail="Internal processing error")

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
        
    if not auth_header.endswith(cron_secret) and request.headers.get("x-pulse-secret") != cron_secret:
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

    if not auth_header.endswith(cron_secret) and request.headers.get("x-pulse-secret") != cron_secret:
        raise HTTPException(status_code=401, detail="Unauthorized")

    result = await process_decision_pulse(auth_secret=cron_secret, trigger="cron")
    return result

# --- EVENING ROUNDUP ---
@app.api_route("/api/roundup", methods=["GET", "POST"])
async def roundup_route(request: Request):
    """Triggered by cron-job.org — evening roundup prompt."""
    auth_header = request.headers.get("Authorization", "")
    cron_secret = os.getenv("CRON_SECRET", os.getenv("PULSE_SECRET"))

    if not cron_secret:
        raise HTTPException(status_code=500, detail="CRON_SECRET missing")

    if not auth_header.endswith(cron_secret) and request.headers.get("x-pulse-secret") != cron_secret:
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
            .select('id') \
            .in_('memory_type', ['note', 'Journal', 'relationship_note']) \
            .gte('created_at', start_of_day.isoformat()) \
            .execute()
            
        if notes_res.data and len(notes_res.data) >= 3:
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
        if task.get('recurrence') and new_status == 'done':
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

        versioned_update(
            table_name='tasks',
            record_id=task_id,
            update_data=update_data,
            change_source='web_done',
            change_reason=f"Status: {new_status}"
        )

        if new_status == 'done':
            proj_name = None
            proj_id = task.get('project_id')
            if proj_id:
                proj_lookup = supabase.table('projects').select('name').eq('id', proj_id).maybe_single().execute()
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

        result = await process_call_pending_decision(int(pending_id), action)

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

        result = await process_whatsapp_pending_decision(int(pending_id), action)

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

    except Exception as e:
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))

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
        raise HTTPException(status_code=500, detail=str(e))

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

        from core.services.db import get_supabase
        supabase = get_supabase()

        pending_row = supabase.table('pending_graph_nodes').select('*').eq('id', int(pending_id)).maybe_single().execute()
        if not pending_row or not pending_row.data:
            return {"success": False, "message": "Merge proposal not found."}

        pr = pending_row.data
        if pr.get('status') != 'merge_proposed':
            return {"success": False, "message": "Merge proposal already processed."}

        if action == 'reject':
            supabase.table('pending_graph_nodes').update({'status': 'rejected'}).eq('id', int(pending_id)).execute()
            return {"success": True, "message": f"Merge rejected for '{pr['label']}'."}

        target_id = pr.get('merge_candidate_id')
        if not target_id:
            return {"success": False, "message": "Merge candidate not found in proposal."}
            
        swap = body.get('swap', False)
        
        from core.lib.graph_rules import get_canonical_id
        
        source_node_res = supabase.table('graph_nodes').select('id, label').eq('label', pr['label']).maybe_single().execute()
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

        supabase.table('graph_nodes').update({'canonical_id': winner_id}).eq('id', loser_id).execute()
        
        # Rewire edges to point to the winner
        supabase.table('graph_edges').update({'source_node_id': winner_id}).eq('source_node_id', loser_id).execute()
        supabase.table('graph_edges').update({'target_node_id': winner_id}).eq('target_node_id', loser_id).execute()
        
        supabase.table('pending_graph_nodes').update({'status': 'approved'}).eq('id', int(pending_id)).execute()

        return {"success": True, "message": f"Merged '{pr['label']}' into canonical node."}

    except Exception as e:
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/graph-node-action")
async def graph_node_action_route(request: Request):
    """Approve or reject a pending graph node via UI."""
    require_api_auth(request)
    try:
        body = await request.json()
        pending_id = body.get('id')
        action = body.get('action')
        org_tag = body.get('org_tag')
        
        if not pending_id or action not in ('approve', 'reject'):
            raise HTTPException(status_code=400, detail="id and valid action (approve/reject) required")
            
        from core.pulse.graph import process_graph_pending_decision
        result = await process_graph_pending_decision(int(pending_id), action, org_tag=org_tag)
        
        if not result.get("success"):
            raise HTTPException(status_code=400, detail=result.get("message", "Failed to process node decision"))
            
        return result
    except HTTPException:
        raise
    except Exception as e:
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/graph-nodes/search")
async def graph_nodes_search_route(request: Request):
    require_api_auth(request)
    q = request.query_params.get('q', '').strip()
    node_type = request.query_params.get('type')
    if not q or len(q) < 2:
        return {"data": []}
    
    from core.services.db import get_supabase
    supabase = get_supabase()
    query = supabase.table('graph_nodes').select('id, label, type').ilike('label', f"%{q}%")
    if node_type:
        query = query.eq('type', node_type)
    
    res = query.order('label').limit(10).execute()
    return {"data": res.data or []}

@app.post("/api/graph-node-merge")
async def graph_node_merge_route(request: Request):
    require_api_auth(request)
    try:
        body = await request.json()
        pending_id = body.get('id')
        target_id = body.get('target_id')
        
        if not pending_id or not target_id:
            raise HTTPException(status_code=400, detail="id and target_id required")
            
        from core.lib.graph_rules import propose_merge
        from core.services.db import get_supabase
        supabase = get_supabase()
        
        target_res = supabase.table('graph_nodes').select('id, label').eq('id', target_id).maybe_single().execute()
        if not target_res or not target_res.data:
            raise HTTPException(status_code=400, detail="Target node not found")
        
        # Check if already approved/created somehow
        pending_res = supabase.table('pending_graph_nodes').select('label, type').eq('id', int(pending_id)).maybe_single().execute()
        if not pending_res or not pending_res.data:
            raise HTTPException(status_code=404, detail="Pending node not found")
            
        label = pending_res.data['label']
        
        source_res = supabase.table('graph_nodes').select('id').eq('label', label).maybe_single().execute()
        if not source_res or not source_res.data:
            # The pending node doesn't exist in graph_nodes yet.
            # Instead of trying to approve it (which might fail via dedup),
            # just directly update its status and skip graph_node creation entirely.
            supabase.table('pending_graph_nodes').update({
                'status': 'merge_proposed',
                'merge_candidate_id': target_id,
                'source_text': 'manual_merge_override'
            }).eq('id', int(pending_id)).execute()
            return {"success": True, "message": "Merge proposed successfully (alias created)."}
            
        # If it DOES exist in graph_nodes already, propose the merge normally
            
        source_id = source_res.data['id']
        
        merge_res = propose_merge(source_id, target_id)
        if not merge_res.get("success"):
            # If propose_merge says "Already proposed", just update pending_graph_nodes to point to our target
            if merge_res.get("message") == "Already proposed":
                 supabase.table('pending_graph_nodes').update({
                    'status': 'merge_proposed',
                    'merge_candidate_id': target_id
                }).eq('id', int(pending_id)).execute()
                 return {"success": True, "message": "Merge updated."}
            raise HTTPException(status_code=400, detail=merge_res.get("message", "Merge proposal failed"))
            
        # Update the original pending node to reflect the merge proposal instead of 'approved' 
        # so it moves to the Merges tab in UI
        supabase.table('pending_graph_nodes').update({
            'status': 'merge_proposed',
            'merge_candidate_id': target_id
        }).eq('id', int(pending_id)).execute()
            
        return merge_res
    except HTTPException:
        raise
    except Exception as e:
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/whatsapp-ingest")
async def whatsapp_ingest_route(request: Request):
    trace_id_var.set(f"wa_{uuid.uuid4().hex[:8]}")
    expected_secret = os.getenv("WHATSAPP_INGEST_SECRET")
    if expected_secret:
        provided = request.headers.get("X-Ingest-Secret", "")
        if not hmac.compare_digest(provided, expected_secret):
            raise HTTPException(status_code=401, detail="Unauthorized")

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


# --- DRIVE WEBHOOK (Receives Google Drive push notifications) ---
@app.post("/api/drive-webhook")
async def drive_webhook(request: Request):
    channel_id = request.headers.get("X-Goog-Channel-ID", "")
    resource_state = request.headers.get("X-Goog-Resource-State", "")
    resource_id = request.headers.get("X-Goog-Resource-ID", "")

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