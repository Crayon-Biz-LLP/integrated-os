import os
import re
import asyncio
import json
from datetime import datetime, timezone, timedelta
from google import genai
from supabase import create_client, Client
from core.lib.rate_limiter import flash_lite_limiter
from core.lib.audit_logger import audit_log_sync


supabase: Client = create_client(
    os.getenv("SUPABASE_URL"),
    os.getenv("SUPABASE_SERVICE_ROLE_KEY")
)


gemini_client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))

EMBEDDING_MODEL = "gemini-embedding-2-preview"

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
            # Rate limit: only for flash-lite model
            if "flash-lite" in model:
                await flash_lite_limiter.acquire_async()
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
            retryable_errors = ['503', '504', '500', 'timeout', 'deadline exceeded']
            should_retry = any(err in error_str for err in retryable_errors)
            if should_retry and attempt < max_retries - 1:
                delay = base_delay * (2 ** attempt)
                audit_log_sync("webhook", "WARNING", f"⚠️ Gemini 503 error, retrying in {delay}s (attempt {attempt + 1}/{max_retries})...")
                await asyncio.sleep(delay)
                continue
            else:
                raise

def get_embedding(text: str) -> list:
    try:
        # 🎯 Force the model to return 768 dimensions to match Supabase
        result = gemini_client.models.embed_content(
            model=EMBEDDING_MODEL,
            contents=text,
            config={
                'output_dimensionality': EMBEDDING_DIMENSION
            }
        )
        return result.embeddings[0].values
    except Exception as e:
        audit_log_sync("webhook", "ERROR", f"Embedding error: {e}")
        return [0] * EMBEDDING_DIMENSION

async def classify_intent(text: str, context: list, ist_hour: int = None, core_json: str = "[]", conversation_history: str = "") -> dict:
    ist_offset = timezone(timedelta(hours=5, minutes=30))
    now = datetime.now(ist_offset)
    current_hour = ist_hour if ist_hour is not None else now.hour

    if 4 <= current_hour < 12:
        time_phase = "morning"
    elif 12 <= current_hour < 18:
        time_phase = "afternoon"
    else:
        time_phase = "night"

    context_str = ""
    if context:
        context_str = f"\n\nPrevious messages for context:\n" + "\n".join([f"- {c['content']}" for c in context])

    prompt = f"""You are Danny's Rhodey. Pragmatic, loyal, and a professional friend. You are the grounding wire to Danny's vision. You don't coach or 'motivate.' Speak simply and punchy. If it's after 9 PM, append a dry command to sign off (e.g., 'Go be a dad').

    PROHIBIT ACTION HALLUCINATION: You are a logging tool, not an agent. NEVER say 'I'll ping', 'I'll check', or 'I'll handle it'. You cannot contact people. Your only job is to confirm Danny's task is SECURED in his system.

    Message: "{text}"{context_str}{conversation_history}
    CURRENT TIME CONTEXT: It's the {time_phase}.
    IDENTITY & BUSINESS CONTEXT: {core_json}

    Return ONLY valid JSON (no markdown, no explanation):
    {{
        "intent": "TASK|NOTE|NOISE|CLARIFICATION_NEEDED|DELEGATE|QUERY|DECLARE_PRACTICE|DAILY_BRIEF",
        "confidence": 0.0-1.0,
        "entity": "SOLVSTRAT|QHORD|PERSONAL|ASHRAYA|INBOX",
        "title": "extracted task title",
        "time_context": "time info if any",
        "clarification_question": "question if needed",
        "receipt": "Stealth status report (no entity names).",
        "possible_intents": ["TASK", "NOTE", "QUERY", "DAILY_BRIEF", "DELEGATE", "DECLARE_PRACTICE", "NOISE"],
        "reasoning": "brief logic"
    }}

    Rules:
    - STRICT TITLE FIDELITY: The title field must be a literal extraction of the task as spoken. NEVER add project names, infer entities, or change Danny's wording (e.g., if he says "this OS," do NOT change it to "Qhord OS").
    - PROJECT ROUTING: Route tasks about personal finances, bills, home, or family to PERSONAL. Route Ashraya church administration, operations, accounts to ASHRAYA. Route personal spiritual practices (bible reading, prayer, volunteering) to PERSONAL. Only route to CRAYON if it relates to corporate governance, business taxes, or legal compliance. Route tech/client work to SOLVSTRAT.
    - STATUS vs TASK: If a message describes something that HAS HAPPENED (e.g., 'Lead generated', 'Meeting finished', 'Sent the file'), classify it as a NOTE. A TASK must imply an OUTSTANDING action for Danny to perform (e.g., 'Call the lead', 'Prepare the ERP plan'). If it's a win or a milestone, it's a NOTE for the Historian.
    - TASK: Any message that implies an action. Do not require a date or time.
    - NOTE: Ideas, insights, or learnings worth remembering.
    - QUERY: The user is asking a question to retrieve information from their past notes, tasks, or the vault (e.g., "What did the analyst say?", "When is my meeting?").
    - DISAMBIGUATION: If confidence < 0.8 and you're torn between multiple intents, list alternatives in "possible_intents". For example, if a message could be either a QUERY or a TASK, set intent to your best guess and possible_intents to ["TASK", "QUERY"]. Leave as an empty array if you're confident.
    - CONVERSATION HISTORY: Use the CONVERSATION HISTORY block above to disambiguate vague follow-ups. If Danny says "what about tomorrow?" after having just asked about today, route as DAILY_BRIEF. If he says "reschedule the 2pm" after discussing calendar, route as TASK. The history tells you what the current topic is.
    - DELEGATE: Research, competitor audits, or autonomous web research.
    - DECLARE_PRACTICE: If Danny says "I want to [activity] every [timeframe]", "I'm going to start [activity]", "Track [activity] for me", "I want to build a practice of [activity]", or expresses intent to establish a recurring behavior — classify as DECLARE_PRACTICE. Extract the practice name into the title field. Route to the most relevant entity (PERSONAL for health/personal routines, SOLVSTRAT for work practices, etc.).
    - DAILY_BRIEF: Danny is asking about today's schedule, calendar, or what's on his plate. Examples: "what's today?", "what's on my calendar?", "what do I have today?", "give me my day", "what's happening today?". Extract into title: "Daily Briefing". Entity: INBOX.
    - RECEIPT RULE: Receipts must be confirmation-only. Use: '[Subject] logged for [Time/Day].'
    - LITERAL SUBJECT RULE: Mirror Danny's verb. (e.g., 'Check with Vasanth' → 'Vasanth check-in logged').
    - ZERO DATA LOSS: Never drop qualifiers like 'Canadian project' or 'Zoho API'.
    - STEALTH ROUTING: Assign the entity in the JSON, but NEVER mention it (SOLVSTRAT, PERSONAL) in the receipt text.
    - DATE HANDSHAKE: If a time or day is mentioned, include it in the receipt for verification.
    - If it's night (Phase: night), confirm the entry first, THEN give the sign-off command. (e.g., 'Vasanth check-in logged. Now go be a dad.').
    - TONE GUARD: NEVER use: 'momentum', 'focus', 'gentle', 'reflection', 'push', 'strategic', 'SITREP', 'optimal', 'mission', 'ready for your review'.
    - PROHIBIT ACTION HALLUCINATION: You are a logging tool, not an agent. NEVER say 'I'll ping', 'I'll check', or 'I'll handle it'. You cannot contact people. Your only job is to confirm Danny's task is SECURED in his system.
    - STRATEGIC CORRECTIONS: If Danny starts a message with 'Record this for the Vault', 'Correction for the Historian', or 'Correction of Record', classify it immediately as a NOTE with 1.0 confidence. These are manual strategic overrides and must never be ignored.
    - META-SYSTEM CONTENT: Allow content that talks about 'Atna', 'Solvstrat', or 'Qhord' even if the message is long or complex. These are high-value strategic inputs."""

    try:
        response = await call_gemini_with_retry(
            prompt=prompt,
            model=CLASSIFICATION_MODEL,
            config={'response_mime_type': 'application/json'}
        )
        clean_json = response.text.replace('```json', '').replace('```', '').strip()
        result = json.loads(clean_json)
        return result
    except Exception as e:
        audit_log_sync("webhook", "ERROR", f"Classification parse error: {e}")
        return {"intent": "NOTE", "confidence": 0.8, "receipt": "Manual correction secured in the vault."}

OPPORTUNITY_PATTERNS = [
    r"new possible project",
    r"potential opportunity",
    r"opportunity with",
    r"we will be tasked",
    r"project opportunity",
    r"potential project",
    r"potential client",
    r"might work on",
    r"client called",
    r"there is a new",
    r"possible new",
]

def detect_opportunity_language(text: str) -> bool:
    text_lower = text.lower()
    for pattern in OPPORTUNITY_PATTERNS:
        if re.search(pattern, text_lower):
            return True
    return False

UPDATE_TRIGGER_WORDS = {'update', 'reschedule', 'reschedule', 'change', 'move', 'push', 'postpone', 'delay', 'bring', 'advance'}


def check_task_overlap_for_update(text: str) -> list:
    """Check if message keywords overlap with active tasks (≥2 keyword match).
    Returns list of matched task dicts, empty if below threshold."""
    try:
        keywords = [w.lower() for w in text.split() if len(w) > 4]
        if len(keywords) < 2:
            return []
        active_keywords = keywords[:3]

        tasks_res = supabase.table('tasks')\
            .select('id, title')\
            .eq('is_current', True)\
            .not_.in_('status', ['done', 'cancelled'])\
            .execute()
        if not tasks_res.data:
            return []

        matched = []
        for task in tasks_res.data:
            existing = task.get('title', '').lower()
            count = sum(1 for kw in active_keywords if kw in existing)
            if count >= 2:
                matched.append(task)
        return matched
    except Exception as e:
        audit_log_sync("webhook", "WARNING", f"Task overlap check failed: {e}")
        return []

INTENT_OPTIONS = {
    "t": ("TASK", "📋 Task — something to do"),
    "q": ("QUERY", "❓ Query — answer a question"),
    "n": ("NOTE", "📝 Note — record this"),
    "b": ("DAILY_BRIEF", "📅 Brief — what's on my schedule"),
    "r": ("DELEGATE", "🤖 Research — look something up"),
    "p": ("DECLARE_PRACTICE", "🏃 Practice — track a habit"),
    "x": ("NOISE", "👍 Nothing — just noise"),
}

INTENT_BY_KEYWORD = {}
for _sc, (_intent, _label) in INTENT_OPTIONS.items():
    INTENT_BY_KEYWORD[_intent.lower()] = _intent
    INTENT_BY_KEYWORD[_sc] = _intent

