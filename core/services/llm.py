from core.llm.compat import call_gemini_with_retry  # noqa: F401
from core.llm.compat import call_gemini_with_retry as call_gemini_classify  # noqa: F401
import os
from google import genai

_gemini_client = None

BRIEFING_MODEL = "gemini-3.5-flash"
CLASSIFICATION_MODEL = "gemini-3.1-flash-lite"
EMBEDDING_MODEL = "gemini-embedding-2-preview"
EMBEDDING_DIMENSION = 768
GEMMA_FALLBACK_MODEL = "gemma-4-31b-it"
GEMMA_SPEED_MODEL = "gemma-4-26b-a4b-it"
OPENROUTER_MODEL = "nvidia/nemotron-3-super-120b-a12b:free"

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
OPENROUTER_BASE_URL = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1/chat/completions")
PULSE_ENABLE_OPENROUTER_FALLBACK = os.getenv("PULSE_ENABLE_OPENROUTER_FALLBACK", "true").lower() == "true"
PULSE_HTTP_REFERER = os.getenv("PULSE_HTTP_REFERER", "http://localhost:8000")
PULSE_APP_NAME = os.getenv("PULSE_APP_NAME", "Pulse")

RETRYABLE_ERRORS = ['503', '504', '500', 'disconnected', 'timeout', 'deadline exceeded', 'unavailable', 'overloaded', 'rate limit']
NON_RETRYABLE_ERRORS = ['401', '403', '400', 'invalid']


def get_gemini_client():
    global _gemini_client
    if _gemini_client is None:
        _gemini_client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
    return _gemini_client


