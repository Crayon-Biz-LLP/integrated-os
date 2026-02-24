from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from .webhook import process_webhook
from .pulse import process_pulse
import os

app = FastAPI(title="Integrated-OS")

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

@app.post("/api/webhook")
async def webhook_route(request: Request):
    update = await request.json()
    await process_webhook(update)
    return {"success": True}

@app.all("/api/pulse")
async def pulse_route(request: Request):
    secret = request.headers.get("x-pulse-secret")
    if secret != os.getenv("PULSE_SECRET"):
        raise HTTPException(status_code=401, detail="Unauthorized")
    
    is_manual = request.headers.get("x-manual-trigger") == 'true'
    await process_pulse(is_manual)
    return {"success": True}
