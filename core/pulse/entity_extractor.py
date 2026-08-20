"""Entity extraction — hardened with LLM primary + deterministic verification.

Phase 1: Deterministic detection (DB ground truth + structural anchors)
Phase 2: LLM semantic extraction (nodes + edges)
Phase 3: Reconciliation (DB wins > Agreement > Conflict→Pending)
"""

from core.llm.constants import CLASSIFICATION_MODEL
from core.lib.audit_logger import audit_log_sync
from core.lib.url_filter import is_url_text
from core.services.db import maybe_single_safe, tenant_aware_client
from core.llm.fallback import generate_content_with_fallback
from core.llm.config import WorkloadProfile
from core.pulse.graph import insert_extracted_entities
from core.prompts.entity_extraction import ENTITY_EXTRACTION_PROMPT
from core.lib.entity_detector import detect_entities
from core.lib.entity_reconcile import reconcile_entity_types

supabase = tenant_aware_client()

async def extract_and_link_entities(
    text: str, source_id: str, source_type: str = 'task'
):
    """Hardened entity extraction using LLM primary + pattern verifier.

    source_type: 'task', 'memory', or 'raw_dump'
    Returns: org_candidates list for upstream backfill.
    """
    if is_url_text(text):
        audit_log_sync("pulse", "INFO", "Skipped entity extraction: text contains URL")
        return []

    # ════════════════════════════════════════════
    # Phase 1: Deterministic entity detection (Grounding & Recall)
    # ════════════════════════════════════════════
    pattern_entities = detect_entities(text)

    # ════════════════════════════════════════════
    # Phase 2: LLM Extraction (Semantics & Judgment)
    # ════════════════════════════════════════════
    llm_nodes = []
    llm_edges = []
    
    prompt = ENTITY_EXTRACTION_PROMPT.format(text=text)

    try:
        response = await generate_content_with_fallback(
            prompt=prompt,
            workload=WorkloadProfile.INTERACTIVE,
            primary_model=CLASSIFICATION_MODEL,
            config={'response_mime_type': 'application/json'},
        )
        if response and response.text:
            data = response.parse_json()
            if isinstance(data, dict):
                llm_nodes = data.get("nodes", [])
                llm_edges = data.get("edges", [])
    except Exception as llm_e:
        audit_log_sync(
            "pulse", "WARNING",
            f"LLM extraction failed for {source_type} {source_id}: {llm_e}"
        )

    # ════════════════════════════════════════════
    # Phase 3: Reconciliation (Fusion & Co-signer rules)
    # ════════════════════════════════════════════
    reconciled_nodes = reconcile_entity_types(llm_nodes, pattern_entities)

    if not reconciled_nodes and not llm_edges:
        return []

    insert_extracted_entities(
        nodes=reconciled_nodes, 
        edges=llm_edges,
        source_id=str(source_id), 
        source_type=source_type,
        source_content=text,
    )
    print(
        f"🕸️ Entities detected for {source_type} {source_id}: "
        f"{len(reconciled_nodes)} nodes, {len(llm_edges)} edges routed"
    )

    # Look up canonical IDs for upstream backfill
    org_candidates = []
    for n in reconciled_nodes:
        label = n.get("label", "").strip()
        ntype = n.get("type", "")
        db_id = n.get("db_id")
        if not label:
            continue

        if ntype == "organization":
            if db_id:
                org_candidates.append(db_id)
            else:
                try:
                    res = maybe_single_safe(
                        supabase.table('graph_nodes').select('id').eq('type', 'organization').eq('is_current', True).ilike('label', label)
                    )
                    if res and res.data:
                        org_candidates.append(res.data['id'])
                except Exception:
                    pass

    return org_candidates
