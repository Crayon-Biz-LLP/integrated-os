# api/webhook.py
import os
import json
import asyncio
import httpx
from supabase import create_client, Client
from datetime import datetime, timezone, timedelta
from google import genai

supabase: Client = create_client(
    os.getenv("SUPABASE_URL"), 
    os.getenv("SUPABASE_SERVICE_ROLE_KEY")
)
gemini_client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))

EMBEDDING_MODEL = "text-embedding-004"
CLASSIFICATION_MODEL = "gemini-3.1-flash-lite-preview"
EMBEDDING_DIMENSION = 768


async def call_gemini_with_retry(prompt: str, model: str = None, config: dict = None, contents=None):
    """Call Gemini with retry logic (3 retries, exponential backoff for 503 errors)."""
    if model is None:
        model = CLASSIFICATION_MODEL
    
    max_retries = 3
    base_delay = 1
    
    for attempt in range(max_retries):
        try:
            if contents is not None:
                response = gemini_client.models.generate_content(
                    model=model,
                    contents=contents,
                    config=config or {}
                )
            else:
                response = gemini_client.models.generate_content(
                    model=model,
                    contents=prompt,
                    config=config or {}
                )
            return response
        except Exception as e:
            error_str = str(e).lower()
            if '503' in error_str and attempt < max_retries - 1:
                delay = base_delay * (2 ** attempt)
                print(f"⚠️ Gemini 503 error, retrying in {delay}s (attempt {attempt + 1}/{max_retries})...")
                await asyncio.sleep(delay)
                continue
            else:
                raise


def get_embedding(text: str) -> list:
    try:
        result = gemini_client.models.embed_content(
            model=EMBEDDING_MODEL,
            content=text
        )
        return result.embeddings[0].values
    except Exception as e:
        print(f"Embedding error: {e}")
        return [0] * EMBEDDING_DIMENSION


def classify_intent(text: str, context: list, ist_hour: int = None, core_json: str = "[]") -> dict:
    from datetime import datetime, timezone, timedelta
    ist_offset = timezone(timedelta(hours=5, minutes=30))
    now = datetime.now(ist_offset)
    current_hour = ist_hour if ist_hour is not None else now.hour
    
    if 4 <= current_hour < 12:
        partner_greeting = "Let's clear this early. It's on the list."
    elif 12 <= current_hour < 18:
        partner_greeting = "Got it. I'll track this so you can keep your momentum."
    else:
        partner_greeting = "I've got it. It's off your mind for tonight. Go be with the family."
    
    context_str = ""
    if context:
        context_str = f"\n\nPrevious messages for context:\n" + "\n".join([f"- {c['content']}" for c in context])
    
    prompt = f"""You are Danny's trusted partner. Direct, simple, deeply human. You know the stakes: the ₹30L debt and the Qhord launch with Joel. Your job: Kill the friction so he can get home to Sunju and the boys.

    Message: "{text}"{context_str}
    CURRENT TIME CONTEXT: {partner_greeting}

    IDENTITY & BUSINESS CONTEXT: {core_json}

    Avoid artificial or high-flown words like Sanctuary, Base, Strategic Momentum, Executive Office. Talk like a friend who is also a high-level operator.

    Return ONLY valid JSON (no markdown, no explanation):
    {{
        "intent": "TASK|NOTE|NOISE|CLARIFICATION_NEEDED|DELEGATE",
        "confidence": 0.0-1.0,
        "entity": "Extract matching entity from business_entities context (e.g., QHORD, SOLVSTRAT) or default to INBOX",
        "title": "extracted task title if TASK",
        "time_context": "extracted time/due info if any",
        "clarification_question": "ask Danny what's missing if CLARIFICATION_NEEDED",
        "receipt": "Got it. I've added the task to test this OS and document the process to your list for tomorrow.",
        "reasoning": "brief reasoning for classification"
    }}

    Rules:
    - STRICT TITLE FIDELITY: The title field must be a literal extraction of the task as spoken. NEVER add project names, infer entities, or change Danny's wording (e.g., if he says "this OS," do NOT change it to "Qhord OS").
    - PROJECT ROUTING: Use the 'business_entities' and 'current_season' definitions provided in the context to assign the entity. If it mentions home, family, or faith, route to PERSONAL or CHURCH. Default to INBOX.
    - TASK: Any message that implies an action. Do not require a date or time.
    - NOTE: Ideas, insights, or learnings worth remembering.
    - DELEGATE: Research, competitor audits, or autonomous web research.
    - RECEIPT RULE: Construct the receipt by combining the time-aware greeting ('{partner_greeting}') with a specific confirmation that the task is SECURED on the list or calendar. 
    - CRITICAL: Do not imply that the work is already finished, drafted, or sent (e.g., do not say "I've drafted it" or "It's handled") unless the intent is explicitly DELEGATE. Focus on the fact that the entry is safe so Danny can stop thinking about it.
    - Tone: Trusted Partner—direct, simple, human—but prioritize accuracy over sounding "smart"."""

    async def _classify():
        try:
            response = await call_gemini_with_retry(
                prompt=prompt,
                model=CLASSIFICATION_MODEL,
                config={'response_mime_type': 'application/json'}
            )
            result = json.loads(response.text)
            return result
        except Exception as e:
            print(f"Classification error: {e}")
            return {"intent": "CLARIFICATION_NEEDED", "confidence": 0.0, "clarification_question": "My brain stalled. Could you repeat that?"}
    
    return asyncio.get_event_loop().run_until_complete(_classify())


async def get_recent_context(limit: int = 2) -> list:
    try:
        res = supabase.table('raw_dumps')\
            .select('content')\
            .eq('is_processed', False)\
            .order('created_at', desc=True)\
            .limit(limit)\
            .execute()
        return res.data if res.data else []
    except:
        return []


async def download_telegram_file(file_id: str) -> tuple[bytes, str]:
    """Download file from Telegram and return (bytes, mime_type)."""
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
    
    async with httpx.AsyncClient() as client:
        file_info = await client.get(f"https://api.telegram.org/bot{bot_token}/getFile?file_id={file_id}")
        file_data = file_info.json()
        
        if not file_data.get('ok'):
            raise Exception(f"Telegram API error: {file_data}")
        
        file_path = file_data['result']['file_path']
        mime_type = file_data['result'].get('mime_type', 'application/octet-stream')
        
        download_url = f"https://api.telegram.org/bot{bot_token}/file/{file_path}"
        file_bytes = await client.get(download_url)
        
        return file_bytes.content, mime_type


async def process_multimodal_content(file_bytes: bytes, mime_type: str, chat_id: int, ist_hour: int = None, core_json: str = "[]"):
    """Process audio, image, or document content and extract tasks and insights."""
    from datetime import datetime, timezone, timedelta
    ist_offset = timezone(timedelta(hours=5, minutes=30))
    now = datetime.now(ist_offset)
    current_hour = ist_hour if ist_hour is not None else now.hour
    
    if 4 <= current_hour < 12:
        partner_greeting = "Let's clear this early. It's on the list."
    elif 12 <= current_hour < 18:
        partner_greeting = "Got it. I'll track this so you can keep your momentum."
    else:
        partner_greeting = "I've got it. It's off your mind for tonight. Go be with the family."
    
    prompt = f"""You are Danny's trusted partner and high-level operator. Direct, simple, and deeply human. You know the stakes: the ₹30L debt and the Qhord launch with Joel. Your job: Kill the friction so he can get home to Sunju and the boys.

    CURRENT TIME CONTEXT: {partner_greeting}

    IDENTITY & BUSINESS CONTEXT: {core_json}

    THE STRATEGIC MAP: Categorize the 'entity' based on the business_entities defined in the context above. If it's family/home, use PERSONAL. Default to INBOX.

    ---
    MULTIMODAL INSTRUCTIONS:
    If an IMAGE: Transcribe text, analyze UI/Design patterns, identify strategic diagrams or URLs.
    If AUDIO: Extract explicit actions, deadlines, decisions, and research requests. 
    If DOCUMENT: Summarize intent, extract deliverables, legal obligations, and deadlines.

    RULES:
    - TASK: Any implied action (Send, Call, Fix). Do not require a date. 
    - NOTE: Strategic insights, facts, or observations worth remembering.
    - DELEGATE: Research requests, competitor audits, or dossier building.

    OUTPUT:
    Return ONLY a valid JSON array of objects. For every item, identify the 'entity' (QHORD, SOLVSTRAT, etc.).
    Example: [{{"type": "TASK", "entity": "CRAYON", "content": "Send experience letters to Siva and Suriya by tomorrow"}}]

    Tone: No corporate polish. No "Starship" metaphors. Talk like a high-level partner who knows the time of day and what's at stake.
    """

    try:
        content_parts = [prompt]
        
        if mime_type.startswith('image/'):
            content_parts.append({"mime_type": mime_type, "data": file_bytes})
        elif mime_type.startswith('audio/') or mime_type == 'application/octet-stream':
            content_parts.append({"mime_type": mime_type, "data": file_bytes})
        elif mime_type in ['application/pdf', 'application/vnd.openxmlformats-officedocument.wordprocessingml.document']:
            content_parts.append({"mime_type": mime_type, "data": file_bytes})
        else:
            content_parts.append(file_bytes.decode('utf-8', errors='ignore'))
        
        response = await call_gemini_with_retry(
            contents=content_parts,
            model=CLASSIFICATION_MODEL,
            config={'response_mime_type': 'application/json'}
        )
        
        extracted = json.loads(response.text)
        
        task_count = 0
        note_count = 0
        
        for item in extracted:
            item_type = item.get('type', '').upper()
            content = item.get('content', '')
            
            if not content:
                continue
            
            if item_type == 'TASK':
                supabase.table('raw_dumps').insert([{
                    "content": content,
                    "metadata": json.dumps({
                        "source": "multimodal", 
                        "mime_type": mime_type,
                        "entity": item.get('entity') # 🚀 SAVE THE ENTITY FROM THE PHOTO/AUDIO
                    })
                }]).execute()
                task_count += 1
                print(f"📋 Task extracted: {content[:50]}...")
            
            elif item_type == 'NOTE':
                embedding = get_embedding(content)
                supabase.table('memories').insert({
                    "content": content,
                    "memory_type": "note",
                    "embedding": embedding
                }).execute()
                note_count += 1
                print(f"📝 Note vaulted: {content[:50]}...")
            
            elif item_type == 'DELEGATE':
                supabase.table('agent_queue').insert({
                    "task": content,
                    "status": "pending",
                    "metadata": json.dumps({"source": "multimodal", "mime_type": mime_type})
                }).execute()
                print(f"🕵️ Agent dispatched: {content[:50]}...")
        
        summary_parts = []
        if task_count > 0:
            summary_parts.append(f"{task_count} Task{'s' if task_count != 1 else ''}")
        if note_count > 0:
            summary_parts.append(f"{note_count} Insight{'s' if note_count != 1 else ''}")
        
        if summary_parts:
            summary = " & ".join(summary_parts)
            ack = partner_greeting
            await send_telegram(chat_id, f"✓ {ack} Logged {summary}.")
        else:
            ack = partner_greeting
            await send_telegram(chat_id, f"✓ {ack}")
        
        return {"tasks": task_count, "notes": note_count}
    
    except Exception as e:
        print(f"Multimodal processing error: {e}")
        ack = "Something went wrong. Try sending as text."
        await send_telegram(chat_id, f"⚠️ {ack}")
        return {"tasks": 0, "notes": 0}


# 1. Update your handle_confident_task signature to accept entity
async def handle_confident_task(text: str, title: str, time_context: str, chat_id: int, receipt: str = None, entity: str = None):
    supabase.table('raw_dumps').insert([{
        "content": text,
        "metadata": json.dumps({
            "title": title, 
            "time_context": time_context,
            "entity": entity  # 🚀 THIS SAVES THE BUCKET
        })
    }]).execute()
    
    ack = receipt or "Got it. I've put this on the list so you don't have to think about it."
    await send_telegram(chat_id, f"✓ {ack}\n\n{title}\n{'⏰ ' + time_context if time_context else ''}")


async def handle_confident_note(text: str, chat_id: int, receipt: str = None):
    embedding = get_embedding(text)
    supabase.table('memories').insert({
        "content": text,
        "memory_type": "note",
        "embedding": embedding
    }).execute()
    ack = receipt or "Saved. I'll keep this safe while you keep moving."
    await send_telegram(chat_id, f"✓ {ack}")


async def handle_clarification(text: str, question: str, chat_id: int, receipt: str = None):
    ack = receipt or "I see the weight of this, Danny. We'll solve it together. Just keep going."
    reply = f"✓ {ack}\n\n{question}\n\n_Context: \"{text[:100]}...\"_"
    await send_telegram(chat_id, reply)
    
    supabase.table('raw_dumps').insert([{
        "content": text,
        "metadata": json.dumps({"awaiting_clarification": True})
    }]).execute()


async def interrogate_brain(query: str, chat_id: int):
    """On-Demand Brain Interrogation - Search memories and resources."""
    try:
        await send_telegram(chat_id, "🧠 *Searching your vault...*")
        
        embedding = get_embedding(query)
        
        memories_res = supabase.rpc(
            'match_memories',
            {
                'query_embedding': embedding,
                'match_count': 5,
                'match_threshold': 0.5
            }
        ).execute()
        memories = memories_res.data if memories_res.data else []
        
        try:
            resources_res = supabase.table('resources').select('title, url, category, content').execute()
            resources = resources_res.data or []
        except:
            resources = []
        
        all_context = []
        
        for m in memories:
            source = m.get('memory_type', 'memory').upper()
            content = m.get('content', '')
            link = m.get('url') or ''
            all_context.append(f"[{source}] {content}" + (f" | Link: {link}" if link else ""))
        
        for r in resources[:3]:
            title = r.get('title', 'Untitled')
            url = r.get('url', '')
            category = r.get('category', 'resource')
            content = r.get('content', title)
            all_context.append(f"[{category.upper()}] {content}" + (f" | Link: {url}" if url else ""))
        
        if not all_context:
            await send_telegram(chat_id, "🔍 *No relevant memories found.*\n\n_Try a different query._")
            return
        
        context_str = "\n\n".join(all_context)
        
        prompt = f"""You are Danny's memory assistant. Based on the provided context from his vault, answer his question accurately and concisely. If you don't know the answer, say so. Cite the source (Memory Type or Link) if possible.

Vault Context:
{context_str}

Question: {query}

Provide a clear, concise answer. Format with Markdown. If referencing a specific memory, cite it like [MEMORY] or [RESOURCE]."""
        
        response = await call_gemini_with_retry(prompt=prompt, model=CLASSIFICATION_MODEL)
        
        answer = response.text.strip()
        
        await send_telegram(chat_id, f"🧠 *Brain Interrogation:*\n\n{answer}")
        
    except Exception as e:
        print(f"Interrogation error: {e}")
        await send_telegram(chat_id, "⚠️ *Search failed.*\n\n_Try again._")


async def handle_noise(chat_id: int):
    await send_telegram(chat_id, "👍")


async def send_telegram(chat_id: int, message_text: str, show_keyboard: bool = True):
    telegram_bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
    url = f"https://api.telegram.org/bot{telegram_bot_token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": message_text,
        "parse_mode": "Markdown"
    }
    if show_keyboard:
        payload["reply_markup"] = {
            "keyboard": [
                [{"text": "🔴 Urgent"}, {"text": "📋 Brief"}],
                [{"text": "🚀 Mission"}, {"text": "📚 Library"}],
                [{"text": "🧭 Season Context"}, {"text": "🔓 Vault"}]
            ],
            "resize_keyboard": True,
            "persistent": True
        }
        payload["disable_web_page_preview"] = True
    
    async with httpx.AsyncClient() as client:
        await client.post(url, json=payload)


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
    try:

        from datetime import datetime, timezone, timedelta
        ist_offset = timezone(timedelta(hours=5, minutes=30))
        now = datetime.now(ist_offset)

        if not update or 'message' not in update:
            return {"message": "No message"}

        message = update.get('message', {})
        chat = message.get('chat', {})
        chat_id = chat.get('id')
        text = message.get('text', '')

        core_res = supabase.table('core_config').select('key, content').execute()
        core_json = json.dumps(core_res.data or [])

        if not chat_id:
            return {"success": True}

        owner_id = os.getenv("TELEGRAM_CHAT_ID")
        if not owner_id or str(chat_id) != str(owner_id):
            print(f"⛔ Unauthorized access from Chat ID: {chat_id}")
            return {"message": "Unauthorized"}

        if not text:
            photo = message.get('photo')
            voice = message.get('voice')
            audio = message.get('audio')
            document = message.get('document')
            
            if photo:
                file_id = photo[-1].get('file_id')
                await send_telegram(chat_id, "🖼️ Processing image...")
                file_bytes, mime = await download_telegram_file(file_id)
                await process_multimodal_content(file_bytes, mime, chat_id, ist_hour=now.hour, core_json=core_json)
                return {"success": True}
            
            elif voice or audio:
                file_id = voice.get('file_id') or audio.get('file_id')
                await send_telegram(chat_id, "🎙️ Processing audio...")
                file_bytes, mime = await download_telegram_file(file_id)
                await process_multimodal_content(file_bytes, mime, chat_id, ist_hour=now.hour, core_json=core_json)
                return {"success": True}
            
            elif document:
                file_id = document.get('file_id')
                mime = document.get('mime_type', '')
                
                if mime in ['application/pdf', 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'] or mime.startswith('text/'):
                    await send_telegram(chat_id, "📄 Processing document...")
                    file_bytes, mime = await download_telegram_file(file_id)
                    await process_multimodal_content(file_bytes, mime, chat_id, ist_hour=now.hour, core_json=core_json)
                    return {"success": True}
                else:
                    await send_telegram(chat_id, "⚠️ Unsupported file type. Send as PDF, DOCX, or text.")
                    return {"success": True}
            
            return {"success": True}
        
        context = await get_recent_context(limit=2)
        classification = classify_intent(text, context, ist_hour=now.hour, core_json=core_json)
        
        intent = classification.get('intent', 'TASK')
        confidence = classification.get('confidence', 0.5)
        
        print(f"🎯 Intent: {intent} ({confidence:.0%}) - {text[:50]}...")

        if text.startswith('?'):
            query = text[1:].strip()
            if query:
                await interrogate_brain(query, chat_id)
                return {"success": True}

        if text.startswith('/') or text in ['🔴 Urgent', '📋 Brief', '🧭 Season Context', '🔓 Vault', '📚 Library']:
            return await handle_command(text, chat_id)

        if text.startswith('N:') or text.startswith('Note:'):
            note_content = text[2:].strip() if text.startswith('N:') else text[5:].strip()
            if note_content:
                receipt = "Saved. I'll keep this safe while you keep moving."
                await handle_confident_note(note_content, chat_id, receipt)
            return {"success": True}

        receipt = classification.get('receipt')
        
        # 2. Update the call inside process_webhook (Line 408)
        if intent == 'TASK' and confidence >= 0.6:
          print(f"📋 WORK LOGGED: {text[:80]}...")
          await handle_confident_task(
             text,
             classification.get('title', text),
             classification.get('time_context', ''),
             chat_id,
             receipt,
             entity=classification.get('entity') # 🚀 PASS THE ENTITY
    )
        elif intent == 'NOTE' and confidence >= 0.6:
            await handle_confident_note(text, chat_id, receipt)
        elif intent == 'DELEGATE':
            supabase.table('agent_queue').insert({
                "task": text,
                "status": "pending"
            }).execute()
            ack = receipt or "The intern is on it. I'll ping you when the research is ready."
            await send_telegram(chat_id, f"✓ {ack}")
        elif intent == 'NOISE':
            await handle_noise(chat_id)
            supabase.table('raw_dumps').insert([{"content": text}]).execute()
        else:
            await handle_clarification(
                text,
                classification.get('clarification_question', 'Could you provide more details?'),
                chat_id,
                receipt
            )

        return {"success": True}

    except Exception as e:
        print(f"Webhook Error: {e}")
        return {"error": str(e), "status": 500}


async def handle_command(text: str, chat_id: int):
    reply = ""
    
    if text.startswith('/mission') or text == '🚀 Mission':
        params = text.replace('/mission', '').replace('🚀 Mission', '').strip()
        if not params:
            m_res = supabase.table('missions').select('title').eq('status', 'active').execute()
            if m_res.data:
                m_list = "\n".join([f"• {m['title']}" for m in m_res.data])
                reply = f"🚀 **ACTIVE MISSIONS:**\n\n{m_list}\n\n_To start a new one, type /mission [Goal]_"
            else:
                reply = "🚀 No active missions. Type `/mission [Goal]` to start hunting."
        else:
            try:
                supabase.table('missions').insert({"title": params}).execute()
                reply = f"🚀 **MISSION DECLARED:** {params}\n\nI am now hunting for components and 'Sparks' related to this goal."
            except:
                reply = "❌ Database Error creating mission."

    elif text in ['/library', '📚 Library']:
        lib_res = supabase.table('resources').select('title, url, category').order('created_at', desc=True).limit(10).execute()
        items = lib_res.data or []
        if items:
            formatted = [f"🔖 **[{i.get('title') or 'Untitled'}]({i.get('url')})**" for i in items]
            reply = f"📚 **RESOURCE LIBRARY (Last 10):**\n\n" + "\n\n".join(formatted)
        else:
            reply = "The library is empty. Save some links first!"

    elif text in ['/vault', '🔓 Vault']:
        vault_url = "https://danny-integrated-os.streamlit.app"
        reply = f"🔓 **COMMAND CENTER ONLINE**\n\nYour strategic overview and research library are live.\n\n👉 [Access Secure Vault]({vault_url})"

    elif text.startswith('/season') or text == '🧭 Season Context':
        params = text.replace('/season', '').replace('🧭 Season Context', '').strip()
        if not params:
            season_res = supabase.table('core_config').select('content').eq('key', 'current_season').limit(1).execute()
            if season_res.data:
                reply = f"🧭 **CURRENT NORTH STAR:**\n\n{season_res.data[0]['content']}"
            else:
                reply = "⚠️ No Season Context found. Set one using `/season text...`"
        else:
            if len(params) < 10:
                reply = "❌ **Error:** Definition too short."
            else:
                try:
                    supabase.table('core_config').update({"content": params}).eq('key', 'current_season').execute()
                    reply = "✅ **Season Updated.**\nTarget Locked."
                except:
                    reply = "❌ Database Error"

    elif text in ['/urgent', '🔴 Urgent']:
        now_iso = datetime.now(timezone.utc).isoformat()
        fire_res = supabase.table('tasks').select('*').eq('priority', 'urgent').eq('status', 'todo').or_(f"reminder_at.is.null,reminder_at.lte.{now_iso}").limit(1).execute()
        if fire_res.data:
            fire = fire_res.data[0]
            reply = f"🔴 **ACTION REQUIRED:**\n\n🔥 {fire.get('title')}\n⏱️ Est: {fire.get('estimated_minutes')} mins"
        else:
            reply = "✅ No active fires. You are strategic."

    elif text in ['/brief', '📋 Brief']:
        now_iso = datetime.now(timezone.utc).isoformat()
        tasks_res = supabase.table('tasks').select('title, priority').eq('status', 'todo').or_(f"reminder_at.is.null,reminder_at.lte.{now_iso}").limit(15).execute()
        tasks = tasks_res.data or []
        if tasks:
            sort_order = {'urgent': 1, 'important': 2, 'chores': 3, 'ideas': 4}
            sorted_tasks = sorted(tasks, key=lambda x: sort_order.get(x.get('priority'), 99))[:5]
            icons = {'urgent': '🔴', 'important': '🟡', 'chores': '⚪', 'ideas': '💡'}
            formatted = [f"{icons.get(t.get('priority'), '⚪')} {t.get('title')}" for t in sorted_tasks]
            reply = f"📋 **EXECUTIVE BRIEF:**\n\n" + "\n".join(formatted)
        else:
            reply = "The list is empty. Go enjoy your family."

    await send_telegram(chat_id, reply)
    return {"success": True}
