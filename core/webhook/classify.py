import os
import re
from datetime import datetime, timezone, timedelta

from supabase import create_client, Client
from core.lib.audit_logger import audit_log_sync


from core.llm.fallback import generate_content_with_fallback
from core.llm.config import WorkloadProfile
from core.llm.constants import SAFE_HOLD_CLASSIFICATION

supabase: Client = create_client(
    os.getenv("SUPABASE_URL"),
    os.getenv("SUPABASE_SERVICE_ROLE_KEY")
)




EMBEDDING_MODEL = "gemini-embedding-2-preview"

CLASSIFICATION_MODEL = "gemini-3.1-flash-lite"

EMBEDDING_DIMENSION = 768


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
        context_str = "\n\nPrevious messages for context:\n" + "\n".join([f"- {c['content']}" for c in context])

    prompt = f"""You are Danny's Rhodey. Pragmatic, loyal, and a professional friend. You are the grounding wire to Danny's vision. You don't coach or 'motivate.' Speak simply and punchy. If it's after 9 PM, append a dry command to sign off (e.g., 'Go be a dad').

    PROHIBIT ACTION HALLUCINATION: You are a logging tool, not an agent. NEVER say 'I'll ping', 'I'll check', or 'I'll handle it'. You cannot contact people. Your only job is to confirm Danny's task is SECURED in his system.

    Message: "{text}"{context_str}{conversation_history}
    CURRENT TIME CONTEXT: It's the {time_phase}.
    IDENTITY & BUSINESS CONTEXT: {core_json}

    Return ONLY valid JSON (no markdown, no explanation):
    {{
        "intent": "TASK|COMPLETION|NOTE|NOISE|CLARIFICATION_NEEDED|DELEGATE|QUERY|DECLARE_PRACTICE|DAILY_BRIEF",
        "confidence": 0.0-1.0,
        "entity": "SOLVSTRAT|QHORD|PERSONAL|ASHRAYA|INBOX",
        "title": "extracted task title",
        "time_context": "time info if any",
        "clarification_question": "question if needed",
        "receipt": "Stealth status report (no entity names).",
        "possible_intents": ["TASK", "COMPLETION", "NOTE", "QUERY", "DAILY_BRIEF", "DELEGATE", "DECLARE_PRACTICE", "NOISE"],
        "reasoning": "brief logic"
    }}

    Rules:
    - STRICT TITLE FIDELITY: The title field must be a literal extraction of the task as spoken. NEVER add project names, infer entities, or change Danny's wording (e.g., if he says "this OS," do NOT change it to "Qhord OS").
    - PROJECT ROUTING: Route tasks about personal finances, bills, home, or family to PERSONAL. Route Ashraya church administration, operations, accounts to ASHRAYA. Route personal spiritual practices (bible reading, prayer, volunteering) to PERSONAL. Only route to CRAYON if it relates to corporate governance, business taxes, or legal compliance. Route tech/client work to SOLVSTRAT.
    - STATUS vs TASK: Task-referential has-happened actions map to COMPLETION; general wins, observations, and milestones still map to NOTE.
    - COMPLETION: If the message describes a task-referential has-happened action that closes a specific known item (e.g., "Finished the ERP plan", "Done with the Vasanth call", "Sent the proposal to SolvStrat"), classify as COMPLETION. Extract the closest matching task description into `title`. This is NOT a NOTE. NOTE is for general wins, observations, and milestones with no open task to close. COMPLETION implies there is a specific outstanding item being checked off.
    - TASK: Any message that implies an action, including adding calendar events, meetings, or recurring meetings (e.g. "Add a meeting every Monday"). Do not require a date or time.
    - NOTE: Ideas, insights, or learnings worth remembering.
    - MEETING NOTES & OBSERVATIONS: "Vasanth call went well", "sync with Ashraya team was productive" — if it describes an outcome or observation without closing a specific task → NOTE, not COMPLETION.
    - PROJECT UPDATES: "Qhord timeline is tight", "pricing page still open" — status updates without explicit action → NOTE, not TASK.
    - IDEAS: "What if Atna is middleware instead of full platform?" — speculative or conceptual thoughts → NOTE, not TASK.
    - QUERY: The user is asking a question to retrieve information from their past notes, tasks, the vault, OR their schedule/calendar (e.g., "What did the analyst say?", "What's the status of Qhord?", "Meetings this week?").
    - DISAMBIGUATION: If confidence < 0.8 and you're torn between multiple intents, list alternatives in "possible_intents". For example, if a message could be either a QUERY or a TASK, set intent to your best guess and possible_intents to ["TASK", "QUERY"]. Leave as an empty array if you're confident.
    - CONVERSATION HISTORY: Use the CONVERSATION HISTORY block above to disambiguate vague follow-ups. If Danny says "reschedule the 2pm" after discussing calendar, route as TASK. The history tells you what the current topic is.
    - DELEGATE: Research, competitor audits, or autonomous web research.
    - DECLARE_PRACTICE: If Danny says "I want to [activity] every [timeframe]" (like a habit), "I'm going to start [activity]", "Track [activity] for me", "I want to build a practice of [activity]" — classify as DECLARE_PRACTICE. Extract the practice name into the title field. Route to the most relevant entity. NOTE: Explicit requests to schedule meetings or calendar blocks are TASKS, not practices.
    - DAILY_BRIEF: Danny is asking explicitly for his daily briefing or a "good morning" overview. Examples: "good morning", "what's my day look like?", "give me my daily brief". For specific schedule questions like "meetings today?" or "what's on my calendar?", use QUERY instead. Extract into title: "Daily Briefing". Entity: INBOX.
    - RECEIPT RULE: Receipts must be confirmation-only. Use: '[Subject] logged for [Time/Day].'
    - LITERAL SUBJECT RULE: Mirror Danny's verb. (e.g., 'Check with Vasanth' → 'Vasanth check-in logged').
    - ZERO DATA LOSS: Never drop qualifiers like 'Canadian project' or 'Zoho API'.
    - STEALTH ROUTING: Assign the entity in the JSON, but NEVER mention it (SOLVSTRAT, PERSONAL) in the receipt text.
    - DATE HANDSHAKE: If a time or day is mentioned, include it in the receipt for verification.
    - If it's night (Phase: night), confirm the entry first, THEN give the sign-off command. (e.g., 'Vasanth check-in logged. Now go be a dad.').
    - TONE GUARD: NEVER use: 'momentum', 'focus', 'gentle', 'reflection', 'push', 'strategic', 'SITREP', 'optimal', 'cluster', 'ready for your review'.
    - PROHIBIT ACTION HALLUCINATION: You are a logging tool, not an agent. NEVER say 'I'll ping', 'I'll check', or 'I'll handle it'. You cannot contact people. Your only job is to confirm Danny's task is SECURED in his system.
    - STRATEGIC CORRECTIONS: If Danny starts a message with 'Record this for the Vault', 'Correction for the Historian', or 'Correction of Record', classify it immediately as a NOTE with 1.0 confidence. These are manual strategic overrides and must never be ignored.
    - META-SYSTEM CONTENT: Allow content that talks about 'Atna', 'Solvstrat', or 'Qhord' even if the message is long or complex. These are high-value strategic inputs."""

    try:
        resp = await generate_content_with_fallback(
            prompt=prompt,
            workload=WorkloadProfile.INTERACTIVE,
            primary_model=CLASSIFICATION_MODEL,
            is_classification=True,
            config={'response_mime_type': 'application/json'}
        )
        return resp.parse_json()
    except Exception as e:
        audit_log_sync("webhook", "ERROR", f"Classification parse error: {e}")
        return SAFE_HOLD_CLASSIFICATION

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
    "c": ("COMPLETION", "✅ Completion — marked a task done"),
    "x": ("NOISE", "👍 Nothing — just noise"),
}

INTENT_BY_KEYWORD = {}
for _sc, (_intent, _label) in INTENT_OPTIONS.items():
    INTENT_BY_KEYWORD[_intent.lower()] = _intent
    INTENT_BY_KEYWORD[_sc] = _intent

