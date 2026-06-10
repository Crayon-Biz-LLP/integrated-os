import os
from datetime import datetime, timezone, timedelta
from googleapiclient.discovery import build
from supabase import create_client

from core.lib.audit_logger import audit_log_sync
from core.services.google_service import get_google_creds
from core.webhook.telegram import send_telegram
from core.pulse.calendar import MemoryCache
from core.llm.fallback import generate_content_with_fallback
from core.llm.config import WorkloadProfile

def get_upcoming_events(minutes_ahead=25):
    """Fetch events starting between now and X minutes from now."""
    service = build('calendar', 'v3', credentials=get_google_creds(), cache=MemoryCache())
    
    # Needs timezone awareness, use UTC because format_rfc3339 expects it or naive.
    # Google API requires RFC3339 format.
    now = datetime.now(timezone.utc)
    end_time = now + timedelta(minutes=minutes_ahead)
    
    rfc_start = now.isoformat()
    rfc_end = end_time.isoformat()

    try:
        events_res = service.events().list(
            calendarId='primary',
            timeMin=rfc_start,
            timeMax=rfc_end,
            singleEvents=True,
            orderBy='startTime'
        ).execute()
        
        return events_res.get('items', [])
    except Exception as e:
        audit_log_sync("sentinel", "ERROR", f"Failed to fetch upcoming events: {e}")
        return []

async def fetch_event_context(title: str, supabase):
    """Grabs a few relevant tasks or memories for the event context."""
    # Extremely basic text search on title words. In a prod app, we'd use pg_vector or embeddings.
    words = [w for w in title.split() if len(w) > 3]
    if not words:
        return ""
    
    query = " | ".join(words)
    context_str = ""
    try:
        # Check active tasks
        tasks_res = supabase.table('tasks')\
            .select('title, status')\
            .eq('is_current', True)\
            .not_.in_('status', ['done', 'cancelled'])\
            .text_search('title', query)\
            .limit(3)\
            .execute()
            
        if tasks_res.data:
            context_str += "📌 Relevant Pending Tasks:\n"
            for t in tasks_res.data:
                context_str += f"- {t['title']}\n"
    except Exception:
        pass
        
    return context_str

async def process_sentinel(auth_secret: str, trigger: str = "cron"):
    from core.pulse.run_logger import create_pulse_run, complete_pulse_run

    """Runs the Sentinel high-frequency scanner."""
    print("🛡️ Running Sentinel Nudge check...")
    supabase_url = os.getenv("SUPABASE_URL")
    supabase_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
    telegram_chat_id = os.getenv("TELEGRAM_CHAT_ID")
    
    if not supabase_url or not supabase_key or not telegram_chat_id:
        print("Sentinel missing env vars.")
        return {"error": "Missing env vars", "status": 500}

    supabase = create_client(supabase_url, supabase_key)
    run_id = await create_pulse_run(supabase, "sentinel", trigger)

    try:
        events = get_upcoming_events(minutes_ahead=25)
        if not events:
            print("No upcoming events in the next 25 mins.")
            await complete_pulse_run(supabase, run_id, status="completed",
                metadata={"reason": "no_upcoming", "alerted": 0})
            return {"success": True, "alerted": 0}

        now = datetime.now(timezone.utc)
        alerted_count = 0
        
        for event in events:
            try:
                event_id = event.get('id')
                title = event.get('summary', 'Untitled Event')
                start_raw = event.get('start', {}).get('dateTime')
                if not start_raw:
                    continue
                    
                start_dt = datetime.fromisoformat(start_raw.replace('Z', '+00:00'))
                mins_until = int((start_dt - now).total_seconds() / 60)
                
                if mins_until < 0 or mins_until > 20:
                    continue

                search_str = f"Sentinel_Sent:{event_id}"
                
                recent_log = supabase.table('audit_logs')\
                    .select('id')\
                    .eq('service', 'sentinel')\
                    .ilike('message', f"%{search_str}%")\
                    .limit(1)\
                    .execute()
                    
                if recent_log.data:
                    print(f"Skipping {title} (already nudged).")
                    continue
                    
                context = await fetch_event_context(title, supabase)
                
                msg = f"🚨 **ALARM: Meeting in {mins_until} mins!**\n📅 {title}"
                if context:
                    prompt = f"Write a 1-2 sentence maximum 'Pre-Flight Briefing' for a meeting called '{title}'. Here is some context from my system. Be extremely brief, do not use pleasantries. Just say what I need to know.\n\nContext:\n{context}"
                    
                    try:
                        ai_briefing = await generate_content_with_fallback(
                            prompt=prompt,
                            workload=WorkloadProfile.SYNTHESIS,
                            primary_model=os.getenv("GEMINI_FLASH_MODEL", "gemini-3.5-flash"),
                            config={"temperature": 0.2}
                        )
                        msg += f"\n\n🧠 **Pre-Flight Context:**\n{ai_briefing.text.strip()}"
                    except Exception as e:
                        audit_log_sync("sentinel", "WARNING", f"AI context generation failed: {e}")
                        msg += f"\n\n🧠 **Context found:**\n{context}"

                success = await send_telegram(int(telegram_chat_id), msg)
                
                if success:
                    audit_log_sync("sentinel", "INFO", f"{search_str} - Nudged for {title}")
                    alerted_count += 1
                    print(f"✅ Nudged for: {title}")
                else:
                    audit_log_sync("sentinel", "ERROR", f"Failed to send Telegram nudge for {title}")
            except Exception as event_err:
                audit_log_sync("sentinel", "ERROR", f"Event processing failed for {event.get('summary', 'unknown')}: {event_err}")
                print(f"❌ Event processing error: {event_err}")
                
        await complete_pulse_run(supabase, run_id, status="completed",
            metadata={"alerted": alerted_count})
        return {"success": True, "alerted": alerted_count}

    except Exception as e:
        import traceback
        audit_log_sync("sentinel", "CRITICAL", f"Sentinel Critical Error: {e}")
        traceback.print_exc()
        await complete_pulse_run(supabase, run_id, status="failed", error_message=str(e))
        return {"error": str(e)}
