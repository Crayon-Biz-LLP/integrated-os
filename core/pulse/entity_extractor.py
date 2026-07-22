"""Entity extraction — simplified.

Entity detection is now done by core.lib.entity_detector.detect_entities()
(deterministic, no LLM). The LLM is only called to extract RELATIONSHIPS
between already-detected entities.

Major simplifications from the old version:
- Removed: LLM entity detection (was biased by prompt examples)
- Removed: 3-layer Guard B (not needed — deterministic output is always valid)
- Removed: 80-line extraction prompt with ✓/✗ examples
- Removed: known entity injection (detector does this via DB lookup)
- New: entity_detector.detect_entities() runs first (synchronous, instant)
- New: relationship-only LLM call for edges between confirmed entities
"""

from core.llm.constants import CLASSIFICATION_MODEL
from core.lib.audit_logger import audit_log_sync
from core.lib.url_filter import is_url_text
from core.services.db import get_supabase, maybe_single_safe
from core.llm.fallback import generate_content_with_fallback
from core.llm.config import WorkloadProfile
from core.pulse.graph import insert_extracted_entities
from core.prompts.relationship import RELATIONSHIP_EXTRACTION_PROMPT
from core.lib.entity_detector import detect_entities

supabase = get_supabase()


async def extract_and_link_entities(
    text: str, source_id: str, source_type: str = 'task'
):
    """Simplified entity extraction using deterministic detection + relationship LLM.

    Phase 1 (deterministic, no LLM):
      detect_entities() scans text against known entities in DB and structural
      patterns. Returns confirmed entities with types and DB IDs.

    Phase 2 (LLM, only for relationships):
      Given text + list of confirmed entities, extract edges between them.

    source_type: 'task', 'memory', or 'raw_dump'
    Returns: (org_candidates, proj_candidates) for upstream backfill.
    """
    # URL FILTER: Do not extract entities from text containing URLs
    if is_url_text(text):
        audit_log_sync("pulse", "INFO", "Skipped entity extraction: text contains URL")
        return [], []

    # ════════════════════════════════════════════
    # Phase 1: Deterministic entity detection
    # No LLM, no prompt, no bias.
    # ════════════════════════════════════════════
    entities = detect_entities(text)

    nodes = []
    for e in entities:
        node = {"label": e.label, "type": e.type}
        nodes.append(node)

    # ════════════════════════════════════════════
    # Phase 2: Relationship extraction (LLM only)
    # Given confirmed entities, extract edges between them.
    # ════════════════════════════════════════════
    edges = []
    if entities:
        entity_list_str = "\n".join(
            f"  - {e.label} ({e.type})" for e in entities
        )
        prompt = RELATIONSHIP_EXTRACTION_PROMPT.format(
            text=text,
            entities=entity_list_str,
        )

        try:
            response = await generate_content_with_fallback(
                prompt=prompt,
                workload=WorkloadProfile.INTERACTIVE,
                primary_model=CLASSIFICATION_MODEL,
                config={'response_mime_type': 'application/json'},
            )
            if response and response.text:
                data = response.parse_json()
                edges = data if isinstance(data, list) else data.get("edges", [])
        except Exception as llm_e:
            audit_log_sync(
                "pulse", "WARNING",
                f"Relationship extraction LLM failed for {source_type} {source_id}: {llm_e}"
            )

    if not nodes and not edges:
        return [], []

    insert_extracted_entities(
        nodes=nodes, edges=edges,
        source_id=str(source_id), source_type=source_type,
        source_content=text,
    )
    print(
        f"🕸️ Entities detected for {source_type} {source_id}: "
        f"{len(nodes)} nodes, {len(edges)} edges routed to pending"
    )

    # Look up canonical IDs for upstream backfill
    org_candidates = []
    proj_candidates = []
    for n in nodes:
        label = n.get("label", "").strip()
        ntype = n.get("type", "")
        if not label:
            continue

        if ntype == "organization":
            try:
                res = maybe_single_safe(
                    supabase.table('organizations').select('id').ilike('name', label)
                )
                if res and res.data:
                    org_candidates.append(res.data['id'])
            except Exception:
                pass
        elif ntype == "project":
            try:
                res = maybe_single_safe(
                    supabase.table('projects')
                    .select('id, organization_id')
                    .ilike('name', label)
                    .eq('is_current', True)
                )
                if res and res.data:
                    proj_candidates.append({
                        'id': res.data['id'],
                        'org_id': res.data.get('organization_id'),
                    })
            except Exception:
                pass

    return org_candidates, proj_candidates
