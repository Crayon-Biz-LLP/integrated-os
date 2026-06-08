import os
import asyncio
from typing import Callable, Dict, Any, List
from supabase import create_client, Client
from google import genai
from core.lib.audit_logger import audit_log_sync
from core.llm.compat import get_embedding_sync as get_embedding

class ToolRegistry:
    def __init__(self):
        self.tools: Dict[str, Callable] = {}
        
    def register(self, func: Callable):
        """Register a python function as a tool."""
        self.tools[func.__name__] = func
        return func
        
    def get_tools_list(self) -> List[Callable]:
        return list(self.tools.values())
        
    async def execute_tool_call(self, function_call: Any) -> Any:
        """Execute a tool call returned by the LLM."""
        name = function_call.name
        if name not in self.tools:
            raise ValueError(f"Unknown tool: {name}")
            
        # google-genai function_call.args is a dict
        args = function_call.args
        if hasattr(args, "model_dump"):
            args = args.model_dump()
            
        func = self.tools[name]
        if asyncio.iscoroutinefunction(func):
            return await func(**args)
        else:
            return func(**args)

# Global tool registry
rhodey_tools = ToolRegistry()

supabase: Client = create_client(
    os.getenv("SUPABASE_URL"),
    os.getenv("SUPABASE_SERVICE_ROLE_KEY")
)


def is_already_in_email_queue(title: str) -> bool:
    """Check if a task title already exists in email_pending_tasks."""
    try:
        keywords = [w for w in title.lower().split() if len(w) > 4]
        if not keywords:
            return False
        for kw in keywords[:3]:
            result = supabase.table('email_pending_tasks')\
                .select('id')\
                .ilike('suggested_title', f'%{kw}%')\
                .is_('danny_decision', 'null')\
                .limit(1)\
                .execute()
            if result.data:
                audit_log_sync("pulse", "WARNING", f"⚠️  Duplicate guard: '{title}' matches pending email task (keyword: '{kw}'). Skipping.")
                return True

        # Semantic embedding check (high threshold to avoid false positives)
        embedding = get_embedding(title)
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

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")

OPENROUTER_BASE_URL = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1/chat/completions")

PULSE_ENABLE_OPENROUTER_FALLBACK = os.getenv("PULSE_ENABLE_OPENROUTER_FALLBACK", "true").lower() == "true"

PULSE_HTTP_REFERER = os.getenv("PULSE_HTTP_REFERER", "http://localhost:8000")

PULSE_APP_NAME = os.getenv("PULSE_APP_NAME", "Pulse")

GEMMA_FALLBACK_MODEL = "gemma-4-31b-it"

GEMMA_SPEED_MODEL = "gemma-4-26b-a4b-it"

OPENROUTER_MODEL = "nvidia/nemotron-3-super-120b-a12b:free"

RETRYABLE_ERRORS = ['503', '504', '500', 'disconnected', 'timeout', 'deadline exceeded', 'unavailable', 'overloaded', 'rate limit']

NON_RETRYABLE_ERRORS = ['401', '403', '400', 'invalid']

gemini_client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))

EMBEDDING_MODEL = "gemini-embedding-2-preview"

EMBEDDING_DIMENSION = 768

BRIEFING_MODEL = "gemini-3.5-flash"
