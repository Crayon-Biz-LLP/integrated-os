# api/webhook.py
import os
import httpx
from supabase import create_client, Client
from datetime import datetime, timezone


# Initialize Supabase Client
supabase: Client = create_client(
    os.getenv("SUPABASE_URL"), 
    os.getenv("SUPABASE_SERVICE_ROLE_KEY")
)


# --- 🎛️ THE CONTROL PANEL ---
KEYBOARD = {
    "keyboard": [
        [{"text": "🔴 Urgent"}, {"text": "📋 Brief"}],
        [{"text": "🚀 Mission"}, {"text": "📚 Library"}],
        [{"text": "🧭 Season Context"}, {"text": "🔓 Vault"}]
    ],
    "resize_keyboard": True,
    "persistent": True
}


async def process_webhook(update: dict):
    reply = ""
    try:
        if not update or 'message' not in update:
            return {"message": "No message"}

        message = update.get('message', {})
        chat = message.get('chat', {})
        chat_id = chat.get('id')
        text = message.get('text', '')

        if not chat_id or not text:
            return {"success": True}  # Acknowledge non-text messages to clear queue

        # --- 🔒 SECURITY GATEKEEPER ---
        owner_id = os.getenv("TELEGRAM_CHAT_ID")
        if not owner_id or str(chat_id) != str(owner_id):
            print(f"⛔ Unauthorized access attempt from Chat ID: {chat_id}")
            return {"message": "Unauthorized"}
        # -----------------------------

        # Helper to send message with the Keyboard attached
        async def send_telegram(message_text: str):
            telegram_bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
            url = f"https://api.telegram.org/bot{telegram_bot_token}/sendMessage"
            payload = {
                "chat_id": chat_id,
                "text": message_text,
                "parse_mode": "Markdown",
                "reply_markup": KEYBOARD,
                "disable_web_page_preview": True  # Keeps the list clean/compact
            }
            async with httpx.AsyncClient() as client:
                await client.post(url, json=payload)

        # 1. COMMAND MODE (Handles /commands AND Button Text)
        command_triggers = ['🔴 Urgent', '📋 Brief', '🧭 Season Context', '🔓 Vault', '📚 Library']
        if text.startswith('/') or text in command_triggers:
            reply = "Thinking..."

            # --- COMMAND: MISSION (View or Create) ---
            if text.startswith('/mission') or text == '🚀 Mission':
                params = text.replace('/mission', '').replace('🚀 Mission', '').strip()
                
                if not params:
                    # List Active Missions
                    m_res = supabase.table('missions').select('title').eq('status', 'active').execute()
                    if m_res.data:
                        m_list = "\n".join([f"• {m['title']}" for m in m_res.data])
                        reply = f"🚀 **ACTIVE MISSIONS:**\n\n{m_list}\n\n_To start a new one, type /mission [Goal]_"
                    else:
                        reply = "🚀 No active missions. Type `/mission [Goal]` to start hunting."
                else:
                    # Create New Mission
                    try:
                        supabase.table('missions').insert({"title": params}).execute()
                        reply = f"🚀 **MISSION DECLARED:** {params}\n\nI am now hunting for components and 'Sparks' related to this goal."
                    except Exception:
                        reply = "❌ Database Error creating mission."

            # --- COMMAND: LIBRARY (Retrieve Saved Links) ---
            elif text in ['/library', '📚 Library']:
                lib_res = supabase.table('resources')\
                    .select('title, url, category')\
                    .order('created_at', desc=True)\
                    .limit(10)\
                    .execute()
                
                items = lib_res.data or []
                if items:
                    formatted_items = []
                    for i in items:
                        display_name = i.get('title') or "Untitled Resource"
                        url = i.get('url')
                        # Using Markdown link syntax [Title](URL)
                        formatted_items.append(f"🔖 **[{display_name}]({url})**")
                    
                    lib_str = "\n\n".join(formatted_items)
                    reply = f"📚 **RESOURCE LIBRARY (Last 10):**\n\n{lib_str}"
                else:
                    reply = "The library is empty. Save some links first!"

            # --- COMMAND: VAULT (Retrieve Ideas) ---
            elif text in ['/vault', '🔓 Vault']:
                # Ensure this matches your actual Streamlit URL
                vault_url = "https://danny-integrated-os.streamlit.app" 
                
                reply = (
                    "🔓 **COMMAND CENTER ONLINE**\n\n"
                    "Your strategic overview, mission progress, and full research library are live on the big screen.\n\n"
                    f"👉 [Access Secure Vault]({vault_url})"
                )

            # --- COMMAND: SEASON (View or Update) ---
            elif text.startswith('/season') or text == '🧭 Season Context':
                params = text.replace('/season', '').replace('🧭 Season Context', '').strip()

                # Scenario A: View Current Season
                if not params:
                    season_res = supabase.table('core_config')\
                        .select('content')\
                        .eq('key', 'current_season')\
                        .limit(1)\
                        .execute()

                    season_data = season_res.data
                    if season_data:
                        reply = f"🧭 **CURRENT NORTH STAR:**\n\n{season_data[0]['content']}"
                    else:
                        reply = "⚠️ No Season Context found. Set one using `/season text...`"

                # Scenario B: Update Season
                else:
                    if len(params) < 10:
                        reply = "❌ **Error:** Definition too short."
                    else:
                        # 🔴 FIX #2: supabase-py UPDATE returns empty data [] by default (204 No Content).
                        # Checking update_res.data always evaluates as False even on success.
                        # Correct pattern: trust the exception — if no exception is raised, it succeeded.
                        try:
                            supabase.table('core_config')\
                                .update({"content": params})\
                                .eq('key', 'current_season')\
                                .execute()
                            reply = "✅ **Season Updated.**\nTarget Locked."
                        except Exception:
                            reply = "❌ Database Error"

            # --- COMMAND: URGENT (Fire Check) ---
            elif text in ['/urgent', '🔴 Urgent']:
                now_iso = datetime.now(timezone.utc).isoformat()

                fire_res = supabase.table('tasks')\
                    .select('*')\
                    .eq('priority', 'urgent')\
                    .eq('status', 'todo')\
                    .or_(f"reminder_at.is.null,reminder_at.lte.{now_iso}")\
                    .limit(1)\
                    .execute()

                fire_data = fire_res.data
                if fire_data:
                    fire = fire_data[0]
                    reply = f"🔴 **ACTION REQUIRED:**\n\n🔥 {fire.get('title')}\n⏱️ Est: {fire.get('estimated_minutes')} mins"
                else:
                    reply = "✅ No active fires. You are strategic."

            # --- COMMAND: BRIEF (Strategic Plan) ---
            elif text in ['/brief', '📋 Brief']:
                now_iso = datetime.now(timezone.utc).isoformat()
                tasks_res = supabase.table('tasks')\
                    .select('title, priority')\
                    .eq('status', 'todo')\
                    .or_(f"reminder_at.is.null,reminder_at.lte.{now_iso}")\
                    .limit(15)\
                    .execute()

                tasks = tasks_res.data or []

                if tasks:
                    sort_order = {'urgent': 1, 'important': 2, 'chores': 3, 'ideas': 4}
                    sorted_tasks = sorted(tasks, key=lambda x: sort_order.get(x.get('priority'), 99))[:5]

                    formatted_tasks = []
                    for t in sorted_tasks:
                        icon = '🔴' if t.get('priority') == 'urgent' else '🟡' if t.get('priority') == 'important' else '⚪'
                        formatted_tasks.append(f"{icon} {t.get('title')}")

                    tasks_str = "\n".join(formatted_tasks)
                    reply = f"📋 **EXECUTIVE BRIEF:**\n\n{tasks_str}"
                else:
                    reply = "The list is empty. Go enjoy your family."

            await send_telegram(reply)
            return {"success": True}

        # 2. CAPTURE MODE (Default)
        if text:
            supabase.table('raw_dumps').insert([{"content": text}]).execute()

            # Receipt Tick
            await send_telegram('✅')

        return {"success": True}

    except Exception as e:
        print(f"Webhook Error: {e}")
        return {"error": str(e), "status": 500}