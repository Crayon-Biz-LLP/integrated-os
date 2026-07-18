from core.services.db import get_supabase
from core.llm import get_embedding
from core.lib.audit_logger import audit_log_sync

supabase = get_supabase()


async def is_already_in_email_queue(title: str) -> bool:
    """Check if a task title already exists in pending emails."""
    try:
        keywords = [w for w in title.lower().split() if len(w) > 4]
        if not keywords:
            return False
        for kw in keywords[:3]:
            result = supabase.table('messages')\
                .select('id')\
                .eq('channel', 'email')\
                .ilike('suggested_title', f'%{kw}%')\
                .is_('danny_decision', 'null')\
                .limit(1)\
                .execute()
            if result.data:
                audit_log_sync("pulse", "WARNING", f"⚠️  Duplicate guard: '{title}' matches pending email task (keyword: '{kw}'). Skipping.")
                return True

        # Semantic embedding check (high threshold to avoid false positives)
        embedding_res = await get_embedding(title)
        embedding = embedding_res.vector if embedding_res else None
        similarity_res = supabase.rpc('match_memories', {
            'query_embedding': embedding,
            'match_count': 1,
            'match_threshold': 0.88
        }).execute()
        if similarity_res.data:
            score = similarity_res.data[0].get('similarity')
            if isinstance(score, (int, float)) and score > 0:
                audit_log_sync("pulse", "WARNING", f"⚠️ Semantic duplicate guard: '{title}' is semantically similar to an existing memory. Skipping.")
                return True

        return False
    except Exception as e:
        audit_log_sync("pulse", "WARNING", f"Duplicate guard check failed: {e}")
        return False









def cosine_similarity(a: list, b: list) -> float:
    """Cosine similarity between two vectors."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = sum(x * x for x in a) ** 0.5
    norm_b = sum(x * x for x in b) ** 0.5
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)
