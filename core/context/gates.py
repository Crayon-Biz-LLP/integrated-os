from typing import List, Tuple
from core.context.schema import RetrievalItem, GateDecision

def apply_entity_grounding_gate(
    items: List[RetrievalItem],
    query_entities: List[str],
    gate_mode: str
) -> Tuple[List[RetrievalItem], List[RetrievalItem], List[GateDecision]]:
    """Applies entity grounding gates to retrieval items.
    
    If gate_mode == 'hard': reject items with named entities that aren't in query_entities.
    If gate_mode == 'soft': downrank them.
    If gate_mode == 'none': do nothing.
    """
    if gate_mode == "none":
        return items, [], []
        
    kept = []
    excluded = []
    decisions = []
    
    query_entities_lower = [e.lower() for e in query_entities]
    
    for item in items:
        item_entities = item.metadata.get('entities', [])
        
        # If the item has NO recognized entities, it's neutral context.
        if not item_entities:
            # Neutral context shouldn't dominate grounded context.
            # Apply a 50% penalty to neutral items so they fall below anchored items.
            item.score *= 0.5
            decisions.append(GateDecision("entity_grounding", "neutral_keep", "No entities in item (downranked)", item.item_id))
            kept.append(item)
            continue
            
        # Check for overlap
        has_overlap = False
        for ent in item_entities:
            if ent.lower() in query_entities_lower:
                has_overlap = True
                break
                
        if not has_overlap:
            if gate_mode == "hard":
                decisions.append(GateDecision("entity_grounding", "reject", f"No anchor overlap (item has {item_entities})", item.item_id))
                excluded.append(item)
            elif gate_mode == "soft":
                item.score *= 0.5  # Downrank
                decisions.append(GateDecision("entity_grounding", "downrank", f"No anchor overlap (item has {item_entities})", item.item_id))
                kept.append(item)
        else:
            decisions.append(GateDecision("entity_grounding", "grounded_keep", "Anchor overlap found", item.item_id))
            kept.append(item)
            
    # Re-sort kept items by score
    kept.sort(key=lambda x: x.score, reverse=True)
    return kept, excluded, decisions

