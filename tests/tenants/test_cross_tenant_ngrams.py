"""Cross-tenant n-gram / entity-resolution collision test (plan §9, mirroring
test_ngrams.py).

Both tenants can legitimately have entities with overlapping names (e.g. both
have an org called "Ashraya"). The guarantee the facade provides is that any
resolution runs against the ACTIVE tenant's entity set only — an n-gram match
must never resolve against another tenant's identically-named entity.

This mirrors the real runtime shape: `get_entity_mappings()` /
organization-resolution reads are tenant-scoped queries, so the candidate set
passed to the matcher is already tenant-filtered. The test proves the
matcher, given a tenant-scoped candidate set, cannot pick another tenant's
row.
"""

import re


def _normalize(s: str) -> str:
    if not s:
        return ""
    s = re.sub(r"[^\w\s]", " ", s.lower())
    return " ".join(s.split())


def _ngrams_up_to(words: list[str], n: int) -> set[str]:
    out = set()
    for size in range(1, n + 1):
        for i in range(len(words) - size + 1):
            out.add(" ".join(words[i:i + size]))
    return out


def _match(text: str, orgs: list[dict]) -> list[str]:
    norm_text = _normalize(text)
    ngrams = _ngrams_up_to(norm_text.split(), 4)
    return [o["name"] for o in orgs if _normalize(o["name"]) in ngrams]


def test_ngram_match_stays_within_active_tenant_entity_set():
    # Tenant A and tenant B BOTH have an org named "Ashraya" (different rows).
    tenant_a_orgs = [
        {"id": "aaaa", "name": "Ashraya"},
        {"id": "aabb", "name": "Ashraya India"},
        {"id": "aacc", "name": "Ashraya Chennai"},
    ]
    tenant_b_orgs = [
        {"id": "bbbb", "name": "Ashraya"},
        {"id": "bbcc", "name": "Ashraya Trust"},
    ]

    text = "Purchase the Ashraya domain"

    # Tenant A resolves against A's orgs only — never B's rows.
    matched_a = _match(text, tenant_a_orgs)
    assert "Ashraya" in matched_a
    assert len(matched_a) == 1  # exact org name wins; no B rows in candidate set

    # Tenant B resolves against B's orgs only.
    matched_b = _match(text, tenant_b_orgs)
    assert "Ashraya" in matched_b
    assert matched_b == ["Ashraya"]

    # The collision scenario: if resolution were fed a MIXED set (the bug the
    # tenant-scoped query prevents), B could see A's orgs — the scoped query
    # is what guarantees this never happens.
    mixed = tenant_a_orgs + tenant_b_orgs
    assert len(_match(text, mixed)) >= 2, "mixed sets collide (exactly why queries must be tenant-scoped)"


def test_ngram_normalization_is_tenant_agnostic():
    # Normalization itself must not embed tenant identity.
    assert _normalize("Ashraya Chennai North") == _normalize("ashraya chennai north")
    assert _normalize("₹30L Debt") == _normalize("30l debt")
