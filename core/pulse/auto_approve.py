from core.services.db import get_supabase, maybe_single_safe
from core.pulse.graph import create_graph_node_with_db_record
from core.lib.graph_rules import resolve_canonical_label
from core.lib.audit_logger import audit_log_sync
from core.decisions import record_decision
from core.pulse.graph import process_pending_edge_decision


async def auto_approve_concepts_and_evokes(label: str):
    """
    Cascade auto-approves EVOKES edges referencing the entity label, 
    and auto-creates the connected concept nodes if needed.
    """
    supabase = get_supabase()
    
    # 1. Get live UUID of the entity we just approved
    entity_res = maybe_single_safe(supabase.table('graph_nodes').select('id').eq('label', label))
    if not entity_res or not entity_res.data:
        return
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
        concept_res = maybe_single_safe(supabase.table('graph_nodes').select('id').eq('label', concept_label))
        if concept_res and concept_res.data:
            concept_uuid = concept_res.data['id']
        else:
            # Create concept node from pending data
            pn_res = maybe_single_safe(supabase.table('pending_graph_nodes').select('*').eq('label', concept_label).eq('type', 'concept'))
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
                c_res = maybe_single_safe(supabase.table('graph_nodes').select('id').eq('label', concept_label))
                if c_res and c_res.data:
                    concept_uuid = c_res.data['id']
                else:
                    continue
            
            # Mark pending concept as approved
            supabase.table('pending_graph_nodes').update({'status': 'approved'}).eq('label', concept_label).eq('type', 'concept').execute()
            
            # Record decision for auto-created concept node
            try:
                record_decision(
                    decision_type="concept_auto_creation",
                    title=f"Auto-created concept: {concept_label}",
                    context=f"Cascade from approving entity '{label}' — EVOKES relationship",
                    entity_type="graph_node",
                    entity_id=str(concept_uuid),
                    confidence=1.0,
                    source="auto_approve_cascade",
                    auto_decided=True,
                )
            except Exception as dec_err:
                audit_log_sync("pulse", "WARNING", f"Failed to record concept creation decision: {dec_err}")
        
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
                node_res = maybe_single_safe(supabase.table('graph_nodes').select('metadata').eq('id', concept_uuid))
                existing_meta = (node_res.data.get('metadata') if node_res and node_res.data else {}) or {}
                
                supabase.table('graph_nodes').update({
                    "metadata": {
                        **existing_meta,
                        "raw_linked_entity": linked_entity,
                        "resolution_failed": True
                    }
                }).eq('id', concept_uuid).execute()
            except Exception as e:
                audit_log_sync("pulse", "WARNING", f"Failed to store raw linked_entity fallback: {e}")
            
        # 5 & 6. Create edge and mark approved via canonical promotion path
        await process_pending_edge_decision(
            pending_id=edge['id'],
            decision='approve',
            new_rel=relationship_verb if relationship_verb != 'EVOKES' else None,
            auto_decided=True
        )

