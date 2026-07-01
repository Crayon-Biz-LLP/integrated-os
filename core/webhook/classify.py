from core.services.db import get_supabase
import hashlib
import re
from datetime import datetime, timezone, timedelta

from core.lib.audit_logger import audit_log_sync
from core.lib.redis_cache import cache_get, cache_set
from core.lib.rate_limiter import SlidingWindowLimiter
from core.llm.fallback import generate_content_with_fallback
from core.llm.config import WorkloadProfile
from core.llm.constants import SAFE_HOLD_CLASSIFICATION, CLASSIFICATION_MODEL

supabase = get_supabase()

# D5: Rate limiter — max 15 classify calls per 60s (flash-lite free tier ceiling)
_classify_limiter = SlidingWindowLimiter(max_calls=15, per_seconds=60, redis_key="rhodey:rate_limit:classify")


async def classify_intent(text: str, context: list, ist_hour: int = None, core_json: str = "[]", conversation_history: str = "") -> dict:
    # ---
    # C3 FALLBACK CONTRACT (see core/FALLBACK_CONTRACTS.md):
    # On LLM failure or rate-limit wait > 3s: returns SAFE_HOLD_CLASSIFICATION
    # = {"intent":"NOTE","confidence":1.0,"entity":"INBOX","title":"Fallback Note",
    #    "receipt":"Message vaulted safely (AI classification temporarily unavailable)."}
    # The message is vaulted as a NOTE — embedded into memories, never enters
    # task/completion pipeline. No Telegram error shown.
    # ---
    # --- M3: Query caching ---
    # Cache key includes text + conversation history (the two most variable inputs)
    # Context and core_json change rarely and don't warrant cache-busting
    cache_hash = hashlib.sha256((text + (conversation_history or "")).encode()).hexdigest()[:16]
    cache_key = f"rhodey:classify:{cache_hash}"
    cached = cache_get(cache_key)
    if cached is not None:
        audit_log_sync("webhook", "INFO", f"Classification cache hit: {text[:30]}...")
        return dict(cached)  # Return a copy to prevent callers from mutating the cached dict

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

    # --- C1: Fetch learned corrections (fail-open) ---
    corrections_str = ''
    try:
        from core.webhook.feedback_loop import get_learned_corrections
        corrections_str = get_learned_corrections()
    except Exception:
        pass  # Fail-open: if corrections module is unavailable, skip silently

    # --- C3: Fetch entity labels from graph (fail-open, Redis-cached) ---
    entities_str = ''
    mentioned_entities_str = ''
    try:
        node_data = cache_get('rhodey:entities:graph_nodes')
        if node_data is None:
            node_res = supabase.table('graph_nodes').select('label, type').in_('type', ['person', 'project', 'organization']).order('updated_at', desc=True).nullslast().limit(30).execute()
            node_data = node_res.data if node_res and node_res.data else []
            cache_set('rhodey:entities:graph_nodes', node_data, ttl=300)
        if node_data:
            people = [n['label'] for n in node_data if n['type'] == 'person'][:8]
            projects = [n['label'] for n in node_data if n['type'] == 'project'][:8]
            orgs = [n['label'] for n in node_data if n['type'] == 'organization'][:8]
            entity_lines = []
            if people:
                entity_lines.append(f"People: {', '.join(people)}")
            if projects:
                entity_lines.append(f"Projects: {', '.join(projects)}")
            if orgs:
                entity_lines.append(f"Organizations: {', '.join(orgs)}")
            entities_str = '\n'.join(entity_lines)

            # Detect which entities the user's message mentions
            text_lower = text.lower()
            mentioned = []
            for n in node_data:
                label = n['label']
                if label.lower() in text_lower and label not in mentioned:
                    mentioned.append(label)
            mentioned = mentioned[:5]
            if mentioned:
                mentioned_entities_str = f"MENTIONED ENTITIES: {', '.join(mentioned)}"
    except Exception:
        pass  # Fail-open: if graph query fails, skip silently

    learned_section = f"\n    {corrections_str}\n    " if corrections_str else ""
    if entities_str:
        entities_section = f"\n    KNOWN ENTITIES:\n    {entities_str}\n    {mentioned_entities_str}\n    "
    elif mentioned_entities_str:
        entities_section = f"\n    {mentioned_entities_str}\n    "
    else:
        entities_section = ""

    from core.prompts.classify import build_classify_intent_prompt
    prompt = build_classify_intent_prompt(
        text=text,
        time_phase=time_phase,
        core_json=core_json,
        entities_section=entities_section,
        learned_section=learned_section,
        context_str=context_str,
        conversation_history=conversation_history
    )

    # D5: Rate limit check before LLM call (fail-open on Redis failure)
    try:
        wait = _classify_limiter._get_wait_secs()
        if wait > 3:
            audit_log_sync("webhook", "WARNING", f"Classification rate limited (wait={wait:.1f}s), returning safe hold")
            cache_set(cache_key, SAFE_HOLD_CLASSIFICATION, ttl=300)
            return SAFE_HOLD_CLASSIFICATION
    except Exception:
        pass  # Fail-open: if limiter is unavailable, proceed with LLM call

    try:
        resp = await generate_content_with_fallback(
            prompt=prompt,
            workload=WorkloadProfile.INTERACTIVE,
            primary_model=CLASSIFICATION_MODEL,
            is_classification=True,
            config={'response_mime_type': 'application/json'}
        )
        result = resp.parse_json()
        # Cache successful classification for 5 minutes
        cache_set(cache_key, result, ttl=300)
        return result
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

UPDATE_TRIGGER_WORDS = {'update', 'reschedule', 'change', 'move', 'push', 'postpone', 'delay', 'bring', 'advance'}


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
    "pu": ("PROJECT_UPDATE", "📈 Project Update — status/decisions"),
    "b": ("DAILY_BRIEF", "📅 Brief — what's on my schedule"),
    "r": ("DELEGATE", "🤖 Research — look something up"),
    "p": ("DECLARE_PRACTICE", "🏃 Practice — track a habit"),
    "c": ("COMPLETION", "✅ Completion — marked a task done"),
    "ru": ("ROLE_UPDATE", "👤 Role Update — update someone's role"),
    "x": ("NOISE", "👍 Nothing — just noise"),
}

# C2: Dynamic per-intent confidence thresholds
# (high, low) tuples — COMPLETION needs higher bar, NOTE lower bar
INTENT_THRESHOLDS = {
    'TASK': (0.8, 0.5),
    'COMPLETION': (0.85, 0.6),
    'NOTE': (0.7, 0.4),
    'QUERY': (0.75, 0.5),
    'PROJECT_UPDATE': (0.8, 0.5),
    'NOISE': (0.6, 0.3),
    'DELEGATE': (0.8, 0.5),
    'DECLARE_PRACTICE': (0.85, 0.5),
    'DAILY_BRIEF': (0.75, 0.5),
    'ROLE_UPDATE': (0.75, 0.5),
    'CLARIFICATION_NEEDED': (0.8, 0.5),
}

INTENT_BY_KEYWORD = {}
for _sc, (_intent, _label) in INTENT_OPTIONS.items():
    INTENT_BY_KEYWORD[_intent.lower()] = _intent
    INTENT_BY_KEYWORD[_sc] = _intent

