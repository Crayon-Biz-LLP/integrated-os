from core.services.db import get_supabase
from core.pulse.graph import create_graph_node_with_db_record
from core.lib.graph_rules import resolve_canonical_label
from core.lib.audit_logger import audit_log_sync

async def auto_approve_concepts_and_evokes(label: str):
    """
    Cascade auto-approves EVOKES edges referencing the entity label, 
    and auto-creates the connected concept nodes if needed.
    """
    supabase = get_supabase()
    
    # 1. Get live UUID of the entity we just approved
    entity_res = supabase.table('graph_nodes').select('id').eq('label', label).maybe_single().execute()
    if not entity_res or not entity_res.data:
        return
    entity_uuid = entity_res.data['id']
    
    # 2. Find pending EVOKES edges referencing this label
    edges_res = supabase.table('pending_graph_edges').select('*')\
        .eq('relationship', 'EVOKES')\
        .eq('status', 'pending')\
        .or_(f"source_label.eq.{label},target_label.eq.{label}")\
        .execute()
        
    if not edges_res or not edges_res.data:
        return
        
    for edge in edges_res.data:
        is_source = edge['source_label'] == label
        concept_label = edge['target_label'] if is_source else edge['source_label']
        
        # Initialize defaults before branching
        context = {}
        source_text = ""
        concept_uuid = None

        # 3. Ensure the concept node exists in graph_nodes
        concept_res = supabase.table('graph_nodes').select('id').eq('label', concept_label).maybe_single().execute()
        if concept_res and concept_res.data:
            concept_uuid = concept_res.data['id']
        else:
            # Create concept node from pending data
            pn_res = supabase.table('pending_graph_nodes').select('*').eq('label', concept_label).eq('type', 'concept').maybe_single().execute()
            if pn_res and pn_res.data:
                source_text = pn_res.data.get('source_text', '')
                context = pn_res.data.get('eval_context', {})
            
            res = await create_graph_node_with_db_record(
                label=concept_label,
                node_type='concept',
                source_text=source_text,
                context=context,
                source_tag="auto_approve_cascade",
                force=True
            )
            if not res.get('success'):
                continue
                
            concept_uuid = res.get('node_id') or res.get('merge_candidate_id')
            if not concept_uuid:
                c_res = supabase.table('graph_nodes').select('id').eq('label', concept_label).maybe_single().execute()
                if c_res and c_res.data:
                    concept_uuid = c_res.data['id']
                else:
                    continue
            
            # Mark pending concept as approved
            supabase.table('pending_graph_nodes').update({'status': 'approved'}).eq('label', concept_label).eq('type', 'concept').execute()
        
        # 4. Extract linked_entity and relationship from context
        linked_entity = context.get('linked_entity', '') if isinstance(context, dict) else ''
        relationship_verb = context.get('relationship', '') or 'EVOKES'
        
        # Resolve linked_entity if exists
        resolved_label = None
        if linked_entity:
            try:
                resolved_label = resolve_canonical_label(linked_entity)
                if resolved_label and resolved_label.get('confidence', 0) > 0:
                    linked_entity = resolved_label['label']
            except Exception as e:
                audit_log_sync("pulse", "WARNING", f"Failed to resolve linked_entity: {e}")
        
        # Store raw linked_entity as fallback metadata when resolution fails
        if linked_entity and not resolved_label and concept_uuid:
            try:
                supabase.table('graph_nodes').update({
                    "metadata": {
                        "raw_linked_entity": linked_entity,
                        "resolution_failed": True
                    }
                }).eq('id', concept_uuid).execute()
            except Exception as e:
                audit_log_sync("pulse", "WARNING", f"Failed to store raw linked_entity fallback: {e}")
            
        # 5. Create the edge in graph_edges
        source_uuid = entity_uuid if is_source else concept_uuid
        target_uuid = concept_uuid if is_source else entity_uuid
        
        # Check if edge already exists
        exist_edge = supabase.table('graph_edges').select('id')\
            .eq('source_node_id', source_uuid)\
            .eq('target_node_id', target_uuid)\
            .eq('relationship', relationship_verb)\
            .execute()
            
        if not exist_edge.data:
            supabase.table('graph_edges').insert({
                'source_node_id': source_uuid,
                'target_node_id': target_uuid,
                'relationship': relationship_verb,
                'weight': 1.0,
                'metadata': {"source": "auto_approve_cascade"}
            }).execute()
            
        # 6. Mark pending edge as approved
        supabase.table('pending_graph_edges').update({'status': 'approved'}).eq('id', edge['id']).execute()

