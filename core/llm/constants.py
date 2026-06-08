from enum import Enum

class Outcome(str, Enum):
    SUCCESS = "success"
    RETRY_SUCCESS = "retry_success"
    DEADLINE_EXHAUSTED = "deadline_exhausted"
    DEADLINE_EXHAUSTED_WAITING_FOR_LIMITER = "deadline_exhausted_waiting_for_limiter"
    PROVIDER_TIMEOUT = "provider_timeout"
    PROVIDER_5XX = "provider_5xx"
    BREAKER_OPEN = "breaker_open"
    FALLBACK_SUCCESS = "fallback_success"
    SAFE_HOLD_EMITTED = "safe_hold_emitted"
    EMBEDDING_ZERO_VECTOR_FALLBACK = "embedding_zero_vector_fallback"
    NON_RETRYABLE_ERROR = "non_retryable_error"
    PARSE_ERROR = "parse_error"

SAFE_HOLD_CLASSIFICATION = {
    "intent": "CLARIFICATION_NEEDED",
    "confidence": 0.0,
    "entity": "INBOX",
    "title": "",
    "time_context": "",
    "clarification_question": "Could you provide more details?",
    "receipt": "Copy that. I need one more detail to log this.",
    "possible_intents": [],
    "reasoning": "safe_hold"
}

CLASSIFICATION_MODEL = "gemini-3.1-flash-lite"
SYNTHESIS_MODEL = "gemini-3.5-flash"
EMBEDDING_MODEL = "gemini-embedding-2-preview"
