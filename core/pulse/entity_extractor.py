from core.llm.constants import CLASSIFICATION_MODEL
from core.lib.audit_logger import audit_log_sync
from core.services.db import get_supabase, maybe_single_safe
from core.llm.fallback import generate_content_with_fallback
from core.llm.config import WorkloadProfile
from core.pulse.graph import insert_extracted_entities

supabase = get_supabase()

async def extract_and_link_entities(text: str, source_id: str, source_type: str = 'task'):
    """
    Real-time entity extraction using Gemini Flash Lite.
    Extracts entities and updates the graph immediately during ingestion.
    source_type: 'task', 'memory', or 'raw_dump'
    """
    # URL FILTER: Do not extract entities from text containing URLs
    import re
    if re.search(r'https?://', text, re.IGNORECASE):
        audit_log_sync("pulse", "INFO", "Skipped entity extraction: text contains URL")
        return [], []

    # Fetch known entities for prompt injection
    try:
        kn_res = supabase.table('graph_nodes').select('label, type').in_('type', ['person', 'organization', 'project', 'place', 'event', 'animal', 'emotional_state', 'concept']).neq('epistemic_status', 'hypothetical').execute()
        known_labels = [r['label'] for r in kn_res.data] if kn_res and kn_res.data else []
        known_str = ", ".join(known_labels[:50]) # limit to avoid huge prompts
    except Exception:
        known_str = "Danny, Mother, Qhord, Ashraya, Solvstrat, Rhodey OS"

    prompt = f"""Extract knowledge graph elements from this text.
    
Return a JSON object with:
- "nodes": array of objects with {{"label": string, "type": "person"|"organization"|"project"|"place"|"event"|"animal"|"emotional_state"|"concept"}}
- "edges": array of objects with {{"source": string, "target": string, "relationship": string}}
    
RULES:
- Only extract explicitly mentioned entities.
- Keep labels concise (e.g. "Danny", "Qhord").
- COMMON MISTAKES TO AVOID:
  - Use canonical names for known entities: "Danny" (not "I", "me", "user"), "Mother" (not "Amma", "amma").
  - Do not extract pronouns or generic terms ("he", "the project", "loops") as nodes.
- KNOWN ENTITIES (use exact spelling if referring to these): {known_str}
- AVOID COMBINING ENTITIES: Never combine an organization and a project into a single label. E.g. "Armour Cyber AI Gateway" must be split into "Armour Cyber" (organization) and "AI Gateway" (project).
- TYPE GUIDANCE:
  - "place": A physical location, venue, or geographic area (e.g. "St. Mary's Church", "Kakkanad office").
  - "event": A scheduled or past occurrence with a time/date (e.g. "Sunday service", "team standup").
  - "animal": Named or referenced pets, animals (e.g. "Max", "the stray cat").
  - "emotional_state": A feeling, mood, or emotional condition (e.g. "stressed", "excited", "overwhelmed").
  - "project": A named initiative with a defined goal and stakeholders.
    ✓ QHORD, Ashraya, Solvstrat, Rhodey OS
    ✗ "Church cash rotation incident" (event), "New Habit" (intention), "Journaling tool" (concept), "Call Marcus" (task)
    If it doesn't have a formal name someone would use to refer to an ongoing initiative — skip it.
- If no clear entities/relationships, return empty arrays.
- Normalize person names to First Last if obvious.
    
Text: "{text}"
"""
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
                    res = maybe_single_safe(supabase.table('projects').select('id, organization_id').ilike('name', label))
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
