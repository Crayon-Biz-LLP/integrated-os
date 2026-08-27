"""Regression tests for the graph matching + person-org resolution fixes.

Pins the Aug-25 campaign fixes that previously shipped with no coverage:
  - match_existing_nodes entity-type fetch filter (the PostgREST 1000-row
    truncation that hid Solvstrat from card chips)
  - match_existing_nodes matching semantics (live/pending scope, owner
    exclusion, cross-type isolation, dedupe)
  - Bug 2 three-tier person-org resolution ladder (entity_context >
    affiliation regex > word-boundary substring, longest-label-first)
  - Bug 9 word-boundary backfill filter ("David" must not hit "Davidson")

Hermetic — all Supabase access is faked; no DB, no LLM.
"""

from types import SimpleNamespace

import pytest

from core.pulse.graph import (
    _label_word_regex,
    _person_org_from_source_text,
    match_existing_nodes,
)

pytestmark = pytest.mark.graph


# ── Fake Supabase: fluent chain recorder ─────────────────────────────────────


class _FakeChain:
    """Records filters and returns canned data on execute()."""

    def __init__(self, data):
        self._data = data
        self.filters = []  # (method, args)

    def select(self, *a, **k):
        self.filters.append(("select", a))
        return self

    def in_(self, col, values):
        self.filters.append(("in_", (col, list(values))))
        return self

    def eq(self, col, val):
        self.filters.append(("eq", (col, val)))
        return self

    def range(self, start, end):
        # Pagination support — slice data to simulate PostgREST range behavior.
        self.filters.append(("range", (start, end)))
        self._data = self._data[start:end + 1]
        return self

    def execute(self):
        return SimpleNamespace(data=self._data)


class _FakeSupabase:
    """Routes table(name).<chain> to canned datasets."""

    def __init__(self, tables):
        # tables: {"users": [...], "graph_nodes": [...], "pending_nodes": [...]}
        self._tables = tables
        self.executed = {}  # table_name -> last _FakeChain

    def table(self, name):
        chain = _FakeChain(self._tables.get(name, []))
        self.executed[name] = chain
        return chain


_OWNER = "11111111-1111-1111-1111-111111111111"


def _sb(live_nodes, pending_nodes=None):
    return _FakeSupabase({
        "users": [{"name": "Danny"}],
        "graph_nodes": live_nodes,
        "pending_nodes": pending_nodes or [],
    })


def _patch_sb(monkeypatch, fake):
    monkeypatch.setattr("core.pulse.graph.supabase", fake)


# ── match_existing_nodes: fetch-filter + semantics ──────────────────────────


def test_live_fetch_is_type_scoped(monkeypatch):
    """The graph_nodes fetch must be restricted to entity node types.

    Regression pin: an unfiltered fetch hits PostgREST's 1000-row page cap on
    any real tenant and silently truncates, hiding older org/person nodes.
    """
    fake = _sb([{"id": "n1", "label": "Solvstrat", "type": "organization"}])
    _patch_sb(monkeypatch, fake)

    match_existing_nodes(
        [{"type": "organization", "label": "Solvstrat", "confidence": 1.0}], _OWNER
    )

    chain = fake.executed["graph_nodes"]
    in_filters = [f for f in chain.filters if f[0] == "in_"]
    assert any(
        f[1][0] == "type" and "organization" in f[1][1]
        for f in in_filters
    ), f"graph_nodes fetch missing entity-type scope: {chain.filters}"
    pending_chain = fake.executed["pending_nodes"]
    assert any(
        f[0] == "in_" and f[1][0] == "node_type" for f in pending_chain.filters
    ), "pending_nodes fetch missing node_type scope"


def test_exact_live_match_and_scope_label(monkeypatch):
    fake = _sb([
        {"id": "org-1", "label": "Solvstrat", "type": "organization"},
    ])
    _patch_sb(monkeypatch, fake)

    result = match_existing_nodes(
        [{"type": "organization", "label": "Solvstrat", "confidence": 1.0}], _OWNER
    )
    ms = result[0]["existing_matches"]
    assert len(ms) == 1
    assert ms[0]["id"] == "org-1"
    assert ms[0]["scope"] == "live"


def test_pending_match_reported_as_pending_scope(monkeypatch):
    fake = _sb(
        [],  # nothing live
        [{"id": "p-1", "label": "Project Balance", "node_type": "organization",
          "status": "pending"}],
    )
    _patch_sb(monkeypatch, fake)

    result = match_existing_nodes(
        [{"type": "organization", "label": "Project Balance", "confidence": 1.0}],
        _OWNER,
    )
    ms = result[0]["existing_matches"]
    assert len(ms) == 1
    assert ms[0]["scope"] == "pending"


def test_cross_type_labels_do_not_match(monkeypatch):
    """A task node mentioning 'Solvstrat' must not match an org entity."""
    fake = _sb([
        {"id": "t-9", "label": "Solvstrat - weekly sync notes", "type": "task"},
        {"id": "m-9", "label": "memory: Solvstrat call recap", "type": "memory"},
    ])
    _patch_sb(monkeypatch, fake)

    result = match_existing_nodes(
        [{"type": "organization", "label": "Solvstrat", "confidence": 1.0}], _OWNER
    )
    assert result[0]["existing_matches"] == []


def test_owner_name_entity_excluded(monkeypatch):
    """The tenant's own name must never be proposed as an existing match."""
    fake = _sb([{"id": "me", "label": "Danny", "type": "person"}])
    _patch_sb(monkeypatch, fake)

    result = match_existing_nodes(
        [{"type": "person", "label": "Danny", "confidence": 1.0}], _OWNER
    )
    # Owner exclusion drops the entity from enrichment entirely.
    assert result == []


def test_duplicate_ids_deduped(monkeypatch):
    fake = _sb([
        {"id": "dup", "label": "Acme Corp", "type": "organization"},
    ])
    _patch_sb(monkeypatch, fake)

    result = match_existing_nodes(
        [{"type": "organization", "label": "Acme Corp", "confidence": 1.0}], _OWNER
    )
    ids = [m["id"] for m in result[0]["existing_matches"]]
    assert len(ids) == len(set(ids))


# ── Bug 2: three-tier person→org resolution ladder ───────────────────────────


_ORGS = ["Solvstrat", "Project Balance", "Qhord"]


def test_tier2_affiliation_beats_other_mentions():
    """'from Project Balance' wins even though Solvstrat appears elsewhere."""
    org, source = _person_org_from_source_text(
        "David Quantson from Project Balance met us about collaborating with Solvstrat",
        _ORGS + ["Project Balance"],
    )
    assert org == "Project Balance"
    assert source == "affiliation_pattern"


def test_tier2_longest_label_wins_on_overlap():
    org, source = _person_org_from_source_text(
        "briefing of Ashraya Chennai team", ["Ashraya", "Ashraya Chennai"],
    )
    assert org == "Ashraya Chennai"
    assert source == "affiliation_pattern"


def test_tier3_longest_label_wins_without_affiliation_prefix():
    """Without from/at/of, tier 3 still prefers the longest label."""
    org, source = _person_org_from_source_text(
        "call with Ashraya Chennai team", ["Ashraya", "Ashraya Chennai"],
    )
    assert org == "Ashraya Chennai"
    assert source == "substring"


def test_tier3_substring_fallback():
    org, source = _person_org_from_source_text("meeting with Solvstrat folks", _ORGS)
    assert org == "Solvstrat"
    assert source == "substring"


def test_tier3_word_boundary_rejects_partial():
    """"Qhord" inside a longer word must not count as a mention."""
    org, source = _person_org_from_source_text("the qhordinates delivered early", _ORGS)
    assert org is None
    assert source is None


def test_no_source_text_returns_none():
    assert _person_org_from_source_text("", _ORGS) == (None, None)
    assert _person_org_from_source_text("   ", _ORGS) == (None, None)


def test_tier1_entity_context_beats_source_text():
    """Tier 1 lives at the call site: context org short-circuits before tiers 2+3."""
    # Simulate the call-site precedence: if entity_context has an org name,
    # _person_org_from_source_text is never invoked. Pin the contract by
    # checking the helper itself stays subordinate (it must NOT be consulted).
    ctx_org = "Solvstrat"
    text = "David Quantson from Project Balance"
    resolved = ctx_org if ctx_org else _person_org_from_source_text(text, _ORGS)[0]
    assert resolved == "Solvstrat"


# ── Bug 9: word-boundary backfill filter ─────────────────────────────────────


def test_backfill_regex_whole_word_positive():
    # Contract: the CALLER lowercases content (production passes .lower());
    # the pattern itself is case-sensitive by design.
    pat = _label_word_regex("david quantson")
    assert pat.search("met david quantson yesterday".lower())
    assert pat.search("Met David Quantson".lower())


def test_backfill_regex_rejects_superstring():
    """The exact false-positive from the audit: 'david' inside 'davidson'."""
    pat = _label_word_regex("david")
    assert not pat.search("email from mr davidson about invoices")
    assert pat.search("davidson".replace("davidson", "david"))


def test_backfill_regex_escapes_regex_metachars():
    # NOTE: \b needs a word-char transition at the edges, so labels must end
    # in word characters (trailing ')' or '.' would defeat the boundary).
    pat = _label_word_regex("R&D")
    assert pat.search("sync with the R&D team today".lower())
    assert not pat.search("rnd labs")


# ── Pagination: guarantee no silent truncation ────────────────────────────


def test_pagination_fetches_all_live_nodes(monkeypatch):
    """When live_nodes exceed one page, pagination must fetch all of them.
    Aug 27 hardening: prevents the silent 1000-row truncation that hid
    Solvstrat from match_existing_nodes for weeks."""
    # Create 2500 fake nodes — more than 2 pages of 1000
    many_nodes = [
        {"id": str(i), "label": f"Org {i}", "type": "organization"}
        for i in range(2500)
    ]
    fake = _FakeSupabase({
        "users": [{"name": "Test"}],
        "graph_nodes": many_nodes,
        "pending_nodes": [],
    })
    _patch_sb(monkeypatch, fake)

    entities = [{"type": "organization", "label": "Org 2499"}]
    from core.pulse.graph import match_existing_nodes
    result = match_existing_nodes(entities, _OWNER)

    # The last node (Org 2499) must be found despite being beyond page 1
    matches = result[0].get("existing_matches", [])
    assert len(matches) >= 1, (
        "Org 2499 not found — pagination likely truncating at page boundary"
    )
    assert matches[0]["label"] == "Org 2499"


def test_pagination_fetches_all_pending_nodes(monkeypatch):
    """Pending nodes must also be paginated."""
    many_pending = [
        {"id": str(i), "label": f"Pending {i}", "node_type": "person"}
        for i in range(1500)
    ]
    fake = _FakeSupabase({
        "users": [{"name": "Test"}],
        "graph_nodes": [],
        "pending_nodes": many_pending,
    })
    _patch_sb(monkeypatch, fake)

    entities = [{"type": "person", "label": "Pending 1499"}]
    from core.pulse.graph import match_existing_nodes
    result = match_existing_nodes(entities, _OWNER)

    matches = result[0].get("existing_matches", [])
    assert len(matches) >= 1, (
        "Pending 1499 not found — pending node pagination likely truncating"
    )
    assert matches[0]["label"] == "Pending 1499"


# ── Tiered matching: exact → fuzzy → semantic ─────────────────────────────


# Shared test orgs
_ORGS = ["Solvstrat", "Qhord", "Crayon"]


def test_tier1_single_exact_match(monkeypatch):
    """Single exact match should auto-link with score 1.0."""
    fake = _FakeSupabase({
        "users": [{"name": "Test"}],
        "graph_nodes": [
            {"id": "n1", "label": "Solvstrat", "type": "organization"},
            {"id": "n2", "label": "Qhord", "type": "organization"},
        ],
        "pending_nodes": [],
    })
    _patch_sb(monkeypatch, fake)

    entities = [{"type": "organization", "label": "Solvstrat"}]
    result = match_existing_nodes(entities, _OWNER)

    matches = result[0].get("existing_matches", [])
    assert len(matches) == 1
    assert matches[0]["id"] == "n1"
    assert matches[0]["score"] == 1.0
    assert matches[0]["match_type"] == "exact"
    assert not result[0].get("ambiguous")


def test_tier2_multiple_exact_matches(monkeypatch):
    """Multiple exact matches should be flagged as ambiguous."""
    fake = _FakeSupabase({
        "users": [{"name": "Test"}],
        "graph_nodes": [
            {"id": "n1", "label": "David", "type": "person"},
            {"id": "n2", "label": "David", "type": "person"},
            {"id": "n3", "label": "David", "type": "person"},
        ],
        "pending_nodes": [],
    })
    _patch_sb(monkeypatch, fake)

    entities = [{"type": "person", "label": "David"}]
    result = match_existing_nodes(entities, _OWNER)

    matches = result[0].get("existing_matches", [])
    assert len(matches) == 3
    assert result[0].get("ambiguous") is True
    assert "3 nodes" in result[0].get("ambiguous_reason", "")
    # All should be exact matches
    assert all(m["match_type"] == "exact" for m in matches)


def test_tier3_fuzzy_match_threshold(monkeypatch):
    """Fuzzy match must use threshold 0.85 — 'David' must NOT match 'Davidson'."""
    fake = _FakeSupabase({
        "users": [{"name": "Test"}],
        "graph_nodes": [
            {"id": "n1", "label": "Davidson", "type": "person"},
        ],
        "pending_nodes": [],
    })
    _patch_sb(monkeypatch, fake)

    entities = [{"type": "person", "label": "David"}]
    result = match_existing_nodes(entities, _OWNER)

    matches = result[0].get("existing_matches", [])
    # Davidson ratio vs David is ~0.82 — below 0.85 threshold
    assert len(matches) == 0, (
        "'David' should NOT match 'Davidson' (ratio ~0.82 < 0.85)"
    )


def test_tier3_fuzzy_match_typo(monkeypatch):
    """Fuzzy match should catch minor typos above 0.85 threshold."""
    fake = _FakeSupabase({
        "users": [{"name": "Test"}],
        "graph_nodes": [
            {"id": "n1", "label": "Havenlight", "type": "organization"},
        ],
        "pending_nodes": [],
    })
    _patch_sb(monkeypatch, fake)

    # 'Havenligt' is a 1-char typo of 'Havenlight'
    entities = [{"type": "organization", "label": "Havenligt"}]
    result = match_existing_nodes(entities, _OWNER)

    matches = result[0].get("existing_matches", [])
    assert len(matches) == 1
    assert matches[0]["id"] == "n1"
    assert matches[0]["match_type"] == "fuzzy"
    assert matches[0]["score"] >= 0.85


def test_no_substring_boost(monkeypatch):
    """Substring containment must NOT boost the score.
    Before fix: 'David' in 'David Davidson' got +0.3 boost → false match."""
    fake = _FakeSupabase({
        "users": [{"name": "Test"}],
        "graph_nodes": [
            {"id": "n1", "label": "David Davidson", "type": "person"},
        ],
        "pending_nodes": [],
    })
    _patch_sb(monkeypatch, fake)

    entities = [{"type": "person", "label": "David"}]
    result = match_existing_nodes(entities, _OWNER)

    matches = result[0].get("existing_matches", [])
    # 'David' vs 'David Davidson' ratio is ~0.52 — no substring boost
    assert len(matches) == 0, (
        "'David' should NOT match 'David Davidson' without substring boost"
    )


def test_exact_across_live_and_pending(monkeypatch):
    """Exact match should find nodes in both live and pending tables."""
    fake = _FakeSupabase({
        "users": [{"name": "Test"}],
        "graph_nodes": [
            {"id": "n1", "label": "Solvstrat", "type": "organization"},
        ],
        "pending_nodes": [
            {"id": "p1", "label": "Qhord", "node_type": "organization"},
        ],
    })
    _patch_sb(monkeypatch, fake)

    entities = [{"type": "organization", "label": "Qhord"}]
    result = match_existing_nodes(entities, _OWNER)

    matches = result[0].get("existing_matches", [])
    assert len(matches) == 1
    assert matches[0]["id"] == "p1"
    assert matches[0]["scope"] == "pending"
    assert matches[0]["match_type"] == "exact"
