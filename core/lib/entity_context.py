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
            person_ids=data.get("person_ids") or [],
            person_names=data.get("person_names") or [],
            pending_person_ids=data.get("pending_person_ids") or [],
            org_to_org_edges=data.get("org_to_org_edges") or [],
            source_text=data.get("source_text") or "",
            extraction_method=data.get("extraction_method") or "",
        )


# ── Pending node creation helpers ────────────────────────────────────────────

def _create_pending_org(label: str, source_text: str, owner_id: str = None) -> Optional[int]:
    """Create a pending org node, deduplicating against existing pending/approved nodes.

    Returns pending_nodes.id or None if org already exists as approved graph node.
    """
    from core.services.db import tenant_aware_client, get_tenant
    supabase = tenant_aware_client()
    owner_id = owner_id or get_tenant()

    # Check pending nodes first (avoid duplicates)
    existing = supabase.table('pending_nodes').select('id').ilike(
        'label', label
    ).eq('owner_id', owner_id).in_('status', ['pending', 'approved']).limit(1).execute()
    if existing and existing.data:
        return existing.data[0]['id']

    # Check approved graph nodes — if exists, no need for pending
    existing_gn = supabase.table('graph_nodes').select('id').ilike(
        'label', label
    ).eq('type', 'organization').eq('is_current', True).eq('owner_id', owner_id).limit(1).execute()
    if existing_gn and existing_gn.data:
        return None  # Already approved — caller should use graph_node id

    # Create new pending node
    try:
        res = supabase.table('pending_nodes').insert({
            'label': label,
            'node_type': 'organization',
            'source_text': source_text[:200] if source_text else '',
            'status': 'pending',
            'owner_id': owner_id,
        }).execute()
        if res.data:
            pending_id = res.data[0]['id']
            audit_log_sync("entity_context", "INFO",
                f"Created pending org '{label}' (id={pending_id})")
            return pending_id
    except Exception as e:
        audit_log_sync("entity_context", "WARNING",
            f"Failed to create pending org '{label}': {e}")
    return None


def _create_pending_person(label: str, source_text: str, owner_id: str = None) -> Optional[int]:
    """Create a pending person node, deduplicating against existing nodes.

    Returns pending_nodes.id or None if person already exists.
    """
    from core.services.db import tenant_aware_client, get_tenant
    supabase = tenant_aware_client()
    owner_id = owner_id or get_tenant()

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

    # Create new pending node
    try:
        res = supabase.table('pending_nodes').insert({
            'label': label,
            'node_type': 'person',
            'source_text': source_text[:200] if source_text else '',
            'status': 'pending',
            'owner_id': owner_id,
        }).execute()
        if res.data:
            pending_id = res.data[0]['id']
            audit_log_sync("entity_context", "INFO",
                f"Created pending person '{label}' (id={pending_id})")
            return pending_id
    except Exception as e:
        audit_log_sync("entity_context", "WARNING",
            f"Failed to create pending person '{label}': {e}")
    return None


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



# ── Entity processing ────────────────────────────────────────────────────────

def _process_deterministic_entities(entities: list, ctx: EntityContext, owner_id: str = None, timing: str = "sync"):
    """Process deterministic entities (from detect_entities) into EntityContext."""
    for e in entities:
        # Populate detected_entities so they aren't lost to the UI
        ctx.detected_entities.append({
            "type": e.type,
            "label": e.label,
            "confidence": 1.0,
            "source": "deterministic"
        })

        if e.type == 'organization':
            if e.db_id and not e.is_new:
                # Existing org — use it (prefer existing over pending)
                if not ctx.organization_id:
                    ctx.organization_id = e.db_id
                    ctx.organization_name = e.label
            elif e.is_new:
                # New org — create pending node
                pending_id = _create_pending_org(e.label, ctx.source_text, owner_id) if timing != "card" else None
                if pending_id and not ctx.pending_org_id:
                    ctx.pending_org_id = pending_id
                    ctx.pending_org_label = e.label
                elif not pending_id and timing == "card" and not ctx.pending_org_id:
                    # Dry run — simulate pending org
                    ctx.pending_org_label = e.label
                elif not pending_id and timing != "card":
                    # Org already exists as approved — find its graph_node id
                    existing = _find_existing_org(e.label, owner_id)
                    if existing and not ctx.organization_id:
                        ctx.organization_id = existing['id']
                        ctx.organization_name = e.label

        elif e.type == 'person':
            if e.db_id and not e.is_new:
                if e.db_id not in ctx.person_ids:
                    ctx.person_ids.append(e.db_id)
                    ctx.person_names.append(e.label)
            elif e.is_new:
                pending_id = _create_pending_person(e.label, ctx.source_text, owner_id) if timing != "card" else None
                if pending_id:
                    if pending_id not in ctx.pending_person_ids:
                        ctx.pending_person_ids.append(pending_id)
                        ctx.person_names.append(e.label)
                elif timing == "card":
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


def _integrate_llm_result(llm_result: dict, ctx: EntityContext, owner_id: str = None, timing: str = "sync"):
    """Integrate LLM org + person detection results into EntityContext.

    Only adds orgs/persons that the deterministic layer missed.
    Respects primary org designation from LLM.
    """
    orgs = llm_result.get("organizations", [])
    persons = llm_result.get("persons", [])

    for org in orgs:
        label = (org.get("name") or "").strip()
        is_primary = org.get("is_primary", False)

        if not label or len(label) < 2:
            continue

        # Skip if already detected deterministically
        label_lower = label.lower()
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
            if is_primary and not ctx.organization_id:
                ctx.organization_id = existing['id']
                ctx.organization_name = label
            # Secondary existing orgs — track for org-to-org edge
            elif label not in ctx.org_to_org_edge_labels:
                ctx.org_to_org_edge_labels.append(label)
        else:
            # New org — create pending
            pending_id = _create_pending_org(label, ctx.source_text, owner_id) if timing != "card" else None
            if pending_id:
                if is_primary and not ctx.pending_org_id and not ctx.organization_id:
                    ctx.pending_org_id = pending_id
                    ctx.pending_org_label = label
                # Secondary new orgs — track for org-to-org edge
                elif label not in ctx.org_to_org_edge_labels:
                    ctx.org_to_org_edge_labels.append(label)

    # Integrate persons from LLM
    for person in persons:
        label = (person.get("name") or "").strip()
        confidence = person.get("confidence", 0.0)
        if label and confidence >= 0.5:
            ctx.detected_entities.append({"type": "person", "label": label, "confidence": confidence})

        if not label or len(label) < 2 or confidence < 0.5:
            continue

        # Skip if already detected deterministically
        label_lower = label.lower()
        if any(pn.lower() == label_lower for pn in ctx.person_names):
            continue

        # Check if person exists in graph
        existing_person = _find_existing_person(label, owner_id)
        if existing_person:
            if existing_person['id'] not in ctx.person_ids:
                ctx.person_ids.append(existing_person['id'])
                ctx.person_names.append(label)
        else:
            # Create pending person
            pending_id = _create_pending_person(label, ctx.source_text, owner_id) if timing != "card" else None
            if pending_id and pending_id not in ctx.pending_person_ids:
                ctx.pending_person_ids.append(pending_id)
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

    One function. Three phases. Different call sites via timing parameter:
      timing="sync"  → at creation time, stamps pending_org_id immediately
      timing="async" → after creation, backfills if sync pass missed
      timing="card"  → for suggestion card display, returns entities for UI

    Three phases:
      Phase 1: Deterministic detect_entities() — fast, ~200ms
      Phase 2: LLM extraction — catches implicit orgs, determines primary
      Phase 3: Reconciliation + pending node creation

    Args:
        text: Full source text (message, document content, etc.)
        cached_entities: Optional pre-computed detect_entities() results
        timing: "sync" (creation time), "async" (enrichment), "card" (UI)

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
        _process_deterministic_entities(entities, ctx, resolved_owner_id, timing=timing)
    except Exception as e:
        audit_log_sync("entity_context", "WARNING",
            f"Deterministic entity detection failed: {e}")

    # ── Phase 2: LLM extraction (always runs, catches implicit orgs + persons) ──
    try:
        llm_result = await _llm_extract_orgs_and_persons(text)
        _integrate_llm_result(llm_result, ctx, resolved_owner_id, timing=timing)
    except Exception as e:
        audit_log_sync("entity_context", "WARNING",
            f"LLM entity extraction failed: {e}")

    # ── Phase 3: Reconciliation + pending node creation ──
    _propose_org_to_org_edges(ctx)

    # ── Phase 4: Personal org fallback (no org detected → use Personal) ──
    if timing != "card" and not ctx.organization_id and not ctx.pending_org_id:
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
                # Lazily create Personal org
                pending_id = _create_pending_org('Personal', ctx.source_text, resolved_owner_id)
                if pending_id:
                    ctx.pending_org_id = pending_id
                    ctx.pending_org_label = 'Personal'
                    ctx.extraction_method = "fallback_personal_created"
                    audit_log_sync("entity_context", "INFO",
                        "Personal org fallback: created pending Personal org")
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
