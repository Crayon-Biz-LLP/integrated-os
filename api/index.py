# api/index.py
import os
from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware

# Updated imports: Pulling from your new 'core' module
from core.webhook import process_webhook
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
    await process_webhook(update)
    return {"success": True}

# --- THE PULSE ENGINE (Routes to pulse.py) ---
@app.post("/api/pulse")
async def pulse_route_post(request: Request):
    # Extracts the secret from the GitHub Actions cron header
    secret = request.headers.get("x-pulse-secret")
    
    # Executes the strategic briefing logic
    result = await process_pulse(auth_secret=secret)
    
    # Gatekeeper error handling
    if result.get("error"):
        raise HTTPException(status_code=result.get("status", 500), detail=result["error"])
        
    return {"success": True, "briefing": result.get("briefing")}