from core.llm.compat import call_gemini_with_retry  # noqa: F401
from core.llm.compat import call_gemini_with_retry as call_gemini_classify  # noqa: F401
import os
from google import genai

_gemini_client = None

CLASSIFICATION_MODEL = "gemini-3.1-flash-lite"

def get_gemini_client():
    global _gemini_client
    if _gemini_client is None:
        _gemini_client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
    return _gemini_client
