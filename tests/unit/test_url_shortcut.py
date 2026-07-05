import os
import re
import pytest
from unittest.mock import patch, MagicMock, AsyncMock

from core.prompts.classify import build_classify_intent_prompt


# ── Global supabase mock (replaces the singleton before any module imports) ──

os.environ.setdefault("SUPABASE_URL", "http://localhost:1")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "test-key")

_MOCK_DB = MagicMock()
_MOCK_TABLE = MagicMock()
_MOCK_TABLE.select.return_value.execute.return_value = MagicMock(data=[])
_MOCK_TABLE.select.return_value.limit.return_value.execute.return_value = MagicMock(data=[])
_MOCK_TABLE.select.return_value.eq.return_value.execute.return_value = MagicMock(data=[])
_MOCK_TABLE.select.return_value.eq.return_value.limit.return_value.maybe_single.return_value.execute.return_value = MagicMock(data=None)
_MOCK_TABLE.select.return_value.eq.return_value.limit.return_value.execute.return_value = MagicMock(data=[])
_MOCK_TABLE.select.return_value.eq.return_value.order.return_value.execute.return_value = MagicMock(data=[])
_MOCK_TABLE.select.return_value.in_.return_value.execute.return_value = MagicMock(data=[])
_MOCK_TABLE.select.return_value.not_.return_value.execute.return_value = MagicMock(data=[])
_MOCK_TABLE.select.return_value.eq.return_value.not_.return_value.execute.return_value = MagicMock(data=[])
_MOCK_TABLE.select.return_value.order.return_value.execute.return_value = MagicMock(data=[])
_MOCK_TABLE.insert.return_value.execute.return_value = MagicMock(data=[{"id": 1}])
_MOCK_TABLE.update.return_value.eq.return_value.execute.return_value = MagicMock(data=[])
_MOCK_TABLE.upsert.return_value.execute.return_value = MagicMock(data=[])
_MOCK_DB.table.return_value = _MOCK_TABLE

import importlib  # noqa: E402
import core.services.db as _db_mod  # noqa: E402
_db_mod._supabase = _MOCK_DB

# Reload utils.py so supabase = get_supabase() uses the mock
import core.webhook.utils as _utils_mod  # noqa: E402
_utils_mod.supabase = _MOCK_DB
# Full reload to catch any module-level get_supabase() calls
importlib.reload(_utils_mod)

# Reload handler.py so its from core.webhook.utils import supabase picks up the mock
import core.webhook.handler as _handler_mod  # noqa: E402
_handler_mod.supabase = _MOCK_DB
importlib.reload(_handler_mod)

# Re-import from the reloaded handler
from core.webhook.handler import process_webhook  # noqa: E402


@pytest.fixture(autouse=True)
def _env_and_patches():
    with patch.dict(os.environ, {"TELEGRAM_CHAT_ID": "12345"}, clear=False), \
         patch("core.webhook.handler.audit_log_sync"), \
         patch("core.webhook.handler.set_decision_chain_id"):
        yield


# ── U1: Bare URL regex pattern ─────────────────────────────────────────


BARE_URL_PATTERN = re.compile(r'^https?://\S+$')


@pytest.mark.parametrize("text,expected", [
    ("https://github.com/Panniantong/Agent-Reach", True),
    ("https://x.com/i/status/2072332626396864866", True),
    ("http://example.com", True),
    ("https://github.com/PhonePe/nika", True),
    ("", False),
    ("check this out https://github.com/foo/bar", False),
    ("Remind me to get the invoice from the hotel", False),
    ("https://github.com/foo/bar and more text", False),
    ("text before https://github.com/foo/bar", False),
])
def test_bare_url_pattern(text, expected):
    assert bool(BARE_URL_PATTERN.match(text.strip())) == expected


# ── U2: Short-circuit routes to handle_confident_note, skips classify_intent ──


@pytest.mark.asyncio
async def test_bare_url_shortcircuit_calls_handle_confident_note():
    with patch("core.webhook.handler.handle_confident_note", new_callable=AsyncMock) as mock_note, \
         patch("core.webhook.handler.classify_intent", new_callable=AsyncMock) as mock_classify:

        mock_note.return_value = None

        fake_update = {
            "message": {
                "text": "https://github.com/Panniantong/Agent-Reach",
                "chat": {"id": 12345},
            },
        }

        with patch("core.webhook.handler.format_history_for_prompt"):
            res = await process_webhook(fake_update)

        assert res.get("success") is True
        mock_note.assert_awaited_once()
        mock_classify.assert_not_awaited()


@pytest.mark.asyncio
async def test_non_url_message_does_not_shortcircuit():
    with patch("core.webhook.handler.classify_intent", new_callable=AsyncMock) as mock_classify, \
         patch("core.webhook.handler.route_by_intent", new_callable=AsyncMock) as mock_route, \
         patch("core.webhook.handler.format_history_for_prompt") as mock_fmt:

        mock_classify.return_value = {
            "intent": "TASK", "confidence": 0.95,
            "title": "Get hotel invoice", "receipt": "Invoice task logged.",
            "entity": "ASHRAYA", "time_context": None,
        }
        mock_route.return_value = None
        mock_fmt.return_value = ""

        fake_update = {
            "message": {
                "text": "Remind me to get the invoice from the hotel in Varanasi",
                "chat": {"id": 12345},
            },
        }

        await process_webhook(fake_update)

        mock_classify.assert_awaited_once()
        mock_route.assert_awaited_once()


# ── U3: core_json filtering strips briefing keys ─────────────────────


@pytest.mark.parametrize("input_keys,expected_keys", [
    (
        [{"key": "identity", "content": "who I am"}, {"key": "latest_briefing", "content": "briefing text"}],
        ["identity"],
    ),
    (
        [{"key": "business_entities", "content": "entities"}, {"key": "briefing_history", "content": "[]"}],
        ["business_entities"],
    ),
    (
        [{"key": "last_pulse_summary", "content": "summary"}],
        [],
    ),
    (
        [{"key": "identity", "content": "a"}, {"key": "frameworks", "content": "b"}],
        ["identity", "frameworks"],
    ),
])
def test_core_json_filters_noise_keys(input_keys, expected_keys):
    _NOISE_KEYS = {'latest_briefing', 'briefing_history', 'last_pulse_summary'}
    filtered = [r for r in input_keys if r.get('key') not in _NOISE_KEYS]
    result_keys = [r['key'] for r in filtered]
    assert result_keys == expected_keys


# ── U4: Classify prompt contains URL guard rule ─────────────────────


def test_classify_prompt_contains_url_guard():
    prompt = build_classify_intent_prompt(
        text="https://github.com/test/repo",
        time_phase="night",
        core_json="[]",
        entities_section="",
        learned_section="",
        context_str="",
        conversation_history="",
    )
    assert "URL-ONLY MESSAGES" in prompt
    assert "classify as NOTE" in prompt
    assert "Repository link logged for the vault" in prompt


# ── U5: Bare URL after task-thread context still routes to NOTE ─────────


@pytest.mark.asyncio
async def test_bare_url_shortcircuit_independent_of_history():
    conversation_history = (
        "CONVERSATION HISTORY:\n"
        "User: Remind me to get the invoice from the hotel in Varanasi related to the Ashraya team trip.\n"
        "Rhodey: Varanasi hotel invoice task logged. Now go be a dad."
    )

    with patch("core.webhook.handler.handle_confident_note", new_callable=AsyncMock) as mock_note, \
         patch("core.webhook.handler.classify_intent", new_callable=AsyncMock) as mock_classify, \
         patch("core.webhook.handler.format_history_for_prompt", return_value=conversation_history):

        mock_note.return_value = None

        fake_update = {
            "message": {
                "text": "https://github.com/resemble-ai/chatterbox",
                "chat": {"id": 12345},
            },
        }

        res = await process_webhook(fake_update)
        assert res.get("success") is True

    mock_note.assert_awaited_once_with(
        "https://github.com/resemble-ai/chatterbox", 12345,
        "Repository link logged for the project vault.", source="telegram"
    )
    mock_classify.assert_not_awaited()


# ── U6: Non-URL message with invoice context reaches classify normally ──


@pytest.mark.asyncio
async def test_invoice_task_still_goes_through_classifier():
    with patch("core.webhook.handler.classify_intent", new_callable=AsyncMock) as mock_classify, \
         patch("core.webhook.handler.route_by_intent", new_callable=AsyncMock) as mock_route, \
         patch("core.webhook.handler.format_history_for_prompt", return_value=""):

        mock_classify.return_value = {
            "intent": "TASK", "confidence": 0.95,
            "title": "Get hotel invoice from Varanasi trip",
            "receipt": "Varanasi hotel invoice task logged.",
            "entity": "ASHRAYA", "time_context": None,
        }
        mock_route.return_value = None

        fake_update = {
            "message": {
                "text": "Remind me to get the invoice from the hotel in Varanasi",
                "chat": {"id": 12345},
            },
        }

        await process_webhook(fake_update)

        mock_classify.assert_awaited_once()
        mock_route.assert_awaited_once()


# ── U7: Bare non-GitHub URL after task thread still routes to NOTE ──────


@pytest.mark.asyncio
async def test_bare_youtube_url_after_task_thread_routes_to_note():
    """Regression: a bare YouTube (or any non-GitHub) URL must also
    short-circuit — the fix is URL-class-wide, not GitHub-specific."""
    conversation_history = (
        "CONVERSATION HISTORY:\n"
        "User: Remind me to get the invoice from the hotel in Varanasi.\n"
        "Rhodey: Varanasi hotel invoice task logged."
    )

    with patch("core.webhook.handler.handle_confident_note", new_callable=AsyncMock) as mock_note, \
         patch("core.webhook.handler.classify_intent", new_callable=AsyncMock) as mock_classify, \
         patch("core.webhook.handler.format_history_for_prompt", return_value=conversation_history):

        mock_note.return_value = None

        fake_update = {
            "message": {
                "text": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
                "chat": {"id": 12345},
            },
        }

        res = await process_webhook(fake_update)
        assert res.get("success") is True

    mock_note.assert_awaited_once_with(
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ", 12345,
        "Repository link logged for the project vault.", source="telegram"
    )
    mock_classify.assert_not_awaited()


# ── U8: URL + extra instruction still goes through classifier ───────────


@pytest.mark.asyncio
async def test_url_with_extra_instruction_does_not_shortcircuit():
    """Regression: a URL followed by extra instruction text (e.g. "check
    this repo and tell me what it does") must NOT short-circuit — it needs
    the LLM to interpret intent."""
    with patch("core.webhook.handler.classify_intent", new_callable=AsyncMock) as mock_classify, \
         patch("core.webhook.handler.route_by_intent", new_callable=AsyncMock) as mock_route, \
         patch("core.webhook.handler.format_history_for_prompt", return_value=""):

        mock_classify.return_value = {
            "intent": "QUERY", "confidence": 0.92,
            "title": "Check out this GitHub repo",
            "receipt": "Researching the repo.",
            "entity": None, "time_context": None,
        }
        mock_route.return_value = None

        fake_update = {
            "message": {
                "text": "https://github.com/docusealco/docuseal — check this out, what do you think?",
                "chat": {"id": 12345},
            },
        }

        await process_webhook(fake_update)

        mock_classify.assert_awaited_once()
        mock_route.assert_awaited_once()
