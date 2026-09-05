"""Entity Context — prerequisite step for guaranteed org linkage.

Every task, note, message, and person must be linked to an org.
This module extracts entity context from source text BEFORE creation,
so the creation function receives full entity context and can link immediately.

Architecture:
    Webhook/API receives message
      → extract_context_from_source(full_text)   [deterministic + LLM]
      → returns EntityContext {org_id, pending_org_id, persons, org_edges}
      → create_task_direct(title, entity_context=ctx)
      → create_note_direct(content, entity_context=ctx)

The LLM pass ALWAYS runs (not conditional on deterministic results).
It detects implicit orgs and determines which org is primary.
"""

from dataclasses import dataclass, field, asdict
from typing import Optional
import json
import logging
import re

from core.lib.audit_logger import audit_log_sync

logger = logging.getLogger(__name__)


@dataclass
class EntityContext:
    """Entity context extracted from a source message BEFORE task/note creation.

    Built by extract_context_from_source(). Passed to creation functions.
    Replaces the ad-hoc org/person resolution scattered across tools.py.

    Exactly one of organization_id / pending_org_id will be set (or both NULL
    if no org found). organization_id takes precedence (existing org wins).
    """
    # Org linkage (mutually exclusive in practice — existing wins over pending)
    organization_id: Optional[str] = None       # existing org (graph_nodes.id UUID)
    organization_name: Optional[str] = None
    pending_org_id: Optional[int] = None        # new org (pending_nodes.id BIGINT)
    pending_org_label: Optional[str] = None

    # Persons found in the same source
    person_ids: list = field(default_factory=list)        # graph_nodes.id UUIDs (existing)
    person_names: list = field(default_factory=list)
    pending_person_ids: list = field(default_factory=list)  # pending_nodes.id (new)

    # Org-to-org edges proposed from the same source
    org_to_org_edges: list = field(default_factory=list)
    # [{source_label, target_label, relationship}]
    org_to_org_edge_labels: list = field(default_factory=list)
    # List of {type, label, confidence, existing_matches}
    detected_entities: list = field(default_factory=list)
    # Secondary org labels detected by LLM (for edge proposal)

    # Source tracking
    source_text: str = ""
    extraction_method: str = ""  # "deterministic", "llm", "hybrid"
    extraction_timing: str = ""  # "sync", "async", "card"

    # Provenance for any pending rows this context queues (origin_table /
    # origin_id) — lets every pending node be traced to its source message /
    # raw_dump / run. Optional: when absent, queue_pending_candidates emits a
    # WARNING so untraceable-ghost rows are visible for cleanup.
    pending_provenance: Optional[dict] = None

    def is_empty(self) -> bool:
        return (self.organization_id is None and self.pending_org_id is None
                and not self.person_ids and not self.pending_person_ids)

    def primary_org_id(self) -> Optional[str]:
        """The org UUID to stamp on the task/note. Prefers existing over pending."""
        return self.organization_id

    def primary_pending_org_id(self) -> Optional[int]:
        """Fallback if no existing org. The pending node BIGINT id."""
        return self.pending_org_id

    def to_dict(self) -> dict:
        """Serialize to a plain dict for JSON storage (enrichment queue, workflows)."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "EntityContext":
        """Deserialize from a dict (e.g., from JSONB column or workflow payload)."""
        if not data:
            return cls()
        return cls(
            organization_id=data.get("organization_id"),
            organization_name=data.get("organization_name"),
            pending_org_id=data.get("pending_org_id"),
            pending_org_label=data.get("pending_org_label"),
            pending_provenance=data.get("pending_provenance"),
            person_ids=data.get("person_ids") or [],
            person_names=data.get("person_names") or [],
            pending_person_ids=data.get("pending_person_ids") or [],
            org_to_org_edges=data.get("org_to_org_edges") or [],
            org_to_org_edge_labels=data.get("org_to_org_edge_labels") or [],
            detected_entities=data.get("detected_entities") or [],
            source_text=data.get("source_text") or "",
            extraction_method=data.get("extraction_method") or "",
            extraction_timing=data.get("extraction_timing") or "",
        )


# ── Evidence gate: common words / generic patterns / label quality ──────────
#
# The evidence gate is NOT a growing blacklist of past failures. There is a
# SMALL bounded set of common words that, when they appear as a single token
# in a person/org candidate, are rejected as clearly-non-entity (these are the
# core of known junk labels like "Please", "Chief", "Staff"). Everything else
# is gated by PATTERNS and heuristics that generalize instead of enumerating:
#
#   1. Generic-pattern rejection — single-word meta-categories (news/media/
#      update/status/...), discourse markers and common verbs (please/sure/
#      hey/what/you/...), and single letters are never entities.
#   2. Label-quality heuristics — multi-word proper-noun-ish labels and all-caps
#      abbreviations are evidence-POSITIVE signals.
#   3. LLM-only minimum evidence — a person/org candidate that only the LLM
#      phase produced (no deterministic signal, not a known name) must clear an
#      evidence bar before it may become a pending row.
#
# If a NEW class of junk appears that isn't caught, the fix is to tighten the
# evidence logic (add a heuristic, raise the LLM-evidence bar, add a generic
# pattern), NOT to add another word to a list.
_COMMON_WORDS: frozenset = frozenset({
    "please", "chief", "staff",
})

_GENERIC_PATTERNS = [
    # generic single-word categories / meta-labels
    re.compile(r"^\s*(news|media|info|information|update|status|report|summary"
               r"|feedback|note|comment|remark|message|chat|text|input|data"
               r"|file|document|attachment|link|url|website|page|entry"
               r"|task|note|meeting|call|email|thread|conversation|session"
               r"|event|reminder|request|command|phrase|idea|concept|issue"
               r"|question|reply|follow|review|submit|pay|clean|finish|start"
               r"|stop|open|close|send|get|make|give|take|known|unknown)\s*$", re.I),
    # sentence fragments / discourse markers / common verbs that should never be
    # a person or org label (catches "Please", "Sure", "Ok", "Yes", "No",
    # "What", "You", "Why", "How", "Hey", "Hi", "Thanks", etc. as a label)
    re.compile(r"^\s*(please|sure|ok|okay|yes|no|yeah|nope|maybe|possibly"
               r"|absolutely|definitely|certainly|probably|hopefully|wow|omg"
               r"|oops|ugh|aha|oh|hey|hi|hello|thanks|thank|yep|nah|sure)\s*$", re.I),
    # single lowercase letter — never an entity
    re.compile(r"^\s*[a-z]\s*$", re.I),
]


def _label_is_common_word(label: str) -> bool:
    """One evidence-gate signal: single token that is a common word."""
    t = label.strip()
    if not t or " " in t:
        return False
    return t.lower() in _COMMON_WORDS


def _label_matches_generic_pattern(label: str) -> bool:
    """One evidence-gate signal: label matches a clearly-generic/nonsensical pattern."""
    t = label.strip()
    if not t:
        return False
    for pat in _GENERIC_PATTERNS:
        if pat.fullmatch(t):
            return True
    return False


def _label_is_all_caps_abbreviation(label: str) -> bool:
    """An evidence-POSITIVE signal: all-caps abbreviation (e.g. DBS, AWS, ICP)."""
    t = label.strip()
    if not t or " " in t or not t.isupper():
        return False
    return len(t) >= 2


def _label_has_capitalization_heuristic(label: str) -> bool:
    """An evidence-POSITIVE signal: multi-word label with normal proper-noun-ish caps."""
    parts = label.split()
    if len(parts) < 2:
        return False
    return all(p[0].isupper() for p in parts if p and not p.isupper())


def _rejected_pending_label(label: str, kind: str) -> bool:
    """Evidence gate: should a candidate label be REJECTED before becoming a
    pending node?

    Returns True only when the label is clearly non-entity under the bounded
    evidence heuristics (too short, common word, or clearly-generic pattern).
    This is a SAFETY NET that catches the common-word / generic-phrase junk
    class WITHOUT enumerating every past failure. The primary mechanism is
    structural evidence (deterministic detection, known names, entity-like
    label quality, dedup); this helper is the bounded safety net on top.
    """
    t = label.strip()
    if not t:
        return True
    if len(t) <= 2:
        return True
    if _label_is_common_word(t):
        return True
    if _label_matches_generic_pattern(t):
        return True
    return False


def _llm_candidate_has_minimum_evidence(
    label: str,
    detected_entities: Optional[list],
    person_names: Optional[list],
) -> bool:
    """Evidence gate for a candidate that came only from the LLM phase.

    A purely-LLM person/org candidate must clear at least ONE of:
      - it was also detected deterministically (appears in detected_entities), OR
      - it already exists as a person name on the context (person_names), OR
      - it is an all-caps abbreviation (DBS, AWS, ICP-like), OR
      - it is a multi-word proper-noun-ish label that does NOT trigger the
        generic-pattern rejection, OR
      - it is a single token that is >= 3 chars, NOT a common word, and does
        NOT match a generic pattern (entity-like single token).

    This is the main guard against LLM-only over-guesses like 'Please' becoming
    a pending person WITHOUT structural evidence.
    """
    t = label.strip()
    if not t:
        return False
    if _rejected_pending_label(t, "person"):
        return False

    # Strong evidence: already detected deterministically
    if detected_entities:
        for e in detected_entities:
            el = (e.get("label") or "").strip().lower()
            if el == t.lower():
                return True

    # Strong evidence: already a known person name on the context
    if person_names:
        for n in person_names:
            nl = (n or "").strip().lower()
            if nl == t.lower():
                return True

    # Positive signal: all-caps abbreviation
    if _label_is_all_caps_abbreviation(t):
        return True

    # Positive signal: multi-word proper-noun-ish label
    if _label_has_capitalization_heuristic(t):
        return True

    # Multi-word label that is not an abbreviation and not capitalization-heuristic:
    # allow only if it doesn't trigger the generic-pattern rejection
    if " " in t:
        if not _label_matches_generic_pattern(t):
            return True

    # Single-token label: require entity-like quality (not common word, not
    # generic, >= 3 chars)
    if " " not in t:
        if _label_is_common_word(t) or _label_matches_generic_pattern(t):
            return False
        return len(t) >= 3 and t[0].isupper()

    return False


# ── Pending node creation helpers (decision-gated, evidence-gated) ──────────

def _create_pending_org(
    label: str,
    source_text: str,
    owner_id: Optional[str] = None,
    *,
    provenance: Optional[dict] = None,
) -> Optional[int]:
    """Create a pending org node, gated by evidence + provenance, deduplicating
    against existing pending/approved nodes.

    provenance: optional dict with keys origin_table, origin_id (and optional
    source_text_hash). When present, written to pending_nodes.provenance as JSON.
    When absent, the row is still created (live call sites aren't provenance-
    aware yet) but a WARNING is emitted so untraceable-ghost rows are visible.

    Returns pending_nodes.id or None if the candidate was rejected by the
    evidence gate or already exists as an approved graph node.
    """
    from core.services.db import tenant_aware_client, get_tenant
    supabase = tenant_aware_client()
    owner_id = owner_id or get_tenant()

    label = (label or "").strip()
    if not label:
        return None

    # ── Evidence gate: reject clearly-non-entity labels ─────────────────────
    if _rejected_pending_label(label, "organization"):
        audit_log_sync("entity_context", "WARNING",
            f"Rejected pending org candidate '{label}' — fails evidence gate "
            f"(generic/common-word label)")
        return None

    # Check pending nodes first (avoid duplicates). Only an UNRESOLVED pending
    # row counts — an 'approved' row already has a live graph node (Step 2), so
    # returning it would stamp a stale pending_org_id that never resolves.
    existing = supabase.table('pending_nodes').select('id').ilike(
        'label', label
    ).eq('owner_id', owner_id).in_('status', ['pending']).limit(1).execute()
    if existing and existing.data:
        return existing.data[0]['id']

    # Check approved graph nodes — if exists, no need for pending
    existing_gn = supabase.table('graph_nodes').select('id').ilike(
        'label', label
    ).eq('type', 'organization').eq('is_current', True).eq('owner_id', owner_id).limit(1).execute()
    if existing_gn and existing_gn.data:
        return None  # Already approved — caller should use graph_node id

    # Create new pending node (with provenance when available)
    row = {
        'label': label,
        'node_type': 'organization',
        'source_text': source_text[:200] if source_text else '',
        'status': 'pending',
        'owner_id': owner_id,
    }
    if provenance:
        try:
            row['provenance'] = json.dumps(provenance)
        except Exception:
            row['provenance'] = None
    else:
        audit_log_sync("entity_context", "WARNING",
            f"Pending org '{label}' created without provenance "
            f"(origin_table/origin_id missing) — untraceable pending row")
    try:
        res = supabase.table('pending_nodes').insert(row).execute()
        if res.data:
            pending_id = res.data[0]['id']
            audit_log_sync("entity_context", "INFO",
                f"Created pending org '{label}' (id={pending_id})")
            return pending_id
    except Exception as e:
        audit_log_sync("entity_context", "WARNING",
            f"Failed to create pending org '{label}': {e}")
    return None


def _create_pending_person(
    label: str,
    source_text: str,
    owner_id: Optional[str] = None,
    *,
    provenance: Optional[dict] = None,
    detected_entities: Optional[list] = None,
    person_names: Optional[list] = None,
) -> Optional[int]:
    """Create a pending person node, gated by evidence + provenance, deduplicating
    against existing pending/approved nodes.

    For LLM-only candidates, an additional evidence bar is enforced (must clear
    _llm_candidate_has_minimum_evidence) so that pure LLM guesses like 'Please'
    do not become pending persons. detected_entities / person_names are the
    context signals used to decide whether the candidate is LLM-only.

    provenance: optional dict (origin_table, origin_id). Written to
    pending_nodes.provenance as JSON; when absent a WARNING is emitted.

    Returns pending_nodes.id, or None if rejected by the evidence gate / the
    LLM-only minimum-evidence bar / an existing approved graph node.
    """
    from core.services.db import tenant_aware_client, get_tenant
    supabase = tenant_aware_client()
    owner_id = owner_id or get_tenant()

    label = (label or "").strip()
    if not label:
        return None

    # ── Evidence gate: reject clearly-non-entity labels ─────────────────────
    if _rejected_pending_label(label, "person"):
        audit_log_sync("entity_context", "WARNING",
            f"Rejected pending person candidate '{label}' — fails evidence gate "
            f"(generic/common-word label)")
        return None

    # ── LLM-only candidates: require minimum evidence ───────────────────────
    # Only enforced when the caller passes the context signals (queue_pending_
    # candidates always does). A candidate that was also detected deterministi-
    # cally, or is already a known person name, has structural evidence and is
    # allowed; a pure LLM guess must clear the evidence bar below.
    if detected_entities is not None and person_names is not None:
        already_deterministic = any(
            (e.get("label") or "").strip().lower() == label.lower()
            for e in detected_entities
        )
        already_known_person = any(
            (n or "").strip().lower() == label.lower()
            for n in person_names
        )
        if not already_deterministic and not already_known_person:
            if not _llm_candidate_has_minimum_evidence(
                label, detected_entities, person_names
            ):
                audit_log_sync("entity_context", "WARNING",
                    f"Rejected pending person candidate '{label}' — LLM-only "
                    f"guess without minimum evidence (no deterministic signal, "
                    f"not a known person name, and no entity-like label quality)")
                return None

    # Check pending nodes
    existing = supabase.table('pending_nodes').select('id').ilike(
        'label', label
    ).eq('owner_id', owner_id).in_('status', ['pending', 'approved']).limit(1).execute()
    if existing and existing.data:
        return existing.data[0]['id']

    # Check approved graph nodes
    existing_gn = supabase.table('graph_nodes').select('id').ilike(
        'label', label
    ).eq('type', 'person').eq('is_current', True).eq('owner_id', owner_id).limit(1).execute()
    if existing_gn and existing_gn.data:
        return None

    # Create new pending node (with provenance when available)
    row = {
        'label': label,
        'node_type': 'person',
        'source_text': source_text[:200] if source_text else '',
        'status': 'pending',
        'owner_id': owner_id,
    }
    if provenance:
        try:
            row['provenance'] = json.dumps(provenance)
        except Exception:
            row['provenance'] = None
    else:
        audit_log_sync("entity_context", "WARNING",
            f"Pending person '{label}' created without provenance "
            f"(origin_table/origin_id missing) — untraceable pending row")
    try:
        res = supabase.table('pending_nodes').insert(row).execute()
        if res.data:
            pending_id = res.data[0]['id']
            audit_log_sync("entity_context", "INFO",
                f"Created pending person '{label}' (id={pending_id})")
            return pending_id
    except Exception as e:
        audit_log_sync("entity_context", "WARNING",
            f"Failed to create pending person '{label}': {e}")
    return None


def queue_pending_candidates(
    ctx: EntityContext,
    owner_id: str = None,
    *,
    provenance: Optional[dict] = None,
) -> None:
    """Decision-gated materialization: create pending rows for unmatched candidates.

    THE only sanctioned place pending_nodes rows originate from an EntityContext.
    Callers must be sites where a user decision already exists:
      - suggestion-card confirm creates LIVE nodes directly (not via this fn), or
      - message/email approval execution queues the message's NEW entities here
        so they surface in Quick Confirmation for entity-level approval.
    Extraction itself (extract_context_from_source) never writes — this is the
    explicit, gated write step.

    Every row created is evidence-gated (common-word / generic labels are
    rejected) and, when provenance is available, stamped with it so pending
    nodes stay traceable to their source message / raw_dump / run. When
    provenance is absent a WARNING is emitted (live call sites aren't all
    provenance-aware yet) so untraceable-ghost rows are visible for cleanup.

    Fills ctx.pending_org_id / ctx.pending_person_ids so downstream creators can
    stamp pending_org_id on tasks/notes (resolved to a live org on approval).
    Deduplicates against existing pending/approved nodes via the helpers, so
    repeated calls are idempotent.
    """
    if ctx is None:
        return

    provenance = provenance or ctx.pending_provenance

    # Org: queue the primary unmatched org candidate (label recorded by extraction)
    if not ctx.organization_id and ctx.pending_org_label:
        pending_id = _create_pending_org(
            ctx.pending_org_label, ctx.source_text, owner_id, provenance=provenance
        )
        if pending_id:
            ctx.pending_org_id = pending_id
        else:
            # No pending row created — a live graph node already exists for this
            # label (or the candidate failed the evidence gate). Resolve to the
            # live node directly so the org isn't left unlinked.
            existing = _find_existing_org(ctx.pending_org_label, owner_id)
            if existing and not ctx.organization_id:
                ctx.organization_id = existing['id']
                ctx.organization_name = existing['label']

    # Persons: queue every detected person candidate with no live match.
    # Deterministic Phase-1 entries carry matched=True/False; LLM entries without
    # the flag fall through to the helpers' own dedup checks (approved node or an
    # existing pending row → None → skipped).
    for e in ctx.detected_entities or []:
        if (e.get("type") or "") != "person":
            continue
        if e.get("matched") or e.get("existing_matches"):
            continue
        label = (e.get("label") or "").strip()
        if not label:
            continue
        pending_id = _create_pending_person(
            label,
            ctx.source_text,
            owner_id,
            provenance=provenance,
            detected_entities=ctx.detected_entities,
            person_names=ctx.person_names,
        )
        if pending_id and pending_id not in ctx.pending_person_ids:
            ctx.pending_person_ids.append(pending_id)


def _find_existing_org(label: str, owner_id: str = None) -> Optional[dict]:
    """Find an existing org graph node by label. Returns {id, label} or None."""
    from core.services.db import tenant_aware_client, get_tenant
    supabase = tenant_aware_client()
    owner_id = owner_id or get_tenant()

    try:
        # Use %label% for partial matching — ilike('John') won't match 'John Smith'
        res = supabase.table('graph_nodes').select('id, label').ilike(
            'label', f'%{label}%'
        ).eq('type', 'organization').eq('is_current', True).eq('owner_id', owner_id).limit(1).execute()
        if res and res.data:
            return res.data[0]
    except Exception:
        pass
    return None


def _find_existing_person(label: str, owner_id: str = None) -> Optional[dict]:
    """Find an existing person graph node by label. Returns {id, label} or None."""
    from core.services.db import tenant_aware_client, get_tenant
    supabase = tenant_aware_client()
    owner_id = owner_id or get_tenant()

    try:
        # Use %label% for partial matching — ilike('John') won't match 'John Smith'
        res = supabase.table('graph_nodes').select('id, label').ilike(
            'label', f'%{label}%'
        ).eq('type', 'person').eq('is_current', True).eq('owner_id', owner_id).limit(1).execute()
        if res and res.data:
            return res.data[0]
    except Exception:
        pass
    return None




_ORG_COLLAPSE_SUFFIXES = {
    'os', 'labs', 'media', 'dynamics', 'group', 'bank', 'hotels', 'analytics',
    'studios', 'systems', 'partners', 'ventures', 'technologies', 'solutions',
    'works', 'industries', 'capital', 'digital', 'software', 'consulting',
    'holdings', 'networks', 'logistics', 'bio', 'pharma', 'inc', 'llc', 'ltd',
    'limited', 'corp', 'corporation', 'company', 'firm', 'agency', 'enterprise'
}

def _collapse_org_duplicates(ctx: EntityContext):
    """Collapse duplicate org candidates like 'Rhodey' and 'Rhodey OS'.
    
    If one candidate is a word-boundary prefix of another, and the trailing
    tokens are all in _ORG_COLLAPSE_SUFFIXES, collapse the shorter into the
    longer, preserving the higher confidence.
    """
    # Only operate on organizations
    orgs = [e for e in ctx.detected_entities if e.get("type") == "organization"]
    if len(orgs) < 2:
        return

    from core.services.db import tenant_aware_client, get_tenant
    supabase = tenant_aware_client()
    owner_id = get_tenant()

    to_remove = []
    
    for i, o1 in enumerate(orgs):
        for j, o2 in enumerate(orgs):
            if i == j or i in to_remove or j in to_remove:
                continue
            
            l1 = (o1.get("label") or "").strip()
            l2 = (o2.get("label") or "").strip()
            
            if not l1 or not l2:
                continue
                
            w1 = l1.lower().split()
            w2 = l2.lower().split()
            
            # Check if l1 is a strict prefix of l2
            if len(w1) < len(w2) and w2[:len(w1)] == w1:
                trailing = w2[len(w1):]
                # Are all trailing words in the suffix list?
                if all(t in _ORG_COLLAPSE_SUFFIXES for t in trailing):
                    # Guard: If BOTH are live nodes, never collapse
                    # (e.g. Ashraya vs Ashraya Chennai North)
                    try:
                        res1 = supabase.table('graph_nodes').select('id').eq('type', 'organization').ilike('label', l1).eq('is_current', True).eq('owner_id', owner_id).limit(1).execute()
                        res2 = supabase.table('graph_nodes').select('id').eq('type', 'organization').ilike('label', l2).eq('is_current', True).eq('owner_id', owner_id).limit(1).execute()
                        if res1 and res1.data and res2 and res2.data:
                            continue  # Both live, distinct orgs
                    except Exception:
                        pass
                    
                    # Collapse o1 (shorter) into o2 (longer)
                    to_remove.append(i)
                    o2["confidence"] = max(o1.get("confidence", 0), o2.get("confidence", 0))
                    
                    # Update pending_org_label / organization_name if it was the short form
                    if ctx.pending_org_label and ctx.pending_org_label.lower() == l1.lower():
                        ctx.pending_org_label = l2
                    if ctx.organization_name and ctx.organization_name.lower() == l1.lower():
                        ctx.organization_name = l2

    # Rebuild detected_entities without the removed ones
    if to_remove:
        new_entities = []
        org_idx = 0
        for e in ctx.detected_entities:
            if e.get("type") == "organization":
                if org_idx not in to_remove:
                    new_entities.append(e)
                org_idx += 1
            else:
                new_entities.append(e)
        ctx.detected_entities = new_entities


# ── Entity processing ────────────────────────────────────────────────────────

def _process_deterministic_entities(entities: list, ctx: EntityContext, owner_id: str = None):
    """Process deterministic entities (from detect_entities) into EntityContext.

    Pure detection/matching — extraction NEVER writes pending nodes (HITL).
    Candidates not found in the graph are recorded (label + matched flag) for
    the suggestion card and for decision-gated queue_pending_candidates() calls.
    """
    for e in entities:
        matched = bool(e.db_id and not e.is_new)
        # Populate detected_entities so they aren't lost to the UI
        ctx.detected_entities.append({
            "type": e.type,
            "label": e.label,
            "confidence": 1.0,
            "source": "deterministic",
            "matched": matched
        })

        if e.type == 'organization':
            if matched:
                # Existing org — use it (prefer existing over pending)
                if not ctx.organization_id:
                    ctx.organization_id = e.db_id
                    ctx.organization_name = e.label
            else:
                # New org — record the label for UI + gated queue only.
                if not ctx.organization_id and not ctx.pending_org_label:
                    ctx.pending_org_label = e.label

        elif e.type == 'person':
            if matched:
                if e.db_id not in ctx.person_ids:
                    ctx.person_ids.append(e.db_id)
                    ctx.person_names.append(e.label)
            else:
                # New person — dry-run only: track the name for UI display.
                # The pending row is created only by queue_pending_candidates()
                # at a decision-gated site, never by extraction.
                if e.label not in ctx.person_names:
                    ctx.person_names.append(e.label)


async def _llm_extract_orgs_and_persons(text: str) -> dict:
    """Single LLM call: detect ALL orgs AND persons (including implicit) + determine primary org.

    Returns: {organizations: [{name, confidence, is_primary}], persons: [{name, confidence}]}
    """
    from core.llm.fallback import generate_content_with_fallback
    from core.llm.constants import CLASSIFICATION_MODEL
    from core.llm.config import WorkloadProfile

    prompt = f"""Analyze this text and identify ALL organizations AND people mentioned.

Text: "{text[:500]}"

Return JSON:
{{
  "organizations": [
    {{"name": "...", "confidence": 0.0-1.0, "is_primary": true}}
  ],
  "persons": [
    {{"name": "...", "confidence": 0.0-1.0}}
  ]
}}

Rules for organizations:
- Include organizations even if not preceded by "client", "vendor", etc.
- "Acme Corp invoice for $500" → Acme Corp is the org
- "Call John about the X proposal" → X is the org
- Only return specific organization names, not generic terms like "company", "team"

Rules for persons:
- Include full names when available: "John Smith" not just "John"
- Include people mentioned by first name if clearly a person: "Call John about..."
- Don't include generic terms like "team", "manager", "someone"
- If a person is referred to by first name only, use that: {{"name": "John", "confidence": 0.8}}

If no organizations or persons found, return {{"organizations": [], "persons": []}}"""

    try:
        response = await generate_content_with_fallback(
            prompt=prompt,
            workload=WorkloadProfile.INTERACTIVE,
            primary_model=CLASSIFICATION_MODEL,
            config={'response_mime_type': 'application/json'},
        )
        if response and response.text:
            raw = response.text.strip()
            if raw.startswith("```"):
                lines = raw.split("\n")
                raw = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])
            data = json.loads(raw)
            if isinstance(data, dict):
                return data
    except Exception as e:
        audit_log_sync("entity_context", "WARNING",
            f"LLM org extraction failed: {e}")
    return {"organizations": []}


def _integrate_llm_result(llm_result: dict, ctx: EntityContext, owner_id: str = None):
    """Integrate LLM org + person detection results into EntityContext.

    Pure detection/matching — extraction NEVER writes pending nodes (HITL).
    Only adds orgs/persons that the deterministic layer missed.
    Respects primary org designation from LLM.
    """
    orgs = llm_result.get("organizations", [])
    persons = llm_result.get("persons", [])

    from core.lib.graph_rules import resolve_alias

    for org in orgs:
        label = (org.get("name") or "").strip()
        is_primary = org.get("is_primary", False)
        
        # Resolve alias early so short-forms map to canonicals
        label = resolve_alias(label)

        if not label or len(label) < 2:
            continue

        # Skip if already detected deterministically
        label_lower = label.lower()
        
        # Add to detected entities for the suggestion card if not already there
        if not any(e.get("label", "").lower() == label_lower for e in ctx.detected_entities):
            confidence = org.get("confidence", 1.0)
            if confidence >= 0.5:
                ctx.detected_entities.append({"type": "organization", "label": label, "confidence": confidence})

        if (ctx.organization_name and ctx.organization_name.lower() == label_lower):
            if is_primary:
                pass
            continue
        if (ctx.pending_org_label and ctx.pending_org_label.lower() == label_lower):
            if is_primary and not ctx.organization_id:
                pass
            continue

        # Check if org exists in graph
        existing = _find_existing_org(label, owner_id)

        if existing:
            if is_primary:
                # LLM primary always wins over deterministic first-match
                ctx.organization_id = existing['id']
                ctx.organization_name = label
            # Secondary existing orgs — track for org-to-org edge
            elif label not in ctx.org_to_org_edge_labels:
                ctx.org_to_org_edge_labels.append(label)
        else:
            # New org — extraction never writes (HITL). When the LLM designates
            # it primary, record the label for the card / gated queue only.
            if is_primary:
                ctx.pending_org_label = label
                # Clear deterministic first-match if LLM says a different org is primary
                if ctx.organization_name and ctx.organization_name.lower() != label.lower():
                    ctx.organization_id = None
                    ctx.organization_name = None

    # Integrate persons from LLM
    for person in persons:
        label = (person.get("name") or "").strip()
        confidence = person.get("confidence", 0.0)
        if not label or len(label) < 2 or confidence < 0.5:
            continue

        label_lower = label.lower()

        # Skip if this label was already detected as an organization.
        # The LLM sometimes classifies the same entity as both org and person
        # (e.g. "Havnelight team" → org via suffix, person via "team" context).
        # Deterministic org detection takes priority.
        if any(e.get("label", "").lower() == label_lower and e.get("type") == "organization"
               for e in ctx.detected_entities):
            continue

        if not any(e.get("label", "").lower() == label_lower for e in ctx.detected_entities):
            ctx.detected_entities.append({"type": "person", "label": label, "confidence": confidence})

        # Skip if already detected deterministically
        if any(pn.lower() == label_lower for pn in ctx.person_names):
            continue

        # Check if person exists in graph
        existing_person = _find_existing_person(label, owner_id)
        if existing_person:
            if existing_person['id'] not in ctx.person_ids:
                ctx.person_ids.append(existing_person['id'])
                ctx.person_names.append(label)
        else:
            # New person — dry-run only: add the name so the suggestion card
            # shows it. The pending row comes from queue_pending_candidates()
            # at a decision-gated site, never from extraction.
            if label not in ctx.person_names:
                ctx.person_names.append(label)


def _propose_org_to_org_edges(ctx: EntityContext):
    """If multiple orgs found, propose org-to-org edges.

    The primary org is the one linked to the task/note.
    Secondary orgs get edges TO the primary.
    """
    all_org_labels = []
    if ctx.organization_name:
        all_org_labels.append(ctx.organization_name)
    if ctx.pending_org_label:
        all_org_labels.append(ctx.pending_org_label)
    # Add secondary orgs detected by LLM
    all_org_labels.extend(ctx.org_to_org_edge_labels)

    if len(all_org_labels) < 2:
        return  # Only one org — no edge needed

    primary = ctx.organization_name or ctx.pending_org_label
    secondary = [o for o in all_org_labels if o != primary]

    for sec in secondary:
        # Default to CLIENT_OF — the most common business relationship
        edge = {
            "source_label": sec,
            "target_label": primary,
            "relationship": "CLIENT_OF",
        }
        # Avoid duplicate edges
        if not any(e["source_label"] == sec and e["target_label"] == primary
                   for e in ctx.org_to_org_edges):
            ctx.org_to_org_edges.append(edge)


# ── Main function ────────────────────────────────────────────────────────────

async def extract_context_from_source(
    text: str,
    cached_entities: list = None,
    timing: str = "sync",
    owner_id: str = None,
) -> EntityContext:
    """THE single entity extraction function for the entire OS.

    Pure detection + matching — this function NEVER writes to pending_nodes /
    graph_nodes (Human-in-the-Loop). Candidates not found in the graph are
    recorded on the returned EntityContext (labels + detected_entities) so the
    suggestion card can show them; pending rows are created ONLY by
    queue_pending_candidates(), which decision-gated sites (card confirm,
    message/email approval) call explicitly.

    Three phases:
      Phase 1: Deterministic detect_entities() — fast, ~200ms
      Phase 2: LLM extraction — catches implicit orgs, determines primary
      Phase 3: Reconciliation (collapse dupes, propose org edges)

    Args:
        text: Full source text (message, document content, etc.)
        cached_entities: Optional pre-computed detect_entities() results
        timing: Informational only ("sync"|"async"|"card"); affects the
            Personal-org fallback and audit labels, never pending creation.
        owner_id: Tenant owner id (defaults to current tenant scope).

    Returns:
        EntityContext with org, person, and edge information.
    """
    if not text or not text.strip():
        return EntityContext(source_text="")

    ctx = EntityContext(source_text=text[:500])
    ctx.extraction_timing = timing

    # Resolve owner_id once, pass through to all helpers
    from core.services.db import get_tenant
    resolved_owner_id = owner_id or get_tenant()

    # ── Phase 1: Deterministic detection (fast, finds known orgs) ──
    try:
        if cached_entities is not None:
            entities = cached_entities
        else:
            from core.lib.entity_detector import detect_entities
            entities = detect_entities(text)
        _process_deterministic_entities(entities, ctx, resolved_owner_id)
    except Exception as e:
        audit_log_sync("entity_context", "WARNING",
            f"Deterministic entity detection failed: {e}")

    # ── Phase 2: LLM extraction (always runs, catches implicit orgs + persons) ──
    try:
        llm_result = await _llm_extract_orgs_and_persons(text)
        _integrate_llm_result(llm_result, ctx, resolved_owner_id)
    except Exception as e:
        audit_log_sync("entity_context", "WARNING",
            f"LLM entity extraction failed: {e}")

    # ── Phase 3: Reconciliation + pending node creation ──
    _collapse_org_duplicates(ctx)
    _propose_org_to_org_edges(ctx)

    # ── Phase 4: Personal org fallback (no org detected → use Personal) ──
    if timing != "card" and not ctx.organization_id and not ctx.pending_org_label:
        try:
            from core.services.db import tenant_aware_client
            supabase = tenant_aware_client()
            # Look up existing Personal org (approved graph node = UUID → organization_id)
            existing = supabase.table('graph_nodes').select('id, label').ilike(
                'label', 'Personal'
            ).eq('type', 'organization').eq('is_current', True).eq('owner_id', resolved_owner_id).limit(1).execute()
            if existing and existing.data:
                ctx.organization_id = existing.data[0]['id']  # UUID → organization_id
                ctx.organization_name = 'Personal'
                ctx.extraction_method = "fallback_personal"
                audit_log_sync("entity_context", "INFO",
                    "Personal org fallback: linked to existing Personal org")
            else:
                # Personal org missing (shouldn't happen — tenant seeding creates
                # it). Extraction never creates it; leave org unlinked rather
                # than silently queueing a node the user never asked for.
                audit_log_sync("entity_context", "WARNING",
                    "Personal org fallback: no existing Personal org found; leaving org unlinked")
        except Exception as e:
            audit_log_sync("entity_context", "WARNING",
                f"Personal org fallback failed: {e}")

    ctx.extraction_method = ctx.extraction_method or ("hybrid" if ctx.pending_org_id else "deterministic")

    audit_log_sync("entity_context", "INFO",
        f"Entity context extracted [{timing}]: org={ctx.organization_name or ctx.pending_org_label or '(none)'}, "
        f"persons={len(ctx.person_ids) + len(ctx.pending_person_ids)}, "
        f"org_edges={len(ctx.org_to_org_edges)}, "
        f"method={ctx.extraction_method}")

    return ctx
