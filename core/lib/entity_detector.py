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

from core.services.db import get_supabase


@dataclass
class DetectedEntity:
    """A single entity detected in text."""
    label: str
    type: str  # person, organization, project, place, event, animal, emotional_state
    source: str  # 'db_lookup' or 'pattern_match'
    db_id: Optional[str] = None  # ID from DB if found in Phase 1
    is_new: bool = False  # True if not found in DB (pattern-matched)
    confidence: float = 1.0


# Words stripped from project label candidates
_PROJECT_NOISE_WORDS = {
    'project', 'website', 'web', 'app', 'application', 'platform',
    'page', 'site', 'service', 'system', 'product',
    'tool', 'task', 'note', 'portal', 'dashboard', 'panel',
    'hub', 'suite', 'manager', 'management', 'tracker',
}

# Words that signal a following capitalized name is a person reference
_PERSON_CONTEXT_WORDS = {
    'with', 'from', 'by', 'to', 'for', 'talked', 'spoke', 'met',
    'called', 'asked', 'told', 'said', 'introduced', 'worked',
    'discussed', 'interviewed', 'contacted', 'assigned',
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
    _SKIP_WORDS = {'i', 'a', 'an', 'the', 'this', 'that', 'these', 'those',
                   'my', 'your', 'his', 'her', 'its', 'our', 'their',
                   'danny', 'mother', 'we', 'he', 'she', 'it', 'they'}
    pattern = r'\b([A-Z][a-z]*(?:\s+[A-Z][a-z]*)*)\b'
    matches = []
    for m in re.finditer(pattern, text):
        phrase = m.group(1)
        if phrase.lower() not in _SKIP_WORDS:
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

    supabase = get_supabase()
    entities: List[DetectedEntity] = []
    seen_labels: set = set()

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
            .select('label, type, id') \
            .in_('type', ['person', 'organization', 'project', 'place',
                          'event', 'animal', 'emotional_state']) \
            .neq('epistemic_status', 'hypothetical') \
            .eq('is_current', True) \
            .execute()
        graph_nodes = gn_res.data or []

        orgs_res = supabase.table('organizations').select('id, name').execute()
        orgs = orgs_res.data or []

        projs_res = supabase.table('projects') \
            .select('id, name, organization_id') \
            .eq('is_current', True) \
            .execute()
        projs = projs_res.data or []

        people_res = supabase.table('people') \
            .select('id, name') \
            .eq('is_current', True) \
            .execute()
        people = people_res.data or []

    except Exception as e:
        audit_log_sync("entity_detector", "WARNING", f"Phase 1 DB fetch failed: {e}")
        graph_nodes = []
        orgs = []
        projs = []
        people = []

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
            _add(DetectedEntity(
                label=node['label'],
                type=node['type'],
                source='db_lookup',
                db_id=node['id'],
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

    # Match projects by name
    for proj in projs:
        norm_name = _normalize(proj['name'])
        if norm_name in text_ngrams:
            _add(DetectedEntity(
                label=proj['name'],
                type='project',
                source='db_lookup',
                db_id=str(proj['id']),
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

    text_lower = text.lower()

    # ── Pattern A: "[Known Org] [Descriptor] project/app/platform" ──
    org_labels = [e.label for e in entities if e.type == 'organization']
    for org_label in org_labels:
        org_lower = org_label.lower()
        org_idx = text_lower.find(org_lower)
        if org_idx < 0:
            continue
        after_org = text[org_idx + len(org_label):]
        # Look for a noise word after the org
        for noise_word in sorted(_PROJECT_NOISE_WORDS, key=len, reverse=True):
            noise_match = re.search(
                r'\b' + re.escape(noise_word) + r'\b',
                after_org, re.IGNORECASE
            )
            if noise_match:
                between = after_org[:noise_match.start()].strip()
                if between:
                    # Title-case the descriptor words
                    descriptor = between.title().strip()
                    project_label = f"{org_label} {descriptor}"
                    project_key = project_label.lower()
                    if project_key not in seen_labels:
                        _add(DetectedEntity(
                            label=project_label,
                            type='project',
                            source='pattern_match',
                            is_new=True,
                            confidence=0.9,
                        ))
                        audit_log_sync("entity_detector", "INFO",
                            f"Pattern A: Proposed project '{project_label}' from "
                            f"'{org_label}' + '{descriptor}'")
                break  # Only use first noise word found

    # ── Pattern B: Person detection via capitalized names in context ──
    caps_phrases = _find_capitalized_phrases(text)
    for phrase, start, end in caps_phrases:
        if phrase.lower() in seen_labels:
            continue
        # Check if preceded by context words
        before = text[max(0, start - 25):start].strip().lower()
        ctx_words = before.split()
        if ctx_words and ctx_words[-1] in _PERSON_CONTEXT_WORDS:
            _add(DetectedEntity(
                label=phrase,
                type='person',
                source='pattern_match',
                is_new=True,
                confidence=0.8,
            ))
            audit_log_sync("entity_detector", "INFO",
                f"Pattern B: Proposed person '{phrase}' via context")

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

    return entities


# ── Convenience: Get org/project IDs from text ───────────────────────────────

def resolve_org_and_project(text: str
                           ) -> Tuple[Optional[str], Optional[int], str]:
    """Convenience: returns (organization_id, project_id, reason).

    Thin wrapper around detect_entities for callers that only need
    org/project ID resolution (replaces entity_resolver.resolve_entities_from_text).
    """
    entities = detect_entities(text)
    org_id = None
    proj_id = None
    reason_parts = []

    for e in entities:
        if e.type == 'organization' and e.db_id:
            if not org_id:
                org_id = e.db_id
                reason_parts.append(f"org_exact_match({e.label})")
            elif e.db_id != org_id:
                reason_parts.append("org_ambiguous")
                org_id = None
        elif e.type == 'project' and e.db_id:
            if not proj_id:
                proj_id = int(e.db_id)
                reason_parts.append(f"proj_exact_match({e.label})")
            elif int(e.db_id) != proj_id:
                reason_parts.append("proj_ambiguous")

    # Infer project's org if project found but no org yet
    if proj_id and not org_id:
        try:
            supabase = get_supabase()
            proj_res = supabase.table('projects') \
                .select('organization_id') \
                .eq('id', proj_id) \
                .limit(1) \
                .execute()
            if proj_res.data and proj_res.data[0].get('organization_id'):
                org_id = proj_res.data[0]['organization_id']
                reason_parts.append("org_inferred_from_proj")
        except Exception:
            pass

    reason = " | ".join(reason_parts) if reason_parts else "no_matches"
    return org_id, proj_id, reason
