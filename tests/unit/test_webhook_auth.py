"""Webhook chat-authorization gate tests (plans/75 §7).

Covers the production auth boundary in core/webhook/handler.py:

- Owner chat (env TELEGRAM_CHAT_ID) is always authorized.
- TEST_CHAT_IDS is a DEFAULT-OFF allow-list: unset/empty → the gate is
  byte-identical to the legacy single-chat check (fail-closed). A test chat
  only passes when explicitly listed.
- Listed test chats are accepted; ANY other chat is rejected (negative
  matrix — the leak UAT chat-impersonation used to rely on).
"""

import os
from unittest.mock import patch

import pytest

from core.webhook.handler import _chat_authorized

pytestmark = pytest.mark.webhook


def _with_env(owner: str | None, test_chats: str | None):
    return patch.dict(
        os.environ,
        {k: v for k, v in {
            "TELEGRAM_CHAT_ID": owner,
            "TEST_CHAT_IDS": test_chats,
        }.items() if v is not None},
        clear=False,
    )


# ── Owner chat always authorized ──────────────────────────────────────────

def test_owner_chat_authorized_no_test_chats():
    with _with_env("756478183", None):
        assert _chat_authorized(756478183) is True


def test_owner_chat_authorized_even_with_test_chats_set():
    with _with_env("756478183", "909999999"):
        assert _chat_authorized(756478183) is True


def test_owner_chat_string_vs_int_forms():
    with _with_env("756478183", None):
        assert _chat_authorized("756478183") is True


# ── TEST_CHAT_IDS default-off (fail-closed) ───────────────────────────────

def test_bypass_absent_when_env_unset():
    with _with_env("756478183", None):
        # Unset TEST_CHAT_IDS → a non-owner chat is rejected, exactly like
        # the legacy single-chat gate.
        assert _chat_authorized(909999999) is False


def test_bypass_absent_when_env_empty():
    with _with_env("756478183", ""):
        assert _chat_authorized(909999999) is False


def test_bypass_absent_when_owner_unset():
    # No owner AND no test chats → nothing is authorized (fail-closed).
    with _with_env(None, None):
        assert _chat_authorized(123) is False


# ── Listed test chats accepted ────────────────────────────────────────────

def test_test_chat_authorized_when_listed():
    with _with_env("756478183", "909999999"):
        assert _chat_authorized(909999999) is True


def test_test_chat_authorized_when_listed_with_spaces():
    with _with_env("756478183", "909999998, 909999999"):
        assert _chat_authorized(909999999) is True


def test_second_listed_chat_authorized():
    with _with_env("756478183", "909999998,909999999"):
        assert _chat_authorized(909999998) is True


# ── Negative matrix: everything else rejected ─────────────────────────────

def test_arbitrary_chat_rejected_when_bypass_on():
    # Bypass is ON for one test chat — any OTHER chat is still rejected.
    with _with_env("756478183", "909999999"):
        assert _chat_authorized(123456789) is False
        assert _chat_authorized(909999990) is False


def test_partial_match_rejected():
    # "90999999" is a substring-ish prefix of the allowed chat, but not an
    # exact token — must NOT pass (exact-match semantics).
    with _with_env("756478183", "909999999"):
        assert _chat_authorized(90999999) is False


def test_no_chat_id_rejected():
    with _with_env("756478183", "909999999"):
        assert _chat_authorized(None) is False
        assert _chat_authorized("") is False
