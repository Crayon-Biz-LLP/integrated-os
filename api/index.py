import os
import hmac
import hashlib
import time
from datetime import datetime, timedelta
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware

from core.webhook import process_webhook, send_draft_reply, process_email_pending_decision
from core.pulse import (
    process_pulse,
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
    # HMAC-SHA256 verification for Pulse trigger requests
    raw_body = await request.body()
    sig_header = request.headers.get('X-Rhodey-Signature', '')
    
    pulse_secret = os.getenv("PULSE_SECRET")
    if not verify_hmac(raw_body, sig_header, pulse_secret):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    # Extracts the secret from the GitHub Actions cron header
    secret = request.headers.get("x-pulse-secret")
    
    # Executes the strategic briefing logic
    result = await process_pulse(auth_secret=secret)
    
    # Gatekeeper error handling
    if result.get("error"):
        raise HTTPException(status_code=result.get("status", 500), detail=result["error"])
        
    return {"success": True, "briefing": result.get("briefing")}

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