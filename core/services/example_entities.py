"""
example_entities.py — M9.2 data-driven prompt examples (plans/70 §M9.2).

The ROLE_UPDATE worked example in the classify prompt is built from the
tenant's OWN graph instead of a hardcoded literal ("Marcus Durai is the
Pastor of Ashraya Chennai Central" — Danny's data, baked into code).

Design contract (the M9.2 hardening spec):
  1. IMPORTANCE GATE  — only entities that earned a canonical_pages row
     qualify (that row exists only because brain_synth_v2 decided the entity
     was worth permanent memory). Frequency of mention is NOT a signal, so an
     unimportant person is never picked as an example.
  2. NOISE FILTER     — blocklisted people and degenerate labels excluded.
  3. DETERMINISTIC    — candidates sorted by label ASC; same DB state ⇒ same
     prompt every run (the byte-diff gate must not flake).
  4. PER-TENANT CACHE — keyed by owner_id (resolved BEFORE the cache lookup —
     never a global cache, the google_service cross-tenant lesson), TTL 15min.
  5. NEVER-RAISE      — any exception ⇒ the neutral example line; no crash,
     no 500, never another tenant's entity.
  6. INJECTION GUARD  — a person with no stored role never enters the role
     example; when no candidate exists the neutral line is rendered.

For Danny this resolves to his graph's Marcus Durai + stored role, rendering
the exact same example he has today — proven by the committed baseline
(tests/golden/classify_tenant1.txt) via scripts/verify_m9_2_examples.py.
"""

from __future__ import annotations

import time

from core.services.db import tenant_aware_client, get_tenant
from core.services.user_settings import resolve_domains


CACHE_TTL_SECONDS = 900  # 15 min — examples shouldn't go stale mid-day

# The neutral fallback — no names, never a fake fact. Rendered for fresh
# tenants (no canonical pages yet) and on any DB error.
NEUTRAL_EXAMPLE = (
    'Example: "A colleague is now the head of a client organization" '
    '→ intent=ROLE_UPDATE, person_name="their name", role_title="their role", '
    'org_name="their organization", entity=the matching domain or INBOX.'
)


# owner_id -> (fetched_ts, rendered_example_line)
_cache: dict[str, tuple[float, str]] = {}


def clear_cache(user_id: str | None = None) -> None:
    """Drop cached examples (tests / role updates / settings edits)."""
    if user_id is None:
        _cache.clear()
    else:
        _cache.pop(user_id, None)


def _fetch_important_titles(db, limit: int = 500) -> set[str]:
    """Titles of canonical pages (owner-scoped via the facade) — the
    importance gate. Returns ORIGINAL-case titles (the IN-query payload must
    match stored graph labels exactly)."""
    titles: set[str] = set()
    try:
        res = (
            db.table("canonical_pages")
            .select("title")
            .eq("is_current", True)
            .order("updated_at", desc=True)
            .limit(limit)
            .execute()
        )
        for row in res.data or []:
            t = (row.get("title") or "").strip()
            if t:
                titles.add(t)
    except Exception:
        pass  # fail-open → empty gate (never-raise holds downstream)
    return titles


def _parse_person_row(row: dict) -> dict | None:
    """Extract {label, role, org} from a graph_nodes row; None if unusable."""
    label = (row.get("label") or "").strip()
    meta = row.get("metadata") or {}
    if isinstance(meta, str):
        try:
            import json

            meta = json.loads(meta)
        except Exception:
            meta = {}
    enrichment = (meta.get("enrichment") or {}) if isinstance(meta, dict) else {}
    if not isinstance(enrichment, dict):
        enrichment = {}
    role = (enrichment.get("role") or "").strip()
    org = (enrichment.get("organization_name") or "").strip()
    if not label or not role:
        return None
    return {"label": label, "role": role, "org": org}


def _fetch_role_people(db, titles_orig: set[str], titles_lower: set[str]) -> list[dict]:
    """Person nodes whose label earned a canonical page, with their metadata.

    Bounded by the importance gate (the candidate set), owner-scoped by the
    facade. Primary path: exact-label IN against original-case titles (canonical
    page titles are created from graph entity names, so casing matches).
    Defensive fallback: if nothing matched, scan bounded person rows and match
    case-insensitively (protects against page-title vs node-label case drift).
    """
    if not titles_orig:
        return []
    people: list[dict] = []

    def _collect(res) -> None:
        for row in (res.data if res and res.data else []):
            parsed = _parse_person_row(row)
            if parsed:
                people.append(parsed)

    # Primary: exact-label IN (chunked to stay under URL limits)
    chunk = sorted(titles_orig)
    for i in range(0, len(chunk), 100):
        batch = chunk[i : i + 100]
        try:
            res = (
                db.table("graph_nodes")
                .select("label, metadata")
                .eq("type", "person")
                .eq("is_current", True)
                .in_("label", batch)
                .limit(300)
                .execute()
            )
            _collect(res)
        except Exception:
            continue  # fail-open on query error

    # Defensive fallback: case drift between page titles and node labels
    if not people and titles_lower:
        try:
            res = (
                db.table("graph_nodes")
                .select("label, metadata")
                .eq("type", "person")
                .eq("is_current", True)
                .limit(500)
                .execute()
            )
            for row in (res.data if res and res.data else []):
                parsed = _parse_person_row(row)
                if parsed and parsed["label"].lower() in titles_lower:
                    people.append(parsed)
        except Exception:
            pass  # fail-open

    return people


def _resolve_entity_for_org(org: str, uid: str | None) -> str:
    """Routing domain whose keywords match the org (→ entity tag in JSON).

    Uppercased to match the entity-tag style of the original prompt literal
    ("entity=ASHRAYA") — byte-identical for Danny, consistent for everyone.
    `uid` threads the resolved tenant through so reads and the cache key are
    consistent (never the active-context tenant when a different id was
    passed).
    """
    if not org:
        return "INBOX"
    org_lower = org.lower()
    for d in resolve_domains(uid):
        name = (d.get("name") or "").strip()
        if not name:
            continue
        kws = [str(k).lower() for k in (d.get("keywords") or [])]
        if any(kw and kw in org_lower for kw in kws):
            return name.upper()
    return "INBOX"


def _render_example(person: dict, uid: str | None) -> str:
    """Render the ROLE_UPDATE example line from a confirmed person node.

    Stored shape (dispatch.handle_role_update): enrichment.role =
    "Pastor of Ashraya Chennai Central", enrichment.organization_name =
    "Ashraya Chennai Central". Reconstructs:
        Example: "Marcus Durai is the Pastor of Ashraya Chennai Central"
        → intent=ROLE_UPDATE, person_name="Marcus Durai", role_title="Pastor",
          org_name="Ashraya Chennai Central", entity=ASHRAYA.
    """
    label = person["label"]
    role = person["role"]
    org = person["org"]
    if not org and " of " in role:
        org = role.split(" of ", 1)[1].strip()
        role_title = role.split(" of ", 1)[0].strip()
    elif org and role.endswith(f" of {org}"):
        role_title = role[: -len(f" of {org}")].strip()
    else:
        role_title = role
    entity = _resolve_entity_for_org(org, uid)
    return (
        f'Example: "{label} is the {role}" '
        f'→ intent=ROLE_UPDATE, person_name="{label}", '
        f'role_title="{role_title}", org_name="{org}", entity={entity}.'
    )


def resolve_role_update_example(user_id: str | None = None) -> str:
    """The ROLE_UPDATE example line for the classify prompt.

    Chain: per-tenant cache → canonical-pages-gated person with stored role →
    neutral line. Never raises.
    """
    # 4. PER-TENANT CACHE — keyed by owner id, resolved BEFORE the lookup.
    # `uid` is the single resolution of the tenant: used for the cache key AND
    # threaded into every read (domains, entity resolution) so a caller passing
    # an explicit id can never cache another tenant's data under that id.
    uid = user_id or get_tenant()
    now = time.time()
    if uid:
        hit = _cache.get(uid)
        if hit and now - hit[0] < CACHE_TTL_SECONDS:
            return hit[1]

    try:
        db = tenant_aware_client()
        titles = _fetch_important_titles(db)
        titles_lower = {t.lower() for t in titles}
        people = _fetch_role_people(db, titles, titles_lower)

        # 2. NOISE FILTER — blocklisted people never become examples.
        try:
            from core.lib.people_utils import is_blocklisted_person

            people = [p for p in people if not is_blocklisted_person(p["label"])]
        except Exception:
            pass

        # 3. DETERMINISTIC — label ASC tiebreak.
        people.sort(key=lambda p: p["label"].lower())

        example = _render_example(people[0], uid) if people else NEUTRAL_EXAMPLE
    except Exception:
        # 5. NEVER-RAISE — any failure degrades to the neutral line.
        example = NEUTRAL_EXAMPLE

    if uid:
        _cache[uid] = (time.time(), example)
    return example
