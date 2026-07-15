import pytest

# test_completion_misclassify is archived.
# The _has_broader_context_signals() heuristic was removed in the P0/P2 overhaul.
# All intent classification now goes through the LLM classifier only — the 
# `contains_hidden_action` field in classify output handles multi-intent detection.
# No keyword pre-filter exists before LLM classify.
#
# If new completion misclassify tests are needed, they should test the LLM 
# classifier's behavior, not prompt-level heuristics.

@pytest.mark.asyncio
async def test_placeholder():
    """Placeholder to keep pytest collection happy. Remove when real tests replace this."""
    pass
