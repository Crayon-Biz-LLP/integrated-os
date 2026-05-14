import os
import uuid
from datetime import datetime, timezone, timedelta

SESSION_TIMEOUT_MINUTES = 5
MAX_HISTORY_TOKENS = 2000

# Create supabase client with fallback (matching webhook.py import pattern)
_supabase = None
def _get_supabase():
    global _supabase
    if _supabase is None:
        try:
            from supabase import create_client, Client
            _supabase = create_client(
                os.getenv("SUPABASE_URL"),
                os.getenv("SUPABASE_SERVICE_ROLE_KEY")
            )
        except Exception as e:
            print(f"conversation: supabase client init failed: {e}")
            raise
    return _supabase


def _approx_tokens(text: str) -> int:
    """Approximate token count based on character length (~4 chars/token)."""
    return max(1, len(text) // 4)


def get_or_create_session(chat_id: int) -> tuple:
    """
    Returns (session_id, history_pairs) for this chat_id.

    History_pairs is a list of dicts: [{'user': {...}, 'bot': {...}}, ...]
    truncated to MAX_HISTORY_TOKENS via sliding window from tail.

    Generates new UUID session_id if:
    - No prior session exists (first ever message)
    - Last exchange was > SESSION_TIMEOUT_MINUTES ago
    """
    res = _get_supabase().table('conversations') \
        .select('session_id, created_at') \
        .eq('chat_id', chat_id) \
        .order('created_at', desc=True) \
        .limit(1) \
        .execute()

    if res.data:
        last = res.data[0]
        last_time = last.get('created_at')
        if last_time:
            if isinstance(last_time, str):
                last_time = datetime.fromisoformat(str(last_time).replace('Z', '+00:00'))
            now = datetime.now(timezone.utc)
            elapsed_min = (now - last_time).total_seconds() / 60
            if elapsed_min < SESSION_TIMEOUT_MINUTES:
                session_id = last['session_id']
                return session_id, get_history(session_id)

    session_id = str(uuid.uuid4())
    return session_id, []


def get_history(session_id: str, max_tokens: int = MAX_HISTORY_TOKENS) -> list:
    """
    Get conversation history for a session, truncated by token budget.
    Builds user+bot pairs, then drops oldest pairs from front until
    within max_tokens.
    """
    res = _get_supabase().table('conversations') \
        .select('role, intent, content, token_count') \
        .eq('session_id', session_id) \
        .order('created_at') \
        .execute()

    rows = res.data or []
    if not rows:
        return []

    pairs = []
    i = 0
    while i < len(rows):
        user_msg = rows[i] if rows[i]['role'] == 'user' else None
        bot_msg = None
        if i + 1 < len(rows) and rows[i + 1]['role'] == 'bot':
            bot_msg = rows[i + 1]
            i += 2
        else:
            i += 1
        pairs.append({'user': user_msg, 'bot': bot_msg})

    if not pairs:
        return []

    total = sum(
        (p.get('user') or {}).get('token_count', 0) +
        (p.get('bot') or {}).get('token_count', 0)
        for p in pairs
    )

    while total > max_tokens and len(pairs) > 1:
        removed = pairs.pop(0)
        total -= (
            (removed.get('user') or {}).get('token_count', 0) +
            (removed.get('bot') or {}).get('token_count', 0)
        )

    return pairs


def log_exchange(session_id: str, role: str, intent: str, content: str, chat_id: int, metadata: dict = None):
    """Insert an exchange row into conversations. Non-blocking on error."""
    try:
        record = {
            "session_id": session_id,
            "role": role,
            "intent": intent,
            "content": content,
            "chat_id": chat_id,
            "token_count": _approx_tokens(content),
            "metadata": metadata or {}
        }
        _get_supabase().table('conversations').insert(record).execute()
    except Exception as e:
        print(f"conversation.log_exchange error: {e}")


def format_history_for_prompt(pairs: list) -> str:
    """Format conversation history as a CONVERSATION HISTORY block for LLM prompts."""
    if not pairs:
        return ""
    lines = ["CONVERSATION HISTORY:"]
    for pair in pairs:
        user = pair.get('user')
        bot = pair.get('bot')
        if user:
            lines.append(f'User: {user.get("content", "")}')
        if bot:
            lines.append(f'Rhodey: {bot.get("content", "")}')
    return "\n".join(lines)
