# api/index.py
import os
import hmac
import hashlib
import httpx
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware

# Updated imports: Pulling from your new 'core' module
from core.webhook import process_webhook, send_draft_reply
from core.pulse import process_pulse

app = FastAPI(title="Integrated-OS")

# CORS setup for future dashboard scalability
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
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
    body = await request.json()
    draft_id = body.get("draft_id")
    if not draft_id:
        raise HTTPException(status_code=400, detail="draft_id required")
    success, error = await send_draft_reply(draft_id)
    return {"success": success, "error": error}

# --- SEND MESSAGE VIA WEB UI (Mirrors Telegram exactly) ---
@app.post("/api/send-message")
async def send_message_route(request: Request):
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
        
        import time
        
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
        raise HTTPException(status_code=500, detail=str(e))

# --- GET MESSAGE HISTORY ---
@app.get("/api/messages")
async def get_messages_route(limit: int = 50, offset: int = 0):
    try:
        from supabase import create_client
        
        supabase = create_client(
            os.getenv("SUPABASE_URL"),
            os.getenv("SUPABASE_SERVICE_ROLE_KEY")
        )
        
        result = supabase.table('raw_dumps')\
            .select('id, content, created_at, direction, sender, message_type, status, metadata, source')\
            .order('created_at', desc=True)\
            .limit(limit)\
            .offset(offset)\
            .execute()
        
        return {"messages": result.data or []}
    
    except Exception as e:
        print(f"Get messages error: {e}")
        raise HTTPException(status_code=500, detail=str(e))