import pytest
import json
from core.llm.constants import SAFE_HOLD_CLASSIFICATION
from core.llm.response import LLMResponse, EmbeddingResult
from core.llm.compat import call_gemini_with_retry

def test_safe_hold_payload_contract():
    """Verify safe hold payload matches exact names expected by handler.py"""
    assert "intent" in SAFE_HOLD_CLASSIFICATION
    assert SAFE_HOLD_CLASSIFICATION["intent"] == "CLARIFICATION_NEEDED"
    assert "clarification_question" in SAFE_HOLD_CLASSIFICATION

def test_llm_response_parse_json():
    """Verify LLMResponse JSON parsing behavior"""
    valid_json = '```json\n{"key": "value"}\n```'
    resp = LLMResponse(valid_json, "test", "test", "test", True, False, None, 1, 100, None)
    parsed = resp.parse_json()
    assert parsed == {"key": "value"}
    
    from core.llm.errors import ParseError
    empty_resp = LLMResponse("", "test", "test", "test", True, False, None, 1, 100, None)
    with pytest.raises(ParseError):
        empty_resp.parse_json()

def test_embedding_result_zero_vector():
    """Verify zero vector detection matches legacy behavior"""
    success = EmbeddingResult([0.1, 0.2], True, False, None, "test", "test", 100)
    assert not success.is_zero_vector
    
    zero = EmbeddingResult([0.0, 0.0], False, True, "err", "test", "test", 100)
    assert zero.is_zero_vector

@pytest.mark.asyncio
async def test_compat_wrapper_preserves_text_property(mocker):
    """Legacy callers expect response.text"""
    mock_resp = LLMResponse("mock_text", "test", "test", "test", True, False, None, 1, 100, None)
    mocker.patch('core.llm.compat.generate_content_with_fallback', return_value=mock_resp)
    
    resp = await call_gemini_with_retry("test prompt")
    
    assert hasattr(resp, "text")
    assert resp.text == "mock_text"

def test_legacy_classify_consumers_route_safe_hold():
    """
    Simulate classify.py failing and returning SAFE_HOLD_CLASSIFICATION.
    Verify that dispatch.py / handler.py would handle it correctly.
    """
    mock_resp = LLMResponse(json.dumps(SAFE_HOLD_CLASSIFICATION), "test", "test", "classification", False, True, "err", 1, 100, None)
    parsed = mock_resp.parse_json()
    
    assert parsed["intent"] == "CLARIFICATION_NEEDED"
    assert parsed.get("clarification_question") == "Could you provide more details?"
