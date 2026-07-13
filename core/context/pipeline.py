from typing import List, Optional
from core.context.schema import RetrievalItem, ContextResult
from core.context.config import StrategyConfig
from core.context.gates import apply_entity_grounding_gate
from core.lib.audit_logger import audit_log_sync
from core.lib.decision_audit import log_decision, DecisionStage, ReasonCode
from core.services.db import get_supabase

async def execute_context_strategy(
    query: str,
    strategy: StrategyConfig,
    active_project_id: Optional[int] = None,
    active_person_id: Optional[str] = None,
    extracted_entities: Optional[List[str]] = None
) -> ContextResult:
    """Execute a context retrieval strategy."""
    import re
    supabase = get_supabase()
    query_entities = list(extracted_entities or [])

    matched_items: List[RetrievalItem] = []
    seen_memory_ids: set = set()  # dedupe between semantic + keyword passes
    query_terms = set(re.findall(r'\b\w{3,}\b', query.lower()))

    # 0. Resolve Anchors (Graph Nodes)
    # Load all person/org/project labels once — reused for both anchor resolution
    # and memory entity extraction (Fix D: replaces fragile regex).
    known_node_labels: List[str] = []
    try:
        nodes_res = supabase.table('graph_nodes')\
            .select('label, type')\
            .in_('type', ['person', 'organization', 'project'])\
            .eq('is_current', True)\
            .execute()
        for n in (nodes_res.data or []):
            label_lower = n['label'].lower()
            known_node_labels.append(n['label'])
            if (label_lower in query.lower() or any(t in label_lower for t in query_terms)) and n['label'] not in query_entities:
                query_entities.append(n['label'])
                for t in query_terms:
                    if t in label_lower and t not in query_entities and t not in [e.lower() for e in query_entities]:
                        query_entities.append(t)
    except Exception as e:
        audit_log_sync("context_registry", "WARNING", f"Anchor resolution failed: {e}")

    # Pre-build a lowercased lookup for O(1) entity matching inside memory loop
    known_labels_lower = {lbl.lower(): lbl for lbl in known_node_labels}

    # 1. Fact Sources
    if "tasks" in strategy.fact_sources:
        try:
            tasks_res = supabase.table('tasks')\
                .select('id, title, status, priority, direction, committed_to')\
                .eq('is_current', True)\
                .not_.in_('status', ['done', 'cancelled'])\
                .text_search('title', query)\
                .limit(5)\
                .execute()
            for t in (tasks_res.data or []):
                # Re-append commitment tags and priority for richer context
                task_str = t['title']
                direction = t.get('direction')
                committed_to = t.get('committed_to', 'someone')
                priority = t.get('priority', 'important')
                if direction == 'waiting_on':
                    task_str += f" [WAITING ON: {committed_to}]"
                elif direction == 'outbound':
                    task_str += f" [OWED TO: {committed_to}]"
                task_str += f" ({priority}) [ID:{t['id']}]"
                matched_items.append(RetrievalItem(
                    item_id=f"task_{t['id']}",
                    content=task_str,
                    metadata=t,
                    score=1.0,
                    source="tasks"
                ))
        except Exception:
            pass

    matched_people_names = []
    if "people" in strategy.fact_sources:
        try:
            people_res = supabase.table('graph_nodes')\
                .select('id, label, metadata')\
                .eq('type', 'person')\
                .eq('is_current', True)\
                .execute()
            for p in (people_res.data or []):
                p_label_lower = p['label'].lower()
                if p_label_lower in query.lower() or any(t in p_label_lower for t in query_terms):
                    matched_people_names.append(p['label'])
                    # 2nd-hop: count task connections via directed edges
                    task_count_str = ""
                    try:
                        edge_res = supabase.table('graph_edges')\
                            .select('id', count='exact')\
                            .eq('source_node_id', p['id'])\
                            .in_('relationship', ['INVOLVES', 'WORKS_ON', 'ASSIGNED_TO'])\
                            .limit(3)\
                            .execute()
                        edge_count = edge_res.count if hasattr(edge_res, 'count') else len(edge_res.data or [])
                        if edge_count:
                            task_count_str = f": {edge_count} active task connection(s)"
                    except Exception:
                        pass
                    matched_items.append(RetrievalItem(
                        item_id=f"person_{p['id']}",
                        content=f"{p['label']}{task_count_str}",
                        metadata=p,
                        score=1.0,
                        source="people"
                    ))
        except Exception:
            pass

    if "emails" in strategy.fact_sources and matched_people_names:
        try:
            from datetime import datetime, timezone, timedelta
            seven_days_ago = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
            email_conditions = [f'sender_name.ilike.%{name}%' for name in matched_people_names[:3]]
            email_res = supabase.table('messages')\
                .select('sender_name, subject, created_at')\
                .eq('channel', 'email')\
                .gte('created_at', seven_days_ago)\
                .or_(','.join(email_conditions))\
                .order('created_at', desc=True)\
                .limit(3)\
                .execute()
            for i, e in enumerate(email_res.data or []):
                matched_items.append(RetrievalItem(
                    item_id=f"email_{i}",
                    content=f"From {e.get('sender_name', '?')}: {(e.get('subject', '')[:60])}",
                    metadata=e,
                    score=1.0,
                    source="emails"
                ))
        except Exception:
            pass

    # Meeting minutes / notes: keyword pass on extracted entity names.
    # Hybrid alongside semantic search — surfaces context the embedding threshold
    # might miss (e.g. IAM meeting minutes whose text is about architecture, not
    # about the literal meeting-title wording). Items are tagged with the matched
    # entity so the hard grounding gate keeps them (anchor overlap guaranteed).
    if "meeting_minutes" in strategy.fact_sources and query_entities:
        try:
            for ent in query_entities[:3]:
                mm_res = supabase.table('memories')\
                    .select('id, content, memory_type, created_at')\
                    .ilike('content', f'%{ent}%')\
                    .order('created_at', desc=True)\
                    .limit(4)\
                    .execute()
                for m in (mm_res.data or []):
                    if m['id'] in seen_memory_ids:
                        continue
                    seen_memory_ids.add(m['id'])
                    matched_items.append(RetrievalItem(
                        item_id=f"minutes_{m['id']}",
                        content=m.get('content', ''),
                        metadata={**m, 'entities': [ent]},
                        score=0.9,
                        source="meeting_minutes"
                    ))
        except Exception:
            pass

    semantic_skipped_no_anchor = False

    # 2. Semantic Search
    run_semantic = strategy.semantic_enabled
    if strategy.semantic_requires_anchor and not query_entities:
        run_semantic = False
        semantic_skipped_no_anchor = True

    if run_semantic:
        try:
            from core.retrieval.search import search_memories_compat
            # PRE_FLIGHT always uses the legacy vector path (match_memories_hybrid RPC)
            # so it can find ALL memories regardless of associative-retrieval indexing
            # status. New memories have their embedding column populated at creation
            # time (dispatch.py), but are often NOT yet present in retrieval_passages /
            # retrieval_phrase_nodes because the fire-and-forget asyncio.create_task
            # in schedule_index_memory does not survive Vercel serverless shutdown.
            # The legacy path queries the memories.embedding column directly via
            # pgvector — no indexing step required.
            # Other strategies (BRIEFING, HINDSIGHT, etc.) continue to use the
            # associative path for deep graph-traversal context.
            use_assoc = None if strategy.name != "PRE_FLIGHT" else False
            memories = await search_memories_compat(
                query_text=query,
                top_k=strategy.top_k,
                threshold=strategy.threshold,
                recency_weight=strategy.weights.recency,
                importance_weight=strategy.weights.importance,
                use_associative=use_assoc,
            )
            for m in (memories or []):
                # Fix D: Extract entities from memory content by matching against
                # known graph node labels (person/org/project) loaded during anchor
                # resolution above. This replaces the fragile \b[A-Z][a-z]+\b regex
                # which missed acronyms ("AI"), short names ("Sai"), mixed-case
                # ("Armour Cyber"), and produced false positives ("The", "So", "But").
                content_lower = m.get('content', '').lower()
                ents = [
                    canonical
                    for lbl_lower, canonical in known_labels_lower.items()
                    if lbl_lower in content_lower
                ]
                m['entities'] = list(set(ents))
                seen_memory_ids.add(m['id'])

                matched_items.append(RetrievalItem(
                    item_id=f"memory_{m['id']}",
                    content=m.get('content', ''),
                    metadata=m,
                    score=m.get('similarity', 0.5),
                    source="memories"
                ))
        except Exception as e:
            audit_log_sync("context_registry", "WARNING", f"Semantic search failed: {e}")

    # 3. Apply Gates
    kept, excluded, decisions = apply_entity_grounding_gate(matched_items, query_entities, strategy.gate_mode)

    # 4. Enforce top_k across blended results
    kept.sort(key=lambda x: x.score, reverse=True)
    gated_snapshot = list(kept)
    kept = kept[:strategy.top_k]

    # Items that passed gates but were cut by top_k
    top_k_cut = [item for item in gated_snapshot if item not in kept]

    # 5. Decision Audit Logging (structured for "/why" command)
    rejection_reasons = {}
    for d in decisions:
        if d.action == "reject":
            rejection_reasons[d.reason] = rejection_reasons.get(d.reason, 0) + 1

    decision_included = [
        {"id": item.item_id, "content": item.content, "score": item.score, "source": item.source}
        for item in kept
    ]
    decision_excluded = []
    for d in decisions:
        if d.action == "reject":
            matching = next((m for m in matched_items if m.item_id == d.item_id), None)
            if matching:
                decision_excluded.append({
                    "id": matching.item_id, "content": matching.content,
                    "score": matching.score, "source": matching.source,
                    "reason": d.reason
                })
    for item in top_k_cut:
        decision_excluded.append({
            "id": item.item_id, "content": item.content,
            "score": item.score, "source": item.source,
            "reason": ReasonCode.TOP_K_TRUNCATED
        })

    await log_decision(
        stage=DecisionStage.CONTEXT_REGISTRY,
        query_text=query,
        resolved_entities=query_entities,
        included_items=decision_included,
        excluded_items=decision_excluded,
        reason_codes=list(rejection_reasons.keys()) + ([ReasonCode.TOP_K_TRUNCATED] if top_k_cut else []),
        summary=f"Context for {strategy.name}: candidates={len(matched_items)} final={len(kept)}"
    )

    neutral_count = sum(1 for d in decisions if d.action == "neutral_keep")
    grounded_count = sum(1 for d in decisions if d.action == "grounded_keep")

    audit_log_sync("context_registry", "INFO", f"Context for {strategy.name}: candidates={len(matched_items)} final={len(kept)}", {
        "strategy": strategy.name,
        "threshold": strategy.threshold,
        "top_k": strategy.top_k,
        "gate_mode": strategy.gate_mode,
        "candidate_count": len(matched_items),
        "rejected_count": len(excluded) + len(top_k_cut),
        "final_count": len(kept),
        "neutral_keep_count": neutral_count,
        "grounded_keep_count": grounded_count,
        "rejection_reasons": rejection_reasons,
        "semantic_skipped_no_anchor": semantic_skipped_no_anchor,
        "top_k_cut": len(top_k_cut)
    })

    exclusion_reasons = {d.item_id: d.reason for d in decisions if d.action == "reject"}

    return ContextResult(
        matched_items=kept,
        excluded_items=excluded + top_k_cut,
        exclusion_reasons=exclusion_reasons,
        gate_decisions=decisions,
        ranking_features_used=["semantic", "recency", "importance"]
    )
