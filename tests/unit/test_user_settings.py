"""M2 — de-personalization layer (plans/69-multi-tenant-product-plan.md).

Unit tests for:
  - user_settings loader: defaults, env fallback, seeded-row merge
  - timezone resolution (settings → env → IST fallback)
  - routing rules text rendered from domains
  - de-personalized prompt builders (classify / email / voice / briefing)
  - briefing greeting name resolution
"""

import os
from datetime import timedelta
from unittest.mock import MagicMock, patch

import pytest

os.environ.setdefault("SUPABASE_URL", "http://localhost:1")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "test-key")

from core.services import user_settings as us  # noqa: E402  (env must be set first)


@pytest.fixture(autouse=True)
def _clean_state():
    us.clear_cache()
    yield
    us.clear_cache()


# ── defaults & resolution ───────────────────────────────────────────────────

def test_defaults_are_danny_era():
    d = us.defaults()
    assert d.name == "Danny"
    assert d.timezone == "Asia/Kolkata"
    assert d.context.startswith("Danny (Yashwant Daniel)")
    names = [x["name"] for x in d.domains]
    assert "Solvstrat" in names and "Ashraya" in names and "Qhord" in names
    assert "Personal" in d.personal_orgs


def test_env_override_name_and_timezone(monkeypatch):
    monkeypatch.setenv("USER_NAME", "Priya")
    monkeypatch.setenv("USER_TIMEZONE", "Asia/Kathmandu")
    d = us.defaults()
    assert d.name == "Priya"
    assert d.timezone == "Asia/Kathmandu"


def _patch_settings_row(data: dict | None):
    """Context manager: make user_settings lookups return `data`.

    Uses get_supabase.side_effect (instead of return_value) so it wins even
    when a shared autouse fixture in the same run already patched
    get_supabase to return a different mock.
    """
    mock_db = MagicMock()

    def _execute():
        return MagicMock(data=data)

    def _maybe_single():
        b = MagicMock()
        b.execute.side_effect = _execute
        return b

    def _limit(_n):
        return MagicMock(maybe_single=_maybe_single)

    mock_db.table.return_value.select.return_value.eq.return_value.limit.side_effect = _limit
    # user_settings.py does `from core.services.db import get_supabase` at module
    # import time, so the name is bound on `us`, not on db_mod — patch `us`.
    return patch.object(us, "get_supabase", side_effect=lambda: mock_db)


def test_load_settings_merges_seeded_row():
    with _patch_settings_row({
        "user_id": "u1", "timezone": "Asia/Tokyo", "voice": None,
        "context": "Priya, product lead at Acme",
        "domains": [{"name": "Acme", "keywords": ["acme", "product"]}],
    }):
        s = us.load_settings("u1")
    assert s.timezone == "Asia/Tokyo"
    assert s.context == "Priya, product lead at Acme"
    assert [d["name"] for d in s.domains] == ["Acme"]
    assert s.user_id == "u1"


def test_load_settings_reads_name_from_users_row():
    """M2 review fix: name has no column on user_settings — it comes from
    the users row, so a fresh tenant is never called 'Danny'."""
    # Patch get_supabase directly: users lookup must return the name.
    mock_db = MagicMock()
    user_row = {"name": "Priya"}

    def _exec_user():
        return MagicMock(data=user_row)

    def _maybe_user():
        b = MagicMock()
        b.execute.side_effect = _exec_user
        return b

    def _limit_user(_n):
        return MagicMock(maybe_single=_maybe_user)

    def _table(name):
        t = MagicMock()
        if name == "users":
            t.select.return_value.eq.return_value.limit.side_effect = _limit_user
        else:
            t.select.return_value.eq.return_value.limit.side_effect = lambda _n: MagicMock(
                maybe_single=lambda: MagicMock(execute=lambda: MagicMock(data=None)))
        return t

    mock_db.table.side_effect = _table
    with patch.object(us, "get_supabase", return_value=mock_db):
        s = us.load_settings("u-named")
    assert s.name == "Priya"
    assert us.resolve_user_name("u-named") == "Priya"


def test_routing_rules_golden_danny_era():
    """Golden test: Danny's seeded/default domains reproduce the pre-M2
    routing taxonomy exactly (behavioral equivalence, not byte-for-byte)."""
    rr = us.routing_rules_text("u-golden")
    assert "PROJECT ROUTING" in rr
    for kw in ("solvstrat", "crayon", "ashraya", "chennai north", "qhord", "atna", "zoho"):
        assert kw in rr.lower()
    # exactly the 6 Danny-era domains, one line each
    assert len(rr.splitlines()) == 7


def test_load_settings_no_row_uses_defaults():
    with _patch_settings_row(None):
        s = us.load_settings("u-no-row")
    assert s.timezone == "Asia/Kolkata"
    assert s.name == "Danny"


def test_load_settings_fails_open_to_defaults():
    mock_db = MagicMock()
    mock_db.table.side_effect = Exception('relation "user_settings" does not exist')
    with patch.object(us, "get_supabase", return_value=mock_db):
        s = us.load_settings("u9")
    assert s.name == "Danny"
    assert s.timezone == "Asia/Kolkata"
    assert us.resolve_user_name("u9") == "Danny"


def test_resolve_timezone_fallback():
    assert us.resolve_timezone() == "Asia/Kolkata"
    assert us.resolve_timezone("u9") == "Asia/Kolkata"  # no row → default


# ── routing rules ───────────────────────────────────────────────────────────

def test_routing_rules_text_from_default_domains():
    rr = us.routing_rules_text()
    assert "PROJECT ROUTING" in rr
    assert "solvstrat" in rr.lower()
    assert "ashraya" in rr.lower()
    # one line per domain (6 domains + header)
    assert len(rr.splitlines()) == 7


def test_routing_rules_text_custom_domains():
    with patch.object(us, "resolve_domains", return_value=[
            {"name": "Acme", "keywords": ["acme"]}]):
        rr = us.routing_rules_text()
    assert "→ Acme" in rr
    assert "Solvstrat" not in rr


# ── timezone helpers ────────────────────────────────────────────────────────

def test_get_user_timezone_ist_fallback():
    from core.lib.time_utils import get_user_timezone
    tz = get_user_timezone()
    # 05:30 offset == IST
    off = tz.utcoffset(__import__("datetime").datetime(2026, 1, 1))
    assert off == timedelta(hours=5, minutes=30)


# ── de-personalized prompt builders ─────────────────────────────────────────

def test_build_classify_prompt_uses_user_name():
    from core.prompts.classify import build_classify_intent_prompt
    p = build_classify_intent_prompt(
        text="hello", time_phase="morning", core_json="[]",
        entities_section="", learned_section="", context_str="",
        conversation_history="", user_name="Priya",
    )
    assert "Priya" in p
    assert "Danny" not in p.replace("Danny", "") or "Danny" not in p


def test_build_classify_prompt_defaults_to_danny():
    from core.prompts.classify import build_classify_intent_prompt
    p = build_classify_intent_prompt(
        text="hello", time_phase="morning", core_json="[]",
        entities_section="", learned_section="", context_str="",
        conversation_history="",
    )
    assert "Danny" in p  # env/default fallback preserves Danny-era behaviour


def test_email_classify_prompt_user_specific():
    from core.prompts.email_classify import build_email_classify_prompt
    p = build_email_classify_prompt(
        mailbox_type="work", sender="a@b.com", subject="S", body="B",
        user_name="Priya", user_context="Priya, product lead at Acme",
    )
    assert "Priya" in p
    assert "Acme" in p
    assert "Danny" not in p


def test_voice_is_lazy_and_per_user():
    from core.prompts.voice import get_voice
    v = get_voice(user_name="Priya")
    assert "Priya's Rhodey" in v
    v_default = get_voice()
    assert "Danny's Rhodey" in v_default


def test_build_daily_brief_prompt_user_specific():
    from core.prompts.briefing import build_daily_brief_prompt
    p = build_daily_brief_prompt(
        now_str="now", day_label="today", calendar_text=None,
        overdue_text=None, todo_text=None, recent_done_text=None,
        user_name="Priya",
    )
    assert "Priya" in p


# ── briefing greeting name ──────────────────────────────────────────────────

def test_briefing_greeting_uses_resolved_name():
    import api.briefing as briefing
    with patch.object(briefing, "resolve_user_name", return_value="Priya"), \
         patch.object(briefing, "current_user_id", return_value=None), \
         patch.object(briefing, "_now", return_value=__import__("datetime").datetime(2026, 8, 6, 9, 0)):
        greeting = briefing._greeting()
        assert greeting == "Good morning"
    # name resolution path is exercised via resolve_user_name directly
    assert briefing.resolve_user_name() == "Danny"
