"""M17 — residue-gate derivation filter (hardened 2026-08-17).

The personal-residue gate (scripts/scan_tenant1_residue.py) derives
blocklist tokens from tenant data. The hardened derivation drops any
DERIVED token whose lowercase form is an ordinary English word
(scripts/common_english_words.txt — the bounded authority): a tenant's
area/domain name like "Errands" is not evidence of a leak. The curated
STATIC_BLOCKLIST is immune to the filter (distinctive tokens must always
flag), and the STOPLIST remains the domain-flavor supplement.
"""


import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.auth

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

import os  # noqa: E402

os.environ.setdefault("SUPABASE_URL", "http://localhost:1")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "test-key")

import importlib.util  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "_scan_residue_mod", ROOT / "scripts" / "scan_tenant1_residue.py"
)
_scan = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(_scan)

from core.services import db as _db_mod  # noqa: E402  (env must be set first)


def _mock_db(users=(), aliases=(), settings=(), vault_urls=()):
    """Build a supabase client mock answering the four derivation queries."""
    db = MagicMock()
    db.table.return_value.execute.return_value = MagicMock(data=[])

    def _table(name):
        res = MagicMock()
        if name == "users":
            res.select.return_value.execute.return_value = MagicMock(data=list(users))
        elif name == "person_aliases":
            res.select.return_value.execute.return_value = MagicMock(data=list(aliases))
        elif name == "user_settings":
            res.select.return_value.execute.return_value = MagicMock(data=list(settings))
        elif name == "core_config":
            # tenant_aware_client().table("core_config").select(...).eq(...)
            res.select.return_value.eq.return_value.execute.return_value = MagicMock(
                data=list(vault_urls)
            )
        return res

    db.table.side_effect = _table
    return db


# ── The filter's core contract ───────────────────────────────────────────


def test_derived_common_word_is_filtered():
    """A tenant domain named 'Errands' must NEVER become a blocklist token —
    an ordinary English word is not evidence of a leak (the exact false
    positive this fix kills)."""
    db = _mock_db(
        settings=[{"user_id": "u1", "domains": [{"name": "Errands", "keywords": ["errands"]}],
                   "personal_orgs": []}]
    )
    with patch.object(_db_mod, "get_supabase", return_value=db), \
         patch.object(_db_mod, "tenant_aware_client", return_value=db):
        tokens, live = _scan.derive_blocklist(offline=False)
    assert live is True
    assert "Errands" not in tokens


def test_derived_distinctive_token_survives():
    """A genuinely distinctive org name (proper noun) still becomes a token —
    the filter only drops ordinary English words."""
    db = _mock_db(
        settings=[{"user_id": "u1", "domains": [{"name": "Qhord"}],
                   "personal_orgs": []}]
    )
    with patch.object(_db_mod, "get_supabase", return_value=db), \
         patch.object(_db_mod, "tenant_aware_client", return_value=db):
        tokens, _ = _scan.derive_blocklist(offline=False)
    assert "Qhord" in tokens


def test_all_derived_sources_are_filtered():
    """The filter applies across every derived source — names, aliases,
    domains, personal orgs, vault URLs — not just domains."""
    db = _mock_db(
        users=[{"name": "Danny"}],
        aliases=[{"alias": "Marcus", "canonical_name": "Marcus"}],
        settings=[{"user_id": "u1",
                   "domains": [{"name": "Work"}],
                   "personal_orgs": ["Family", "Solvstrat"]}],
        vault_urls=[{"content": "https://danny-integrated-os.supabase.co"}],
    )
    with patch.object(_db_mod, "get_supabase", return_value=db), \
         patch.object(_db_mod, "tenant_aware_client", return_value=db):
        tokens, _ = _scan.derive_blocklist(offline=False)
    # common words dropped
    for common in ("Work", "Family"):
        assert common not in tokens, f"{common} should be filtered"
    # distinctive tokens survive
    for distinctive in ("Danny", "Marcus", "Solvstrat", "danny-integrated-os"):
        assert distinctive in tokens, f"{distinctive} should survive"


def test_static_supplement_is_immune_to_filter():
    """The curated static blocklist is deliberately distinctive — even if a
    word were added to COMMON_WORDS, the static entries must always flag."""
    assert "Qhord" in _scan.STATIC_BLOCKLIST
    tokens, _ = _scan.derive_blocklist(offline=True)
    for t in _scan.STATIC_BLOCKLIST:
        assert t in tokens, f"static token {t!r} must never be filtered"


def test_common_words_list_is_bounded_and_loaded():
    """COMMON_WORDS loads from the bundled file (bounded authority — English's
    common vocabulary doesn't grow with the tenant count)."""
    assert "errands" in _scan.COMMON_WORDS
    assert "family" in _scan.COMMON_WORDS
    # proper nouns / distinctive tokens must NOT be in the common list
    assert "qhort" not in _scan.COMMON_WORDS  # typo-guard on the assertion
    assert "qhor" not in _scan.COMMON_WORDS
    # sanity: the list is a real, bounded set (not a copy of everything)
    assert 100 <= len(_scan.COMMON_WORDS) <= 5000


def test_offline_mode_uses_static_supplement_only():
    """--offline (CI without secrets) returns the static supplement and
    never touches the DB."""
    tokens, live = _scan.derive_blocklist(offline=True)
    assert live is False
    assert set(_scan.STATIC_BLOCKLIST) <= set(tokens)


def test_clean_tokens_still_applies_stoplist():
    """STOPLIST remains the domain-flavor supplement on top of COMMON_WORDS."""
    out = _scan._clean_tokens({"prayer", "Qhord"})
    assert "prayer" not in out      # STOPLIST supplement
    assert "Qhord" in out           # distinctive survives both filters
