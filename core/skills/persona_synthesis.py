"""Monthly persona synthesis (M18) — the Persona Layer job.

Pipeline (hardened, fail-closed)::

    deterministic extraction (owner-scoped, source-referenced facts)
        → LLM transform (facts → card draft)
        → CODE VERIFIER (G1-G4: traceability, timing, edges, sensitive)
        → pass?  → versioned upsert (core_config 'persona' + 'persona_prev')
        → fail?  → reject, log reason, keep the previous card

Every claim in the card must trace to a row in the tenant's own data. A
card that fails any gate is NEVER written. On any LLM/DB error the previous
card stays (fail-closed).

Run (GHA monthly cron + manual dispatch; fans out to all tenants)::

    python -m core.skills.persona_synthesis            # all tenants
    python -m core.skills.persona_synthesis --user Danny
    python -m core.skills.persona_synthesis --dry-run  # preview, no writes
    python -m core.skills.persona_synthesis --restore  # roll back to prev
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
from collections import Counter
from datetime import datetime, timezone

from core.lib.audit_logger import audit_log_sync
from core.services.db import arun_tenant_fanout, get_supabase, tenant_scope
from core.services.persona import (
    CARD_SCHEMA_VERSION,
    _PERSONA_KEY,
    _PERSONA_PREV_KEY,
    clear_persona_cache,
)
from core.services.persona_verifier import verify_persona_card

_PAGE = 1000
_MAX_NODES = 5000
_MAX_EDGES = 20000

_PERSON_TYPES = {"person", "people", "human"}
_ORG_TYPES = {"org", "organization", "company", "project", "business"}

# Life texture: relationship roles that say who the user IS outside work.
_FAMILY_RELS = {"FAMILY_OF", "SPOUSE_OF", "PARENT_OF", "CHILD_OF",
                "SIBLING_OF", "FRIEND_OF", "RELATES_TO"}
_ROLE_WORD = {"FAMILY_OF": "family", "SPOUSE_OF": "spouse",
              "PARENT_OF": "parent", "CHILD_OF": "child",
              "SIBLING_OF": "sibling", "FRIEND_OF": "friend",
              "RELATES_TO": "close to"}

# Curated-row sections that describe WORK, not life — people under these
# never become life texture (the card's personal circle is family/home).
_CURATED_WORK_SECTIONS = {
    "professional", "work", "business", "team", "client", "clients",
    "colleague", "colleagues", "office", "vendor", "vendors",
}

# Curated-row role words → the card vocabulary ("Wife - URGENT/Connection"
# cleans to "wife", which maps to the graph term "spouse").
_CURATED_ROLE_WORD = {
    "wife": "spouse", "husband": "spouse", "partner": "spouse",
    "son": "child", "daughter": "child", "kid": "child",
    "kids": "child", "children": "child", "mom": "parent",
    "mother": "parent", "dad": "parent", "father": "parent",
    "brother": "sibling", "sister": "sibling", "friend": "friend",
    "family": "family", "close to": "close to",
}

# Positive life signals to surface from introspection/relationship memories
# (snippets containing a sensitive keyword are excluded — the boundary wins).
_LIFE_KEYWORDS = (
    "anniversary", "wedding", "wife", "husband", "kids", "children",
    "son", "daughter", "dog", "puppy", "vet", "prayer", "church",
    "family", "love", "blessed", "grateful", "hug", "mom", "dad",
    "birthday", "vacation", "trip", "home",
)

# Personal-introspection memory types — the ONLY places sensitive boundaries
# are mined. Work notes (relationship_note/note/outcome) are excluded so a
# legitimate finance domain never trips a guard.
_SENSITIVE_TYPES = {"Journal", "Prayer", "Psalm", "reflection", "archive"}

_SENSITIVE_KEYWORDS = (
    "debt", "loan", "mortgage", "bankruptcy", "overdraft",
    "stress", "overwhelmed", "broken", "failure", "suicidal", "suicide",
    "depressed", "depression", "come clean", "dread", "feel like living",
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _norm_label(s: str) -> str:
    """Casefold label for alias/label matching (keeps spaces/punctuation)."""
    return (s or "").strip().casefold()


def _paginate(
    table_name: str, cols: str, owner_id: str, cap: int, extra_eq: tuple = ()
) -> list[dict]:
    db = get_supabase()
    out: list[dict] = []
    offset = 0
    while offset < cap:
        q = db.table(table_name).select(cols).eq("owner_id", owner_id)
        if extra_eq:
            q = q.eq(*extra_eq)
        batch = q.range(offset, offset + _PAGE - 1).limit(_PAGE).execute().data
        if not batch:
            break
        out.extend(batch)
        offset += len(batch)
        if len(batch) < _PAGE:
            break
    return out


def extract_facts(owner_id: str) -> dict:
    """Deterministic, owner-scoped fact bundle with source references."""
    from core.services.user_settings import resolve_context, resolve_user_orgs

    context = resolve_context(owner_id)
    domains = [d.get("name", "") for d in resolve_user_orgs(owner_id) if d.get("name")]

    try:
        from core.lib.graph_rules import resolve_root_label

        root_label = resolve_root_label() or ""
    except Exception:
        root_label = ""

    nodes = _paginate("graph_nodes", "id,label,type", owner_id, _MAX_NODES)
    edges = _paginate(
        "graph_edges", "source_node_id,target_node_id,relationship", owner_id, _MAX_EDGES
    )

    deg = Counter()
    for e in edges:
        deg[e["source_node_id"]] += 1
        deg[e["target_node_id"]] += 1

    by_id = {n["id"]: n for n in nodes}
    # People cap MUST match the read-path contract (validate_card_shape:
    # people<=10). The LLM is told to pick from this list, so a 12-person
    # pool invites an 11-person card that the read path would silently
    # reject (the dormant-persona bug). Cap at the contract.
    people = sorted(
        (n for n in nodes if (n.get("type") or "").lower() in _PERSON_TYPES),
        key=lambda n: -deg[n["id"]],
    )[:10]
    orgs = sorted(
        (n for n in nodes if (n.get("type") or "").lower() in _ORG_TYPES),
        key=lambda n: -deg[n["id"]],
    )[:8]

    # ── Life texture: who the user IS outside work. ──────────────────────
    # M18a: the tenant's CURATED 'relationships' config row is the
    # authoritative life circle (their own written answer — wife/kids/family
    # they chose). The graph is only the fallback for tenants who never
    # curated a row. Curated names are resolved to canonical graph labels
    # via exact label match, then token-prefix, then entity_mappings aliases.
    life_roles: list[str] = []
    life_other_ids: set[str] = set()
    curated_display_names: set[str] = set()
    from core.services.user_settings import resolve_curated_people

    curated = resolve_curated_people(owner_id)
    if curated:
        from core.lib.graph_rules import resolve_alias

        _label_by_norm: dict[str, str] = {}
        for n in nodes:
            _label_by_norm.setdefault(_norm_label(n.get("label") or ""), n["label"])

        seen: set[tuple[str, str]] = set()
        for person in curated:
            name = (person.get("name") or "").strip()
            if not name:
                continue
            # Life texture only — work sections (PROFESSIONAL/TEAM/...) stay
            # out of the personal circle (they are real people, just not the
            # card's life snapshot).
            if (person.get("section") or "").strip().lower() in _CURATED_WORK_SECTIONS:
                continue
            # Resolve curated name -> canonical graph label: the graph's own
            # alias resolver FIRST (metadata.aliases, migration 76 — the same
            # "sunju" → "Sunjula Daniel" path the chat uses), then exact node
            # label, then the curated name verbatim. Alias-first matters:
            # a nickname NODE can exist ("Sunju") whose exact label would
            # otherwise shadow the canonical person.
            try:
                canon_label = resolve_alias(name) or None
            except Exception:
                canon_label = None
            if not canon_label or canon_label.strip().casefold() == _norm_label(name):
                canon_label = _label_by_norm.get(_norm_label(name)) or canon_label or name
            display = canon_label or name
            _role_raw = (person.get("role") or "family").strip().lower()
            role = _CURATED_ROLE_WORD.get(_role_raw, _role_raw)
            entry = f"{display} ({role})"
            key = (display.casefold(), role)
            if key in seen:
                continue
            seen.add(key)
            life_roles.append(entry)
            curated_display_names.add(display)
            # Vocabulary: canonical node if resolvable, else the curated name
            # itself (a source row — the verifier's snapshot gate accepts it).
            matched = next(
                (n for n in nodes if n.get("label") == canon_label), None
            ) if canon_label else None
            if matched:
                life_other_ids.add(matched["id"])
    else:
        for e in edges:
            a = by_id.get(e["source_node_id"])
            b = by_id.get(e["target_node_id"])
            rel = (e.get("relationship") or "").strip().upper()
            if not a or not b or rel not in _FAMILY_RELS:
                continue
            if a.get("label") == root_label:
                other = b
            elif b.get("label") == root_label:
                other = a
            else:
                continue
            if other.get("label") == root_label:
                continue
            role = _ROLE_WORD.get(rel, rel.lower())
            entry = f"{other['label']} ({role})"
            if entry not in life_roles:
                life_roles.append(entry)
                life_other_ids.add(other["id"])

    # Claim vocabulary: root + top people + top orgs + family counterparts
    # (both IDs and LABELS — family members are entities the card may name).
    # Curated names that never resolved to a node are STILL source rows —
    # the verifier's snapshot gate accepts them as provided vocabulary.
    kept_labels = ({root_label} | {n["label"] for n in people + orgs}
                   | {by_id[fid]["label"] for fid in life_other_ids if fid in by_id}
                   | curated_display_names)
    kept_ids = {n["id"] for n in people + orgs} | life_other_ids
    if root_label:
        for n in nodes:
            if n["label"] == root_label:
                kept_ids.add(n["id"])
                break
    label_of = {nid: n["label"] for nid, n in by_id.items() if nid in kept_ids}
    known_triples: set[tuple[str, str, str]] = set()
    for e in edges:
        a = label_of.get(e["source_node_id"])
        b = label_of.get(e["target_node_id"])
        rel = (e.get("relationship") or "").strip().upper()
        if a and b and rel:
            known_triples.add((a, rel, b))
            known_triples.add((b, rel, a))

    telemetry: Counter = Counter()
    for r in _paginate("subsystem_telemetry", "outcome", owner_id, 2000):
        if r.get("outcome"):
            telemetry[r["outcome"]] += 1

    all_memories = _paginate(
        "memories", "memory_type,content,created_at", owner_id, 10000
    )

    # ── Life signals: literal positive snippets from the user's OWN words.
    # Introspection types (Journal/Prayer/Psalm/reflection/archive) always
    # qualify; relationship_notes only when they are prayer-group chatter
    # (the one third-party category with life texture). Work chatter and
    # anything touching a sensitive boundary is excluded — the boundary
    # always wins over texture.
    _WORK_STOPWORDS = (
        "office", "task", "team", "client", "project", "meeting", "email",
        "domain", "hosting", "blacklist", "players", "aar:", "inform",
        "informed", "teams:", "work", "deadline", "invoice", "pricing",
        "gtm", "product", "api", "zoho",
    )
    life_signals: list[str] = []
    for m in all_memories:
        mtype = m.get("memory_type") or ""
        c = str(m.get("content") or "")
        cl = c.lower()
        if mtype in _SENSITIVE_TYPES:
            pass
        elif mtype == "relationship_note" and re.match(r"^(90[- ]day prayer|prayer)", cl):
            pass
        else:
            continue
        if not any(kw in cl for kw in _LIFE_KEYWORDS):
            continue
        if any(sk in cl for sk in _SENSITIVE_KEYWORDS):
            continue
        if any(w in cl for w in _WORK_STOPWORDS):
            continue
        sentence = re.split(r"[.!?]\s", c, maxsplit=1)[0].strip()[:110]
        if sentence and sentence not in life_signals:
            life_signals.append(sentence)
        if len(life_signals) >= 6:
            break

    life_snapshot = life_roles[:4] + life_signals[:6]

    recent = sorted(
        all_memories, key=lambda m: str(m.get("created_at") or ""), reverse=True
    )[:6]
    recent_texts = [str(m.get("content") or "")[:140] for m in recent]

    # Sensitive topics: scan the personal-introspection memory types across
    # ALL of history (a year-old Journal entry is still a boundary).
    sensitive = sorted({
        kw for kw in _SENSITIVE_KEYWORDS
        for m in all_memories
        if m.get("memory_type") in _SENSITIVE_TYPES
        and kw in str(m.get("content") or "").lower()
    })

    fingerprint = {
        "nodes": len(nodes),
        "edges": len(edges),
        "people": len(people),
        "orgs": len(orgs),
        "confirmed": telemetry.get("confirmed", 0),
        "corrected": telemetry.get("corrected", 0),
        "rejected": telemetry.get("rejected", 0),
        "sensitive_topics": sensitive,
    }

    return {
        "context": context,
        "domains": domains,
        "root_label": root_label,
        "people": [n["label"] for n in people],
        "orgs": [n["label"] for n in orgs],
        "life_snapshot": life_snapshot,
        "allowed_names": sorted(kept_labels),
        "known_triples": sorted(known_triples),
        "telemetry": dict(telemetry),
        "recent_memories": recent_texts,
        "sensitive_topics": sensitive,
        "fingerprint": fingerprint,
    }


def build_transform_prompt(facts: dict) -> str:
    """Instruct the LLM to transform facts into a card — never invent.

    The facts JSON is bounded deterministically BEFORE serialization (never
    sliced mid-JSON) so a rich tenant cannot silently lose facts.
    """
    slim = dict(facts)
    slim["known_triples"] = sorted(facts["known_triples"])[:150]
    slim["allowed_names"] = sorted(facts["allowed_names"])[:80]
    slim["life_snapshot"] = facts.get("life_snapshot", [])[:10]
    slim["recent_memories"] = facts["recent_memories"][:4]
    facts_json = json.dumps(slim, ensure_ascii=False, indent=1)
    return f"""You are building a "persona card" for one user of a personal assistant.
The card tells the assistant who this user is, so it can write fitting briefings
and sign-offs. The card is READ BY OTHER MODELS, so it must be trustworthy.

HARD RULES — the card is verified by a deterministic checker and rejected if violated:
1. Use ONLY the facts below. Never invent, infer, or combine facts into new claims.
2. Never mention times, days, or dates in `who`, `style`, or `signoffs` ("tonight",
   "tomorrow", "this week").
3. Never mention a person or organization that is not in the provided lists.
4. Every `claims` entry must be a relationship that is literally in `known_triples`.
5. If a sensitive topic appears in `sensitive_topics`, it may ONLY appear inside
   the `never` list — never in `who`, `style`, `signoffs`, or `life_snapshot`.
6. Sign-offs are 3-12 words in Rhodey's EXISTING voice: the calm, concise
   chief-of-staff register — warm but composed, direct, never gushing.
   Ground them in `life_snapshot` (family/home warmth like parenthood,
   faith, pets) so they feel personal, but keep the current Rhodey tone
   (e.g. "Rest well." / "Locked in for the night."). NOT an email or
   letter: never "regards", "sincerely", "yours truly", "faithfully",
   "cheers", "best". NOT a casual text either: no "hey", "xo", "thinking
   of you", emoji, exclamation-heavy slang. CRITICAL: never use ANY
   person's name in a sign-off — not even a family member's — and never a
   time word ("today", "tonight", "tomorrow", "this week", "on Monday"...):
   time words and name-drops are rejected by the checker.
7. `who` is one sentence, <= 200 chars, and may weave in life facts from
   `life_snapshot` (roles, family, faith). `style.voice` <= 150 chars.
8. `life_snapshot` (optional, max 6 entries) lists the most human life facts,
   chosen VERBATIM from the provided `life_snapshot` facts — never reworded,
   never combined, never invented.

Output ONLY valid JSON with this exact shape:
{{
  "who": "...",
  "people": ["...", "..."],
  "domains": ["...", "..."],
  "style": {{"voice": "..."}},
  "signoffs": ["...", "..."],
  "claims": [{{"subject": "...", "predicate": "...", "object": "..."}}],
  "life_snapshot": ["...", "..."],
  "never": ["..."]
}}

FACTS (all verified, from the user's own data):
{facts_json}
"""


def _parse_llm_json(text: str) -> dict | None:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```[a-zA-Z]*\n?", "", cleaned)
        cleaned = re.sub(r"\n?```$", "", cleaned)
    try:
        parsed = json.loads(cleaned)
        return parsed if isinstance(parsed, dict) else None
    except Exception:
        return None


def synthesize_tenant(owner_id: str, dry_run: bool = False) -> bool:
    """Full pipeline for one tenant. Returns True when a card was written."""
    facts = extract_facts(owner_id)
    name = facts.get("root_label") or owner_id[:8]
    print(f"🔎 {name}: {facts['fingerprint']}", flush=True)

    if not facts["people"] and not facts["orgs"]:
        print(f"⏭️  {name}: graph too thin — no card (cold start), keeping previous.", flush=True)
        audit_log_sync("persona", "INFO", f"cold start: no card for {owner_id}")
        return False

    from core.llm.compat import call_llm_with_fallback_sync
    from core.llm.constants import CLASSIFICATION_MODEL

    try:
        resp = call_llm_with_fallback_sync(
            build_transform_prompt(facts),
            model=CLASSIFICATION_MODEL,
            is_critical=False,
        )
    except Exception as e:
        print(f"❌ {name}: LLM failed — previous card kept: {e}", flush=True)
        audit_log_sync("persona", "ERROR", f"synthesis LLM failed for {owner_id}: {e}")
        return False

    card = _parse_llm_json(getattr(resp, "text", "") or "")
    if card is None:
        print(f"❌ {name}: LLM returned unparseable JSON — previous card kept.", flush=True)
        audit_log_sync("persona", "ERROR", f"unparseable card for {owner_id}")
        return False

    card["schema_version"] = CARD_SCHEMA_VERSION
    card["source_fingerprint"] = facts["fingerprint"]
    card["generated_at"] = _now_iso()

    # Normalize at WRITE time so stored cards are guaranteed clean: the read
    # path (persona_voice_block) strips too, so every surface renders the
    # exact same byte-string (byte-identical guarantee holds by construction).
    if isinstance(card.get("who"), str):
        card["who"] = card["who"].strip()
    style = card.get("style") or {}
    if isinstance(style.get("voice"), str):
        style["voice"] = style["voice"].strip()
    never = card.get("never")
    if isinstance(never, list):
        card["never"] = [t.strip() for t in never if isinstance(t, str) and t.strip()]
    # Count backstops — clamp to the read-path contract (validate_card_shape)
    # BEFORE the verifier so a non-compliant LLM output is either clamped or
    # rejected at write time, never silently refused at read time. The
    # verifier's count gates below are the real enforcement; clamping here
    # just avoids churning on trivially over-long lists.
    for _key, _cap in (("people", 10), ("domains", 8),
                       ("life_snapshot", 12), ("claims", 20)):
        _val = card.get(_key)
        if isinstance(_val, list) and len(_val) > _cap:
            card[_key] = _val[:_cap]
    _sign = card.get("signoffs")
    if isinstance(_sign, list) and len(_sign) > 4:
        card["signoffs"] = _sign[:4]

    ok, errors = verify_persona_card(card, facts)
    if not ok:
        print(f"❌ {name}: verifier rejected card ({len(errors)} issues) — previous card kept.", flush=True)
        for err in errors[:12]:
            print(f"   - {err}", flush=True)
        audit_log_sync("persona", "WARN", f"card rejected for {owner_id}", {"errors": errors[:12]})
        return False

    print(f"✅ {name}: card passed all gates ({len(card.get('claims', []))} claims).", flush=True)
    if dry_run:
        print(f"   [dry-run] who: {card['who'][:120]}", flush=True)
        print(f"   [dry-run] signoffs: {card['signoffs']}", flush=True)
        print(f"   [dry-run] life: {card.get('life_snapshot') or []}", flush=True)
        print(f"   [dry-run] never: {card.get('never')}", flush=True)
        return False  # nothing written

    _write_card(owner_id, card, name)
    return True


def _write_card(owner_id: str, card: dict, name: str) -> None:
    db = get_supabase()
    current = (
        db.table("core_config")
        .select("content")
        .eq("owner_id", owner_id)
        .eq("key", _PERSONA_KEY)
        .limit(1)
        .execute()
        .data
    )
    prev_content = current[0]["content"] if current else None
    try:
        prev_gen = 0
        if prev_content:
            prev = json.loads(prev_content) if isinstance(prev_content, str) else prev_content
            prev_gen = int((prev or {}).get("generation") or 0)
    except Exception:
        prev_gen = 0
    card["generation"] = prev_gen + 1

    content = json.dumps(card, ensure_ascii=False)

    # Versioning: previous card parked for rollback (--restore). Skipped
    # when there was no previous card, so restore can never resurrect
    # "null".
    if prev_content is not None:
        db.table("core_config").upsert(
            {"owner_id": owner_id, "key": _PERSONA_PREV_KEY, "content": prev_content},
            on_conflict="owner_id,key",
        ).execute()
    db.table("core_config").upsert(
        {"owner_id": owner_id, "key": _PERSONA_KEY, "content": content},
        on_conflict="owner_id,key",
    ).execute()

    # Self-verify write.
    check = (
        db.table("core_config")
        .select("content")
        .eq("owner_id", owner_id)
        .eq("key", _PERSONA_KEY)
        .limit(1)
        .execute()
        .data
    )
    got = check[0]["content"] if check else None
    if got != content:
        raise RuntimeError(f"verification failed for {owner_id}: write mismatch")

    clear_persona_cache(owner_id)
    audit_log_sync(
        "persona", "INFO", f"card v{card['generation']} written for {owner_id}",
        {"fingerprint": card["source_fingerprint"]},
    )
    print(f"✅ {name}: card v{card['generation']} written + verified.", flush=True)


def _restore(owner_id: str) -> bool:
    db = get_supabase()
    rows = (
        db.table("core_config")
        .select("content")
        .eq("owner_id", owner_id)
        .eq("key", _PERSONA_PREV_KEY)
        .limit(1)
        .execute()
        .data
    )
    if not rows:
        print(f"⏭️  {owner_id[:8]}: no previous card to restore.")
        return False
    prev = rows[0]["content"]
    try:
        parsed = json.loads(prev) if isinstance(prev, str) else prev
        if not isinstance(parsed, dict):
            print(f"⏭️  {owner_id[:8]}: stored previous card invalid — not restoring.")
            return False
    except Exception:
        print(f"⏭️  {owner_id[:8]}: stored previous card unparseable — not restoring.")
        return False
    db.table("core_config").upsert(
        {"owner_id": owner_id, "key": _PERSONA_KEY, "content": prev},
        on_conflict="owner_id,key",
    ).execute()
    clear_persona_cache(owner_id)
    audit_log_sync("persona", "INFO", f"card restored for {owner_id}")
    print(f"✅ {owner_id[:8]}: previous card restored.")
    return True


def _resolve_owner_id(display_name: str) -> str:
    rows = (
        get_supabase().table("users").select("id").eq("name", display_name).limit(1).execute().data
    )
    if not rows:
        raise SystemExit(f"❌ No user named '{display_name}'")
    return rows[0]["id"]


async def _fanout(dry_run: bool, restore: bool) -> list[bool]:
    async def _per_tenant() -> bool:
        from core.services.user_settings import current_user_id

        uid = current_user_id()
        if not uid:
            return False
        if restore:
            return _restore(uid)
        return synthesize_tenant(uid, dry_run=dry_run)

    return await arun_tenant_fanout(_per_tenant, job_name="persona")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--user", default=None, help="Run for one tenant by display name")
    parser.add_argument("--dry-run", action="store_true", help="Preview the card, write nothing")
    parser.add_argument("--restore", action="store_true", help="Restore previous card from persona_prev")
    args = parser.parse_args()

    if args.user:
        uid = _resolve_owner_id(args.user)
        with tenant_scope(uid):  # per-tenant resolvers need the scope (root label)
            ok = _restore(uid) if args.restore else synthesize_tenant(uid, dry_run=args.dry_run)
        raise SystemExit(0 if ok else 1)
    asyncio.run(_fanout(dry_run=args.dry_run, restore=args.restore))


if __name__ == "__main__":
    main()
