from core.services.db import get_supabase
import uuid

SESSION_TIMEOUT_MINUTES = 60
MAX_HISTORY_TOKENS = 2000

def _approx_tokens(text: str) -> int:
    """Approximate token count based on character length (~4 chars/token)."""
    return max(1, len(text) // 4)

def _touch_thread(thread_id: str):
    try:
        get_supabase().table('conversation_threads').update({'last_active_at': 'now()'}).eq('id', thread_id).execute()
    except Exception as e:
        print(f"Failed to touch thread {thread_id}: {e}")

def resolve_thread(chat_id: int, text: str = None) -> tuple:
    """Returns (thread_id, active_anchor)"""
    supabase = get_supabase()
    
    try:
        # 1. Open workflow bound to chat_id
        workflows = supabase.table('conversation_workflows').select('thread_id').eq('chat_id', chat_id).eq('status', 'active').execute()
        if workflows.data and len(workflows.data) == 1:
            thread_id = workflows.data[0]['thread_id']
            _touch_thread(thread_id)
            # get anchor
            t_res = supabase.table('conversation_threads').select('active_anchor').eq('id', thread_id).execute()
            anchor = t_res.data[0].get('active_anchor') if t_res.data else None
            return thread_id, anchor
            
        # 3. Exact entity thread match
        if text:
            try:
                from core.pulse.entity_resolver import resolve_entities_from_text
                org_id, proj_id, reason = resolve_entities_from_text(text)
                
                if proj_id or org_id:
                    e_type = 'project' if proj_id else 'organization'
                    e_id = str(proj_id) if proj_id else str(org_id)
                    
                    existing = supabase.table('conversation_threads') \
                        .select('id, active_anchor') \
                        .eq('chat_id', chat_id) \
                        .eq('thread_type', 'entity') \
                        .eq('entity_type', e_type) \
                        .eq('entity_id', e_id) \
                        .is_('archived_at', 'null') \
                        .execute()
                        
                    if existing.data:
                        thread_id = existing.data[0]['id']
                        _touch_thread(thread_id)
                        return thread_id, existing.data[0].get('active_anchor')
                    else:
                        new_thread = supabase.table('conversation_threads').insert({
                            'chat_id': chat_id,
                            'thread_type': 'entity',
                            'entity_type': e_type,
                            'entity_id': e_id,
                            'routing_confidence': reason
                        }).execute()
                        return new_thread.data[0]['id'], None
            except Exception as inner_e:
                print(f"Entity resolution failed in thread routing: {inner_e}")

        # 4. Last active non-archived thread (if previous bot turn ended with question)
        last_thread = supabase.table('conversation_threads') \
            .select('id, active_anchor') \
            .eq('chat_id', chat_id) \
            .is_('archived_at', 'null') \
            .order('last_active_at', desc=True) \
            .limit(1) \
            .execute()
            
        if last_thread.data:
            thread_id = last_thread.data[0]['id']
            last_msg = supabase.table('conversations') \
                .select('role, content') \
                .eq('thread_id', thread_id) \
                .order('created_at', desc=True) \
                .limit(1) \
                .execute()
                
            if last_msg.data and last_msg.data[0]['role'] == 'bot':
                content = last_msg.data[0]['content']
                if content.strip().endswith('?') or 'clarification' in content.lower():
                    _touch_thread(thread_id)
                    return thread_id, last_thread.data[0].get('active_anchor')

        # 5. Else general
        general = supabase.table('conversation_threads') \
            .select('id, active_anchor') \
            .eq('chat_id', chat_id) \
            .eq('thread_type', 'general') \
            .is_('archived_at', 'null') \
            .execute()
            
        if general.data:
            thread_id = general.data[0]['id']
            _touch_thread(thread_id)
            return thread_id, general.data[0].get('active_anchor')
        else:
            new_thread = supabase.table('conversation_threads').insert({
                'chat_id': chat_id,
                'thread_type': 'general'
            }).execute()
            return new_thread.data[0]['id'], None

    except Exception as e:
        print(f"Thread routing failed, falling back to new session: {e}")
        return str(uuid.uuid4()), None

def get_or_create_session(chat_id: int, message_text: str = None) -> tuple:
    """
    Legacy wrapper: Returns (session_id, history_pairs, active_anchor)
    Behind the scenes, resolves a thread and uses thread_id as session_id.
    """
    thread_id, active_anchor = resolve_thread(chat_id, message_text)
    return thread_id, get_history(thread_id), active_anchor

def get_history(session_id: str, max_tokens: int = MAX_HISTORY_TOKENS) -> list:
    """
    Get conversation history for a thread (session_id = thread_id), truncated by token budget.
    Builds user+bot pairs, then drops oldest pairs from front until within max_tokens.
    """
    res = get_supabase().table('conversations') \
        .select('role, intent, content, token_count') \
        .eq('thread_id', session_id) \
        .order('created_at') \
        .execute()
        
    rows = res.data or []
    # Fallback to session_id if no rows found (for old conversations before migration)
    if not rows:
        res = get_supabase().table('conversations') \
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
    """Insert an exchange row into conversations. Maps session_id to thread_id."""
    try:
        record = {
            "session_id": session_id,
            "thread_id": session_id, # We use the same UUID for both to maintain legacy compatibility
            "role": role,
            "intent": intent,
            "content": content,
            "chat_id": chat_id,
            "token_count": _approx_tokens(content),
            "metadata": metadata or {}
        }
        get_supabase().table('conversations').insert(record).execute()
        _touch_thread(session_id)
    except Exception as e:
        from core.lib.audit_logger import audit_log_sync
        audit_log_sync("conversation", "ERROR", f"log_exchange error: {e}")

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
