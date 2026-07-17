from core.llm.constants import CLASSIFICATION_MODEL
from core.lib.audit_logger import audit_log_sync
from core.lib.url_filter import is_url_text
from core.services.db import get_supabase, maybe_single_safe
from core.llm.fallback import generate_content_with_fallback
from core.llm.config import WorkloadProfile
from core.pulse.graph import insert_extracted_entities
from core.prompts.entity_extraction import SHARED_EXTRACTION_PROMPT

supabase = get_supabase()

async def extract_and_link_entities(text: str, source_id: str, source_type: str = 'task'):
    """
    Real-time entity extraction using Gemini Flash Lite.
    Extracts entities and updates the graph immediately during ingestion.
    source_type: 'task', 'memory', or 'raw_dump'
    """
    # URL FILTER: Do not extract entities from text containing URLs
    if is_url_text(text):
        audit_log_sync("pulse", "INFO", "Skipped entity extraction: text contains URL")
        return [], []

    # Fetch known entities for prompt injection
    try:
        kn_res = supabase.table('graph_nodes').select('label, type').in_('type', ['person', 'organization', 'project', 'place', 'event', 'animal', 'emotional_state']).neq('epistemic_status', 'hypothetical').eq('is_current', True).execute()
        known_labels = [r['label'] for r in kn_res.data] if kn_res and kn_res.data else []
        known_str = ", ".join(known_labels[:50]) # limit to avoid huge prompts
    except Exception:
        known_str = "Danny, Mother, Qhord, Ashraya, Solvstrat, Rhodey OS"

    known_str_extra = f"\n- KNOWN ENTITIES (use exact spelling if referring to these): {known_str}" if known_str else ""
    prompt = SHARED_EXTRACTION_PROMPT + known_str_extra + f"\n\nText: \"{text}\"\n"

    try:
        response = await generate_content_with_fallback(
            prompt=prompt,
            workload=WorkloadProfile.INTERACTIVE,
            primary_model=CLASSIFICATION_MODEL,
            config={'response_mime_type': 'application/json'}
        )
        if not response or not response.text:
            return [], []

        data = response.parse_json()
        nodes = data.get("nodes", [])
        edges = data.get("edges", [])

        # ── Guard B: Text-anchoring validation ──
        # Drop any extracted node whose label doesn't appear verbatim in the source text
        text_lower = text.lower()
        valid_nodes = []
        for n in nodes:
            label = n.get("label", "") if isinstance(n, dict) else ""
            if not label:
                continue
            if label.lower() in text_lower or label.lower() == 'danny':
                valid_nodes.append(n)
            else:
                audit_log_sync("pulse", "INFO",
                               f"Text-anchoring guard: dropped hallucinated node '{label}' from {source_type}:{source_id}")
        valid_labels = {n.get('label', '').lower() for n in valid_nodes}
        valid_edges = []
        for e in edges:
            if isinstance(e, dict) and e.get('source', '').lower() in valid_labels and e.get('target', '').lower() in valid_labels:
                valid_edges.append(e)
        nodes = valid_nodes
        edges = valid_edges

        if not nodes and not edges:
            return [], []
            
        insert_extracted_entities(nodes=nodes, edges=edges, source_id=str(source_id), source_type=source_type, source_content=text)
        print(f"🕸️ Real-time entities extracted for {source_type} {source_id}: {len(nodes)} nodes, {len(edges)} edges routed to pending")
        
        # Look up canonical orgs and projects from extracted nodes for enrichment
        org_candidates = []
        proj_candidates = []
        for n in nodes:
            label = n.get("label", "").strip()
            ntype = n.get("type", "")
            if not label:
                continue
            
            if ntype == "organization":
                try:
                    res = maybe_single_safe(supabase.table('organizations').select('id').ilike('name', label))
                    if res and res.data:
                        org_candidates.append(res.data['id'])
                except Exception:
                    pass
            elif ntype == "project":
                try:
                    res = maybe_single_safe(supabase.table('projects').select('id, organization_id').ilike('name', label).eq('is_current', True))
                    if res and res.data:
                        proj_candidates.append({
                            'id': res.data['id'],
                            'org_id': res.data.get('organization_id')
                        })
                except Exception:
                    pass
        
        return org_candidates, proj_candidates

    except Exception as e:
        audit_log_sync("pulse", "WARNING", f"⚠️ Entity extraction failed for {source_id}: {e}")
        return [], []
