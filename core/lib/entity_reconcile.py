from typing import List, Dict, Any
from core.lib.entity_detector import DetectedEntity
from core.lib.audit_logger import audit_log_sync

def reconcile_entity_types(
    llm_nodes: List[Dict[str, Any]], 
    pattern_entities: List[DetectedEntity]
) -> List[Dict[str, Any]]:
    """
    Reconciles the LLM's semantic types against the deterministic detector's structural types.
    
    Returns a list of node dicts: {"label": str, "type": str, "type_conflict": bool, "source": str, "evidence": ...}
    """
    # Create normalized lookup for pattern entities
    pat_map = {}
    for pe in pattern_entities:
        key = pe.label.strip().lower()
        pat_map[key] = pe

    reconciled = []
    seen_labels = set()

    for node in llm_nodes:
        raw_label = node.get("label", "").strip()
        if not raw_label:
            continue
            
        key = raw_label.lower()
        if key in seen_labels:
            continue
        seen_labels.add(key)

        llm_type = node.get("type", "")
        evidence = node.get("evidence", "")

        pat_ent = pat_map.get(key)
        
        if pat_ent:
            # We have both LLM and pattern output for this entity.
            
            # Rule 1: DB-known entity wins absolutely.
            if pat_ent.db_id:
                reconciled.append({
                    "label": pat_ent.label,  # Use canonical capitalization
                    "type": pat_ent.type,
                    "source": "db",
                    "confidence": 1.0,
                    "db_id": pat_ent.db_id
                })
                continue
                
            # Rule 2: Agreement -> Accept at high confidence.
            if pat_ent.type == llm_type:
                reconciled.append({
                    "label": raw_label,
                    "type": llm_type,
                    "source": "llm+patterns",
                    "confidence": 0.9,
                    "evidence": evidence
                })
                continue
                
            # Rule 3: Disagreement -> Conflict. Co-signer rule enforces a pending route.
            # We trust the LLM type as the suggestion because it reads full context,
            # but mark it as a conflict so the pipeline requires human approval.
            audit_log_sync(
                "entity_reconcile", "INFO",
                f"Type conflict on {raw_label!r}: LLM says {llm_type}, pattern says {pat_ent.type}. Routing as pending."
            )
            reconciled.append({
                "label": raw_label,
                "type": llm_type,
                "type_conflict": True, # This flag will trigger co-signer / pending route
                "source": "llm (conflict)",
                "confidence": 0.5,
                "evidence": f"LLM: {llm_type} ({evidence}) | Pattern: {pat_ent.type}"
            })
        else:
            # LLM only (pattern missed it).
            # The pattern pass enforces structural sanity. If pattern completely missed it,
            # it might be a hallucination or a novel entity.
            reconciled.append({
                "label": raw_label,
                "type": llm_type,
                "type_conflict": True, # One source only (uncorroborated) -> needs co-signer (pending)
                "source": "llm_only",
                "confidence": 0.6,
                "evidence": evidence
            })

    # Add any pattern entities that the LLM completely missed.
    for key, pat_ent in pat_map.items():
        if key not in seen_labels:
            reconciled.append({
                "label": pat_ent.label,
                "type": pat_ent.type,
                "source": "db" if pat_ent.db_id else "patterns_only",
                "confidence": pat_ent.confidence,
                "type_conflict": not bool(pat_ent.db_id), # Uncorroborated pattern -> pending if not DB
                "db_id": pat_ent.db_id
            })

    return reconciled
