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

        # ── Guard B: 3-layer text-anchoring validation (hardened) ──
        # Drops hallucinated nodes while preserving valid entities whose label
        # differs slightly from the source text (e.g., "FC Madras Website" in
        # "FC Madras website project").
        #
        # Layer 1: Verbatim match — exact label appears in text
        # Layer 2: Strip trailing noise words (project, website, etc.) then re-check
        # Layer 3: Token-level n-gram overlap of non-noise words
        text_lower = text.lower()
        text_words = text_lower.split()

        # Noise words that are generic project/organization descriptors
        # Stripped from the END of labels to find the meaningful core
        _PROJECT_NOISE_WORDS = {
            'project', 'website', 'web', 'app', 'application', 'platform',
            'page', 'site', 'service', 'system', 'product',
            'tool', 'task', 'note', 'portal', 'dashboard', 'panel',
            'hub', 'suite', 'manager', 'management', 'tracker',
        }

        def _is_text_anchored(label: str) -> bool:
            """3-layer text anchoring check.

            Returns True if the label is grounded in the source text.
            """
            label_lower = label.lower()

            # Layer 1: Verbatim match (strict)
            if label_lower in text_lower or label_lower == 'danny':
                return True

            # Layer 2: Iteratively strip trailing noise words, then re-check verbatim
            # Handles: "FC Madras Website" -> strip "website" -> "FC Madras" is verbatim
            words = label_lower.split()
            while words and words[-1] in _PROJECT_NOISE_WORDS:
                words = words[:-1]
                if words:
                    stripped = ' '.join(words)
                    if stripped in text_lower:
                        return True

            # Layer 3: Token-level n-gram check for remaining non-noise words
            # Handles cases where noise-stripped words appear as a contiguous
            # block but weren't caught by verbatim (e.g., different spacing/case)
            if len(words) >= 2:
                for i in range(len(text_words) - len(words) + 1):
                    if text_words[i:i+len(words)] == words:
                        return True

            return False

        valid_nodes = []
        dropped_labels = []
        for n in nodes:
            label = n.get("label", "") if isinstance(n, dict) else ""
            if not label:
                continue
            if _is_text_anchored(label):
                valid_nodes.append(n)
            else:
                dropped_labels.append(label)

        if dropped_labels:
            audit_log_sync("pulse", "INFO",
                f"Text-anchoring guard (hardened): dropped {len(dropped_labels)} hallucinated node(s): "
                f"{', '.join(dropped_labels[:5])} from {source_type}:{source_id}")

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
