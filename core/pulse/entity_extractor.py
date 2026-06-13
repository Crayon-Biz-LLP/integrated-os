from core.lib.audit_logger import audit_log_sync
from core.services.db import get_supabase
from core.llm.fallback import generate_content_with_fallback
from core.llm.config import WorkloadProfile

supabase = get_supabase()

async def extract_and_link_entities(text: str, source_id: str, source_type: str = 'task'):
    """
    Real-time entity extraction using Gemini Flash Lite.
    Extracts entities and updates the graph immediately during ingestion.
    source_type: 'task', 'memory', or 'raw_dump'
    """
    prompt = f"""Extract knowledge graph elements from this text.
    
Return a JSON object with:
- "nodes": array of objects with {{"label": string, "type": "person"|"organization"|"project"|"emotional_state"|"concept"}}
- "edges": array of objects with {{"source": string, "target": string, "relationship": string}}
    
RULES:
- Only extract explicitly mentioned entities.
- Keep labels concise (e.g. "Danny", "Qhord").
- PROJECT DEFINITION: A named initiative with a defined goal and stakeholders.
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
            primary_model="gemini-3.1-flash-lite",
            config={'response_mime_type': 'application/json'}
        )
        if not response or not response.text:
            return

        data = response.parse_json()
        nodes = data.get("nodes", [])
        edges = data.get("edges", [])
        
        if not nodes and not edges:
            return
            
        # 1. Ensure source node exists (e.g. the task or memory itself)
        source_label = f"{source_type.capitalize()}_{source_id}"
        source_node_res = supabase.table('graph_nodes') \
            .select('id') \
            .eq('type', source_type) \
            .filter(f'metadata->>{source_type}_id', 'eq', str(source_id)) \
            .maybe_single() \
            .execute()
            
        if source_node_res and source_node_res.data:
            root_node_id = source_node_res.data['id']
        else:
            new_node = supabase.table('graph_nodes').insert({
                "label": source_label,
                "type": source_type,
                "metadata": {f"{source_type}_id": source_id, "source": "entity_extractor"}
            }).execute()
            root_node_id = new_node.data[0]['id']

        # 2. Process extracted nodes
        node_id_map = {}
        for node in nodes:
            label = node.get('label')
            n_type = node.get('type', 'concept')
            if not label:
                continue

            # GUARD 2: Entity Grounding for projects
            if n_type == 'project':
                proj_check = supabase.table('projects').select('id').ilike('name', label.strip()).execute()
                if not proj_check.data:
                    audit_log_sync("pulse", "WARNING", f"Skipped ungrounded project node: {label}")
                    continue
                
            existing_res = supabase.table('graph_nodes').select('id').eq('label', label).maybe_single().execute()
            if existing_res and existing_res.data:
                node_id_map[label] = existing_res.data['id']
            else:
                # GUARD 3: Entity Grounding check for other types
                status = "pending"
                if n_type == 'person':
                    p_check = supabase.table('people').select('id').ilike('name', label.strip()).execute()
                    if not p_check.data:
                        status = "flagged"
                        
                # Instead of inserting to graph_nodes immediately, if it's flagged, send to pending and skip edge
                if status == "flagged":
                    try:
                        # Only insert if it doesn't already exist in pending
                        pend_check = supabase.table('pending_graph_nodes').select('id').eq('label', label).maybe_single().execute()
                        if not pend_check.data:
                            supabase.table('pending_graph_nodes').insert({
                                "label": label,
                                "type": n_type,
                                "source_text": f"{source_type}:{source_id}",
                                "status": "flagged"
                            }).execute()
                    except Exception:
                        pass
                    continue # Skip edge creation because node isn't in graph_nodes yet
                
                ins_res = supabase.table('graph_nodes').insert({
                    "label": label,
                    "type": n_type,
                    "metadata": {"source": "entity_extractor"}
                }).execute()
                node_id_map[label] = ins_res.data[0]['id']
                
            # Link extracted node to the source (task/memory)
            supabase.table('graph_edges').insert({
                "source_node_id": root_node_id,
                "target_node_id": node_id_map[label],
                "relationship": "MENTIONS",
                "weight": 1.0,
                "metadata": {"source": "entity_extractor"}
            }).execute()

        # 3. Process extracted edges between the nodes
        for edge in edges:
            source = edge.get('source')
            target = edge.get('target')
            rel = edge.get('relationship', 'RELATED_TO').upper()
            
            if source in node_id_map and target in node_id_map:
                s_id = node_id_map[source]
                t_id = node_id_map[target]
                
                ext_edge = supabase.table('graph_edges') \
                    .select('id') \
                    .eq('source_node_id', s_id) \
                    .eq('target_node_id', t_id) \
                    .eq('relationship', rel) \
                    .maybe_single() \
                    .execute()
                    
                if not ext_edge or not ext_edge.data:
                    supabase.table('graph_edges').insert({
                        "source_node_id": s_id,
                        "target_node_id": t_id,
                        "relationship": rel,
                        "weight": 1.0,
                        "metadata": {"source": "entity_extractor"}
                    }).execute()
                    
        print(f"🕸️ Real-time entities extracted for {source_type} {source_id}: {len(nodes)} nodes, {len(edges)} edges")
    except Exception as e:
        audit_log_sync("pulse", "WARNING", f"⚠️ Entity extraction failed for {source_id}: {e}")
