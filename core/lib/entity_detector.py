"""Deterministic Entity Detector — no LLM.

Three-phase entity detection that replaces the old LLM-based entity extraction:
1. DB Lookup — match text against graph_nodes, people, orgs, projects via n-grams
2. Pattern Match — detect unregistered entities using structural text patterns
3. Output — returns detected entities with types and DB IDs where found

Called by both Layer 2 (Processing, sync at creation time) and Layer 3
(Intelligence, async in enrichment queue). Single source of truth for
entity detection — no prompt drift, no LLM bias, no examples to maintain.

Replaces:
  - entity_extractor.py LLM extraction (Layer 3)
  - entity_linker.py + entity_resolver.py n-gram matching (Layer 2)
  - backfill_graph.py LLM extraction (Layer 3)
"""

from dataclasses import dataclass
from typing import List, Optional, Tuple
import re

from core.services.db import tenant_aware_client


@dataclass
class DetectedEntity:
    """A single entity detected in text."""
    label: str
    type: str  # person, organization, place, event, animal, emotional_state
    source: str  # 'db_lookup' or 'pattern_match'
    db_id: Optional[str] = None  # ID from DB if found in Phase 1
    is_new: bool = False  # True if not found in DB (pattern-matched)
    confidence: float = 1.0


# Words that signal a following capitalized name is a person reference.
# IMPORTANT: Prepositions ('with', 'from', 'by', 'to', 'for') were REMOVED —
# they precede ANY capitalized word ("scheduled for Friday"), which caused
# weekdays, months, and time words to be proposed as people. Only true
# person-signal verbs remain. Because a preposition can sit between the verb
# and the name ("met with Joel", "talked to Arani"), patterns scan a 3-word
# window before the capitalized phrase (see detect_entities Pattern B).
_PERSON_CONTEXT_WORDS = {
    'talked', 'spoke', 'met', 'called', 'asked', 'told', 'said',
    'introduced', 'worked', 'discussed', 'interviewed', 'contacted',
    'assigned',
    # Aug 26: base forms + variants — "Meet Kavya Raman" previously missed
    # because only irregular 'met' was listed (round-1 batch UAT finding).
    'meet', 'call', 'email', 'message', 'ping', 'sync',
}


def _signal_base_form(word: str) -> str:
    """Crude morphological normalization for signal-word matching.

    'meeting' → 'meet', 'calls' → 'call'. Irregulars like 'met' are listed
    explicitly in the context-word sets. Only strips when the result stays
    a plausible word (min length 3).
    """
    if len(word) > 4 and word.endswith('ing'):
        return word[:-3]
    if len(word) > 3 and word.endswith('s'):
        return word[:-1]
    return word

# How many words before a capitalized phrase to scan for a signal word.
# Small enough to avoid cross-clause false positives ("the meeting on Friday"
# has no verb in its window), large enough to bridge "met with Joel".
_SIGNAL_WINDOW = 3

# Words that can NEVER become entities — weekdays, months, and time references.
# Pattern B (person) and Pattern D (organization) used to propose these as
# entities whenever they followed a context word ("meeting scheduled for Friday"
# → "Friday" became a person node). Filtered at the candidate source so both
# patterns are protected.
_RESERVED_ENTITY_WORDS = {
    # Weekdays
    'monday', 'tuesday', 'wednesday', 'thursday', 'friday',
    'saturday', 'sunday',
    # Months
    'january', 'february', 'march', 'april', 'may', 'june', 'july',
    'august', 'september', 'october', 'november', 'december',
    # Time-of-day / relative time
    'morning', 'afternoon', 'evening', 'night', 'noon', 'midnight',
    'today', 'tomorrow', 'yesterday', 'tonight',
    'week', 'weekend', 'month', 'year',
}

# Words that signal a following capitalized name is an organization reference
# Mirrors _PERSON_CONTEXT_WORDS pattern. Catches patterns like:
#   "new client Marutham" → "client" before "Marutham"
#   "our vendor Acme"    → "vendor" before "Acme"
#   "the company Quanta" → "company" before "Quanta"
# Strong corporate suffixes — the gate for re-enabled new-org proposals
# (Pattern D2, Aug 26). A phrase must END in one of these to be proposed.
_ORG_SUFFIX_LEXICON = {
    'labs', 'media', 'dynamics', 'group', 'bank', 'hotels', 'analytics',
    'studios', 'systems', 'partners', 'ventures', 'technologies',
    'solutions', 'works', 'industries', 'capital', 'digital', 'software',
    'consulting', 'holdings', 'networks', 'logistics', 'bio', 'pharma',
}
_ORG_CONTEXT_WORDS = {
    'client', 'company', 'startup', 'start-up', 'agency', 'firm',
    'vendor', 'partner', 'organization', 'organisation', 'business',
    'enterprise', 'outfit', 'shop', 'studio', 'group', 'corporation',
    'corp', 'inc', 'llc', 'ltd', 'limited',
}

# Common English words that must NEVER become organization entities, even when
# they follow an org-context word. Pattern D over-triggered on ordinary words
# like "Crayon discussed company evolution" → "Evolution" (organization) in
# the Aug 6 backfill batch (Great, Now, Praying, Structure, Evolution, Business
# were all proposed as orgs). Real orgs (Qhord, Marutham, Solvstrat) are proper
# nouns and are NOT in this set — the guard is deliberately a small curated
# list of ordinary vocabulary, mirroring _EMOTIONAL_STATES and
# _RESERVED_ENTITY_WORDS.
_COMMON_ORG_WORDS = {
    'business', 'company', 'group', 'team', 'teams', 'startup', 'agency',
    'firm', 'vendor', 'partner', 'client', 'project', 'mission', 'plan',
    'plans', 'goal', 'goals', 'work', 'works', 'job', 'role', 'career',
    'meeting', 'meetings', 'call', 'calls', 'event', 'events', 'board',
    'committee', 'council', 'church', 'school', 'college', 'university',
    'ministry', 'family', 'community', 'home', 'office', 'market',
    'marketing', 'sales', 'finance', 'accounting', 'operations', 'system',
    'systems', 'platform', 'platforms', 'product', 'products', 'service',
    'services', 'solution', 'solutions', 'technology', 'software', 'data',
    'network', 'networks', 'industry', 'industries', 'department', 'division',
    'unit', 'section', 'branch', 'store', 'shop', 'studio', 'agency', 'firm',
    'foundation', 'institute', 'academy', 'center', 'centre', 'program',
    'programs', 'project', 'initiative', 'initiatives', 'task', 'tasks',
    'report', 'reports', 'document', 'documents', 'review', 'reviews',
    'game', 'games', 'app', 'apps', 'website', 'site', 'portal', 'portal',
    'dashboard', 'workspace', 'organization', 'organisation', 'enterprise',
    'corporation', 'outfit', 'operation', 'structure', 'evolution', 'now',
    'great', 'praying', 'happy', 'sad', 'angry', 'excited', 'inspired',
    'exhausted', 'tired', 'depressed', 'frustrated', 'betrayed', 'broken',
    'lost', 'grateful', 'stress', 'anxiety', 'anxious', 'overwhelmed',
    'burnt out', 'burned out', 'desperate', 'lonely', 'hopeless', 'helpless',
    'ashamed', 'guilty', 'crushed', 'grief', 'fear', 'worried', 'nervous',
    'confused', 'motivated', 'confident', 'hopeful', 'weekend', 'morning',
    'afternoon', 'evening', 'night', 'today', 'tomorrow', 'yesterday',
    'week', 'month', 'year', 'spring', 'summer', 'fall', 'winter',
}

# Known emotional state words
_EMOTIONAL_STATES = {
    'stressed', 'excited', 'overwhelmed', 'anxious', 'worried',
    'happy', 'sad', 'angry', 'frustrated', 'hopeful', 'tired',
    'exhausted', 'depressed', 'grateful', 'confident', 'nervous',
    'confused', 'motivated', 'inspired', 'burned out', 'burnt out',
    'desperate', 'lonely', 'hopeless', 'helpless', 'ashamed',
    'guilty', 'betrayed', 'crushed', 'broken', 'lost', 'grief',
}


# ── Helpers ──────────────────────────────────────────────────────────────────

def _normalize(s: str) -> str:
    if not s:
        return ""
    s = re.sub(r'[^\w\s]', ' ', s.lower())
    return ' '.join(s.split())


def _get_ngrams(words: list[str], n: int) -> set[str]:
    ngrams = set()
    for i in range(len(words) - n + 1):
        ngrams.add(' '.join(words[i:i+n]))
    return ngrams


def _find_capitalized_phrases(text: str) -> list[tuple[str, int, int]]:
    """Find capitalized phrases in text. Returns [(phrase, start, end)]."""
    # M17: the user's OWN display name + root label are skipped (a tenant's
    # self-references are not entities) — resolved per-tenant instead of a
    # hardcoded 'danny' literal. 'mother' stays: it is a generic reference.
    _SKIP_WORDS = {'i', 'a', 'an', 'the', 'this', 'that', 'these', 'those',
                   'my', 'your', 'his', 'her', 'its', 'our', 'their',
                   'mother', 'we', 'he', 'she', 'it', 'they'}
    try:
        from core.services.user_settings import resolve_user_name
        from core.lib.graph_rules import resolve_root_label
        _self_name = (resolve_user_name() or "").strip().lower()
        _root_name = (resolve_root_label() or "").strip().lower()
        if _self_name:
            _SKIP_WORDS.add(_self_name)
        if _root_name and _root_name not in _SKIP_WORDS:
            _SKIP_WORDS.add(_root_name)
    except Exception:
        pass  # fail-open: no tenant context → legacy behavior
    pattern = r'\b([A-Z][a-z]*(?:\s+[A-Z][a-z]*)*)\b'
    matches = []
    for m in re.finditer(pattern, text):
        phrase = m.group(1)
        phrase_lower = phrase.lower()
        if phrase_lower in _SKIP_WORDS:
            continue
        # Never propose reserved words (weekdays, months, time refs) as entities
        if any(w in _RESERVED_ENTITY_WORDS for w in phrase_lower.split()):
            continue
        matches.append((phrase, m.start(), m.end()))
    return matches


def _match_emotional_states(text: str) -> list[str]:
    """Match emotional states in text, handling multi-word states like 'burned out'.

    Checks both single words and adjacent word pairs against _EMOTIONAL_STATES.
    Returns matched labels, deduplicated, preserving original casing.
    """
    words = text.split()
    matches = []
    seen = set()
    for i, word in enumerate(words):
        # Single word check
        clean = word.strip('.,!?;:()[]{}"\'').lower()
        if clean in _EMOTIONAL_STATES and clean not in seen:
            seen.add(clean)
            matches.append(word.strip('.,!?;:()[]{}"\'').capitalize())
        # Two-word check
        if i < len(words) - 1:
            pair = word.strip('.,!?;:()[]{}"\'') + ' ' + words[i+1].strip('.,!?;:()[]{}"\'')
            pair_lower = pair.lower()
            if pair_lower in _EMOTIONAL_STATES and pair_lower not in seen:
                seen.add(pair_lower)
                # Title case the pair for the label
                matches.append(pair.title())
    return matches


def _is_url_text(text: str) -> bool:
    """Check if text is primarily a URL."""
    return bool(re.match(r'^https?://\S+$', text.strip()))


# ── Main Function ────────────────────────────────────────────────────────────

def detect_entities(text: str) -> List[DetectedEntity]:
    """Three-phase deterministic entity detection. No LLM.

    Phase 1: DB Lookup — match text against known entities via n-grams
    Phase 2: Pattern Match — find unregistered entities via structural patterns

    Returns a deduplicated list of DetectedEntity objects.
    """
    from core.lib.audit_logger import audit_log_sync

    # Skip URL-only text
    if _is_url_text(text):
        return []

    supabase = tenant_aware_client()
    entities: List[DetectedEntity] = []
    seen_labels: set = set()

    # Hardening (Aug 6 root cause): if Phase 1 DB lookup fails, detection is
    # UNGROUNDED — patterns would otherwise run at full confidence against an
    # empty known-entity set, producing the mislabel batch (Blessy→concept,
    # 'company evolution'→Evolution org). When the DB is down we degrade:
    #   - org proposals (Pattern D) are disabled entirely
    #   - person proposals (Pattern B) are capped at low confidence
    #   - one audit event marks the degraded run
    db_grounded = True

    def _add(e: DetectedEntity):
        key = e.label.lower().strip()
        if key and key not in seen_labels:
            seen_labels.add(key)
            entities.append(e)

    # ════════════════════════════════════════════════════════════════════════
    # Phase 1: DB Lookup
    # ════════════════════════════════════════════════════════════════════════

    try:
        # Fetch all known entities
        gn_res = supabase.table('graph_nodes') \
            .select('label, type, id, db_record_id') \
            .in_('type', ['person', 'organization', 'place',
                          'event', 'animal', 'emotional_state']) \
            .neq('epistemic_status', 'hypothetical') \
            .eq('is_current', True) \
            .execute()
        graph_nodes = gn_res.data or []

        # Consolidation (migration 74): orgs/people come from live graph nodes.
        # db ids below are the LEGACY domain-table ids (orgs: organizations.id,
        # people: people.id) preserved in node metadata — memories FKs depend on them.
        orgs_res = supabase.table('graph_nodes') \
            .select('id, label, metadata, db_record_id') \
            .eq('type', 'organization') \
            .eq('is_current', True) \
            .execute()
        import json as _json
        orgs = []
        for _n in orgs_res.data or []:
            _meta = _n.get('metadata') or {}
            if isinstance(_meta, str):
                try:
                    _meta = _json.loads(_meta)
                except Exception:
                    _meta = {}
            # Graph-first (migration 75): the org id IS the graph node id
            orgs.append({'name': _n.get('label'), 'id': _n.get('id')})
        orgs = [o for o in orgs if o.get('id')]

        people_res = supabase.table('graph_nodes') \
            .select('id, label, metadata, db_record_id') \
            .eq('type', 'person') \
            .eq('is_current', True) \
            .execute()
        people = []
        for _n in people_res.data or []:
            _meta = _n.get('metadata') or {}
            if isinstance(_meta, str):
                try:
                    _meta = _json.loads(_meta)
                except Exception:
                    _meta = {}
            # Graph-first (migration 75): the person id IS the graph node id
            people.append({'name': _n.get('label'), 'id': _n.get('id')})
        people = [p for p in people if p.get('id')]

    except Exception as e:
        audit_log_sync("entity_detector", "WARNING", f"Phase 1 DB fetch failed: {e}")
        graph_nodes = []
        orgs = []
        people = []
        db_grounded = False

    # Build n-gram index from text
    norm_text = _normalize(text)
    words = norm_text.split()
    text_ngrams: set = set()
    for i in range(1, 5):
        text_ngrams.update(_get_ngrams(words, i))

    # Match graph nodes
    for node in graph_nodes:
        norm_label = _normalize(node['label'])
        if norm_label in text_ngrams:
            # Use db_record_id (domain table PK) instead of node['id']
            # (graph_nodes UUID) — memories.organization_id/project_id FK to
            # domain tables, not graph_nodes. All three types have
            # db_record_id set via create_graph_node_with_db_record.
            if node['type'] in ('organization', 'person') and node.get('db_record_id'):
                db_id_val = str(node['db_record_id'])
            else:
                db_id_val = node['id']
            _add(DetectedEntity(
                label=node['label'],
                type=node['type'],
                source='db_lookup',
                db_id=db_id_val,
                is_new=False,
            ))

    # Match organizations by name
    for org in orgs:
        norm_name = _normalize(org['name'])
        if norm_name in text_ngrams:
            _add(DetectedEntity(
                label=org['name'],
                type='organization',
                source='db_lookup',
                db_id=str(org['id']),
                is_new=False,
            ))

    # Match people by name
    for p in people:
        norm_name = _normalize(p['name'])
        if norm_name in text_ngrams:
            _add(DetectedEntity(
                label=p['name'],
                type='person',
                source='db_lookup',
                db_id=str(p['id']),
                is_new=False,
            ))

    # ════════════════════════════════════════════════════════════════════════
    # Phase 2: Pattern Match — detect unregistered entities
    # ════════════════════════════════════════════════════════════════════════

    caps_phrases = _find_capitalized_phrases(text)

    # ── Pattern D2: NEW-organization detection via suffix lexicon (Aug 26) ──
    # Runs BEFORE Pattern B so org-suffix phrases are claimed as organizations
    # first ("Nova Dynamics" was incorrectly typed person by B in round 2).
    _d2_claimed: set[str] = set()
    for phrase, start, end in caps_phrases:
        if phrase.lower() in seen_labels:
            continue
        words = phrase.split()
        if len(words) >= 2 and words[-1].lower() in _ORG_SUFFIX_LEXICON:
            conf = 0.8 if db_grounded else 0.4
            _add(DetectedEntity(
                label=phrase,
                type='organization',
                source='pattern_match',
                is_new=True,
                confidence=conf,
            ))
            _d2_claimed.add(phrase.lower())
            audit_log_sync("entity_detector", "INFO",
                f"Pattern D2: Proposed organization '{phrase}' via suffix gate")

    # ── Pattern B: Person detection via capitalized names in context ──
    for phrase, start, end in caps_phrases:
        if phrase.lower() in seen_labels or phrase.lower() in _d2_claimed:
            continue
        # Hardened Aug 26: sentence-initial verbs ("Meet", "Call") get absorbed
        # into the phrase by _find_capitalized_phrases. Strip leading signal
        # words so the real name becomes the candidate label and the signal is
        # known to have fired (round-1 UAT: "Meet Kavya Raman" = empty
        # phrase = nothing, because "Meet" consumed the window).
        words = phrase.split()
        signal_in_phrase = False
        while len(words) > 1:
            w0 = words[0].lower()
            if w0 in _PERSON_CONTEXT_WORDS or _signal_base_form(w0) in _PERSON_CONTEXT_WORDS:
                signal_in_phrase = True
                words = words[1:]
            else:
                break
        label = ' '.join(words)
        if label.lower() in seen_labels or label.lower() in _d2_claimed:
            continue
        # Standard window check for non-stripped phrases
        if not signal_in_phrase:
            before = text[max(0, start - 25):start].strip().lower()
            ctx_words = before.split()
            if not ctx_words or not any(
                w in _PERSON_CONTEXT_WORDS
                or _signal_base_form(w) in _PERSON_CONTEXT_WORDS
                for w in ctx_words[-_SIGNAL_WINDOW:]
            ):
                continue
        conf = 0.8 if db_grounded else 0.4
        _add(DetectedEntity(
            label=label,
            type='person',
            source='pattern_match',
            is_new=True,
            confidence=conf,
        ))
        audit_log_sync("entity_detector", "INFO",
            f"Pattern B: Proposed person '{label}' via context")

    # Degraded mode marker
    if not db_grounded:
        audit_log_sync("entity_detector", "WARNING",
            "DEGRADED MODE: Phase 1 DB fetch failed — ungrounded detection "
            "(Pattern B capped at low confidence)")

    # ── Pattern C: Emotional state detection (handles multi-word like 'burned out') ──
    for emotion in _match_emotional_states(text):
        if emotion.lower() not in seen_labels:
            _add(DetectedEntity(
                label=emotion,
                type='emotional_state',
                source='pattern_match',
                is_new=True,
                confidence=0.9,
            ))

    # ── Pattern D: Organization detection via capitalized names in context ──
    # Same mechanism as Pattern B but with _ORG_CONTEXT_WORDS.
    # Runs AFTER Pattern B/C so person detections are already in _seen_labels
    # and won't be re-caught as orgs.
    # Catches unregistered organizations introduced by context words like:
    #   "A new client Marutham..."        → "client" → organization
    #   "They signed with vendor Acme..." → "vendor" → organization
    #
    # Hardening (Aug 6 root cause):
    #   - Disabled entirely when Phase 1 DB lookup failed (ungrounded org
    #     proposals were the mislabel source).
    #   - _COMMON_ORG_WORDS guard: ordinary vocabulary ("company evolution" →
    #     "Evolution") can never become an organization.
    #   - Window tightened to the last 2 words (was 3) to cut cross-clause hits
    #     — the org-context word must be nearly adjacent to the candidate.
    if db_grounded:
        caps_phrases = _find_capitalized_phrases(text)
        for phrase, start, end in caps_phrases:
            if phrase.lower() in seen_labels:
                continue
            # Never propose ordinary English vocabulary as organizations.
            if phrase.lower() in _COMMON_ORG_WORDS:
                continue
            # Check if preceded by an organization context word within a small window.
            before = text[max(0, start - 25):start].strip().lower()
            ctx_words = before.split()
            if ctx_words and any(
                w in _ORG_CONTEXT_WORDS for w in ctx_words[-2:]
            ):
                _add(DetectedEntity(
                    label=phrase,
                    type='organization',
                    source='pattern_match',
                    is_new=True,
                    confidence=0.8,
                ))
                audit_log_sync("entity_detector", "INFO",
                    f"Pattern D: Proposed organization '{phrase}' via context")

    return entities


# ── Convenience: Get org/project IDs from text ───────────────────────────────

def resolve_org_and_project(text: str
                           ) -> Tuple[Optional[str], Optional[int], str]:
    """Convenience: returns (organization_id, project_id, reason).

    Thin wrapper around detect_entities for callers that only need
    org ID resolution (replaces entity_resolver.resolve_entities_from_text).
    Note: project_id is always None (projects table decommissioned) —
    returned for backward compat with existing callers.
    """
    entities = detect_entities(text)
    org_id = None
    reason_parts = []

    for e in entities:
        if e.type == 'organization' and e.db_id:
            if not org_id:
                org_id = e.db_id
                reason_parts.append(f"org_exact_match({e.label})")
            elif e.db_id != org_id:
                reason_parts.append("org_ambiguous")
                org_id = None
    reason = " | ".join(reason_parts) if reason_parts else "no_matches"
    return org_id, None, reason
