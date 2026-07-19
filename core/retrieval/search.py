import hashlib
from core.lib.redis_cache import cache_get, cache_set

import asyncio
from typing import List, Optional, Dict
import time
from datetime import datetime, timezone
from core.services.db import get_supabase
from core.retrieval.config import (
    config, DEFAULT_TOP_K_PHRASES, DEFAULT_TOP_K_MEMORIES, RECOGNITION_THRESHOLD,
)
from core.retrieval.normalizer import expand_shorthand, is_noise_phrase
from core.retrieval.ppr import build_adjacency_from_edges, personalized_pagerank, normalize_scores
from core.retrieval.ranking import rank_memories
from core.retrieval.schema import ExplainableBundle, ScoredMemory

_MAX_SUPPORTING_PASSAGES = 5

supabase = get_supabase()


async def associative_retrieve(
    query: str,
    top_k: int = DEFAULT_TOP_K_MEMORIES,
    active_project_id: Optional[int] = None,
    active_person_id: Optional[str] = None,
    retrieval_mode: str = "blended",
) -> ExplainableBundle:
    """Main retrieval pipeline.
    
    Pipeline:
    1. Parse query → extract query phrases.
    2. Retrieve candidate phrase nodes + passages + triples.
    3. Recognition filter (discard weak candidates).
    4. Seed retrieval graph with surviving nodes.
    5. Run Personalized PageRank.
    6. Aggregate PPR scores to passages → memories.
    7. Blended ranking with semantic, recency, importance, project/person boosts.
    8. Bundle assembly with dedup and explanation.
    """
    start = time.time()
    debug = {}

    # 1. Parse query, fetch embedding, and search lexical phrases concurrently
    
    query_norm = query.lower().strip()
    query_hash = hashlib.sha256(query_norm.encode()).hexdigest()
    ent_key = f"retrieval:entities:{query_hash}"
    emb_key = f"retrieval:embedding:{query_hash}"

    async def _get_cached_entities():
        res = await asyncio.to_thread(cache_get, ent_key)
        if res is not None:
            return res
        ents = await _extract_query_entities(query)
        if ents:
            await asyncio.to_thread(cache_set, ent_key, ents, 3600)
        return ents or []
        
    async def _get_cached_embedding():
        res = await asyncio.to_thread(cache_get, emb_key)
        if res is not None:
            return res
        from core.llm import get_embedding as _get_embedding
        emb = await _get_embedding(query)
        vec = emb.vector if emb else None
        if vec:
            await asyncio.to_thread(cache_set, emb_key, vec, 86400)
        return vec

    llm_task = asyncio.create_task(_get_cached_entities())
    emb_task = asyncio.create_task(_get_cached_embedding())

    lex_phrases = _parse_query(query) or []
    
    def fetch_lex_candidates():
        return _retrieve_phrase_candidates(lex_phrases, top_k=30)
    lex_cand_task = asyncio.create_task(asyncio.to_thread(fetch_lex_candidates))

    llm_phrases = await llm_task
    query_emb = await emb_task

    # Phase 3: if chunk_enrichment is on, re-embed query with entity prefix
    # to align with enriched passage embeddings. Uses a generic [retrieval]
    # prefix so the query embedding space is source_type-agnostic.
    if config.chunk_enrichment and llm_phrases:
        entity_part = ", ".join(p.lower() for p in llm_phrases[:3])
        enriched_query = f"[retrieval, {entity_part}] {query}"
        try:
            from core.llm import get_embedding as _get_embedding2
            enriched_emb = await _get_embedding2(enriched_query)
            if enriched_emb and enriched_emb.vector:
                query_emb = enriched_emb.vector
        except Exception:
            pass  # Fall back to raw query embedding

    lex_candidates = await lex_cand_task
    
    new_llm_phrases = [p for p in llm_phrases if p not in lex_phrases]
    if new_llm_phrases:
        def fetch_llm_candidates():
            return _retrieve_phrase_candidates(new_llm_phrases, top_k=10)
        llm_candidates = await asyncio.to_thread(fetch_llm_candidates)
    else:
        llm_candidates = []
        
    all_candidates = lex_candidates + llm_candidates
    seen_ids = set()
    phrase_nodes = []
    for c in all_candidates:
        if c["id"] not in seen_ids:
            seen_ids.add(c["id"])
            phrase_nodes.append(c)
            
    phrase_nodes.sort(key=lambda x: x.get("similarity", 0), reverse=True)
    phrase_nodes = phrase_nodes[:DEFAULT_TOP_K_PHRASES]

    query_phrases = list(set(llm_phrases + lex_phrases))
    debug["query_phrases"] = "[REDACTED]"
    debug["llm_phrases"] = "[REDACTED]"
    debug["lex_phrases"] = "[REDACTED]"

    if not phrase_nodes:
        return ExplainableBundle(query=query, items=[], latency_ms=int((time.time() - start) * 1000))

    # 3. Recognition filter
    filtered_nodes = _recognition_filter(phrase_nodes, query_phrases)
    debug["filtered_nodes"] = len(filtered_nodes)

    if not filtered_nodes:
        return ExplainableBundle(query=query, items=[], latency_ms=int((time.time() - start) * 1000))

    # 4. Build adjacency and seed PPR
    seed_nodes = {n["id"]: n.get("similarity", 0.5) for n in filtered_nodes}
    
    edges = await _fetch_subgraph_edges(list(seed_nodes.keys()))
    debug["subgraph_edges"] = len(edges)

    if not edges:
        return ExplainableBundle(query=query, items=[], latency_ms=int((time.time() - start) * 1000))

    # 5. Run PPR
    adjacency = build_adjacency_from_edges(edges)
    ppr_raw = personalized_pagerank(adjacency, seed_nodes)
    ppr_norm = normalize_scores(ppr_raw)
    debug["ppr_nodes"] = len(ppr_norm)

    # 6. Aggregate PPR → passages → memories
    memory_scores, passage_ids = await _aggregate_to_memories(ppr_norm, list(seed_nodes.keys()))
    debug["memory_candidates"] = len(memory_scores)

    if not memory_scores:
        return ExplainableBundle(query=query, items=[], latency_ms=int((time.time() - start) * 1000))

    # Filter expired memories before ranking
    try:
        now_iso = datetime.now(timezone.utc).isoformat()
        expired_res = supabase.table("memories") \
            .select("id") \
            .in_("id", list(memory_scores.keys())) \
            .lt("expires_at", now_iso) \
            .execute()
        expired_ids = {r["id"] for r in (expired_res.data or [])}
        if expired_ids:
            memory_scores = {k: v for k, v in memory_scores.items() if k not in expired_ids}
            debug["expired_filtered"] = len(expired_ids)
    except Exception:
        pass

    if not memory_scores:
        return ExplainableBundle(query=query, items=[], latency_ms=int((time.time() - start) * 1000))

    # 7. Blended ranking
    memory_ids = list(memory_scores.keys())
    
    meta_task = asyncio.create_task(asyncio.to_thread(_fetch_memory_metadata_boosts, memory_ids, active_project_id))
    spec_task = asyncio.create_task(_compute_specificity_boost(list(seed_nodes.keys()), passage_ids))
    sem_task = asyncio.create_task(asyncio.to_thread(_compute_semantic_scores, memory_ids, query_emb))
    
    person_task = None
    if active_person_id:
        person_task = asyncio.create_task(asyncio.to_thread(_compute_person_boost, memory_ids, active_person_id))

    (recency_boost, importance_boost, project_boost), specificity_boost, semantic_scores = await asyncio.gather(
        meta_task, spec_task, sem_task
    )
    
    # Ensure specificity boost covers all memory IDs
    specificity_boost = {m: specificity_boost.get(m, 0.5) for m in memory_ids}
    
    person_boost = await person_task if person_task else {}

    ranked = rank_memories(
        memory_scores=memory_scores,
        ppr_scores=memory_scores,
        semantic_scores=semantic_scores,
        specificity_boost=specificity_boost,
        recency_boost=recency_boost,
        importance_boost=importance_boost,
        project_boost=project_boost,
        person_boost=person_boost,
    )

    top_memories = ranked[:top_k]

    # 8. Bundle assembly
    def assemble_b():
        return _assemble_bundles(top_memories, ppr_norm, list(seed_nodes.keys()))
    items = await asyncio.to_thread(assemble_b)

    latency = int((time.time() - start) * 1000)

    return ExplainableBundle(
        query=query,
        items=items,
        total_candidates=len(ranked),
        latency_ms=latency,
        debug_trace=debug if config.debug_explanations else None,
        blended=(retrieval_mode == "blended"),
    )


async def _extract_query_entities(query: str) -> List[str]:
    """Extract entities from query using deterministic entity resolution.

    Uses entity_resolver.py (n-gram + substring ILIKE matching) instead of an
    LLM call — no probabilistic failure, no latency, no token cost.
    Returns empty list if no known entities found (caller falls back to lexical).
    """
    try:
        from core.pulse.entity_resolver import resolve_entities_from_text
        org_id, proj_id, reason = resolve_entities_from_text(query)
        if "no_matches" in reason:
            return []
        # Return entity labels from the resolved IDs
        labels = []
        supabase = get_supabase()
        if org_id:
            org = supabase.table('organizations').select('name').eq('id', org_id).maybe_single().execute()
            if org and org.data:
                labels.append(org.data['name'])
        if proj_id:
            proj = supabase.table('projects').select('name').eq('id', proj_id).eq('is_current', True).maybe_single().execute()
            if proj and proj.data:
                labels.append(proj.data['name'])
        return labels
    except Exception:
        return []


def _parse_query(query: str) -> List[str]:
    """Extract candidate phrases from a natural language query."""
    normalized = expand_shorthand(query.lower())
    phrases = set()
    words = normalized.split()

    for w in words:
        w = w.strip(",.!?;:'\"")
        if not is_noise_phrase(w) and len(w) >= 3:
            phrases.add(w)

    if not phrases:
        return []

    return list(phrases)


_TSQUERY_SPECIAL = set('&|!()<>')


def _build_tsquery(phrases: List[str]) -> str:
    """Build a tsquery string from phrases using OR.

    Splits multi-word phrases into individual tokens and joins with OR.
    Result is a valid tsquery input for to_tsquery('simple', ...).
    Skips words containing tsquery operators to prevent syntax errors.
    """
    words = []
    for phrase in phrases:
        for word in phrase.lower().split():
            clean = word.strip(',.!?;:\'\'"')
            if clean and len(clean) >= 2 and not any(c in clean for c in _TSQUERY_SPECIAL):
                words.append(clean)
    # Deduplicate preserving order
    seen = set()
    unique = []
    for w in words:
        if w not in seen:
            seen.add(w)
            unique.append(w)
    return " | ".join(unique)


def _retrieve_phrase_candidates(phrases: List[str], top_k: int = 30) -> List[dict]:
    """Retrieve candidate phrase nodes using Postgres full-text search (tsvector)."""
    if not phrases:
        return []

    tsquery = _build_tsquery(phrases)
    if not tsquery:
        return []

    candidates = []
    seen = set()

    try:
        res = supabase.rpc('search_phrase_nodes', {
            'query_text': tsquery,
            'result_limit': top_k * 2,
        }).execute()

        if res and res.data:
            for row in res.data:
                nid = row["id"]
                if nid not in seen:
                    seen.add(nid)
                    row["similarity"] = row.get("rank", 0.5)
                    candidates.append(row)
    except Exception as e:
        from core.lib.audit_logger import audit_log_sync
        audit_log_sync("retrieval", "WARNING",
                       f"_retrieve_phrase_candidates tsvector search failed: {e}")

    candidates.sort(key=lambda x: x.get("similarity", 0), reverse=True)
    return candidates[:top_k]


def _recognition_filter(candidates: List[dict], query_phrases: List[str]) -> List[dict]:
    """Filter out weak candidates that are unlikely to be relevant.
    
    Keeps candidates where:
    - The normalized_text directly contains a query phrase, OR
    - The node has some similarity signal.
    """
    filtered = []
    for c in candidates:
        text = c.get("normalized_text", "").lower()
        score = c.get("similarity", 0.0)

        if score >= RECOGNITION_THRESHOLD:
            filtered.append(c)
            continue

        if any(qp in text for qp in query_phrases):
            filtered.append(c)
            continue

        if c.get("node_type") in ("person", "project", "organization") and len(text) >= 3:
            filtered.append(c)
            continue

    return filtered


async def _fetch_subgraph_edges(node_ids: List[int]) -> List[tuple]:
    """Fetch edges connecting the seeded nodes (both directions)."""
    if not node_ids:
        return []

    id_csv = ",".join(map(str, node_ids))
    or_filter = f"from_node_id.in.({id_csv}),to_node_id.in.({id_csv})"

    edges_result, alias_result = await asyncio.gather(
        asyncio.to_thread(
            lambda: supabase.table("retrieval_edges") \
                .select("from_node_id, to_node_id, weight") \
                .or_(or_filter) \
                .limit(5000) \
                .execute()
        ),
        asyncio.to_thread(
            lambda: supabase.table("retrieval_alias_edges") \
                .select("from_node_id, to_node_id, weight") \
                .in_("from_node_id", node_ids) \
                .execute()
        )
    )

    edges = []
    for row in (edges_result.data or []):
        edges.append((row["from_node_id"], row["to_node_id"], row.get("weight", 1.0)))
        
    for row in (alias_result.data or []):
        edges.append((row["from_node_id"], row["to_node_id"], row.get("weight", 0.8)))

    return edges


async def _aggregate_to_memories(ppr_scores: Dict[int, float], seed_node_ids: List[int]) -> tuple[Dict[int, float], List[int]]:
    """Aggregate PPR scores from phrase nodes back to memory IDs via passage links."""
    if not ppr_scores or not seed_node_ids:
        return {}, []

    result = await asyncio.to_thread(
        lambda: supabase.table("retrieval_passage_phrase_links") \
            .select("node_id, passage_id, retrieval_passages!inner(id, retrieval_memory_bundle_links!inner(memory_id))") \
            .in_("node_id", seed_node_ids) \
            .limit(2000) \
            .execute()
    )

    passage_scores: Dict[int, float] = {}
    for row in (result.data or []):
        nid = row.get("node_id")
        pid = row.get("passage_id")
        score = ppr_scores.get(nid, 0.0)
        if pid:
            passage_scores[pid] = max(passage_scores.get(pid, 0.0), score)

    memory_scores = {}
    for row in (result.data or []):
        pid = row.get("passage_id")
        if not pid: 
            continue
        passage_obj = row.get("retrieval_passages") or {}
        for bundle_row in (passage_obj.get("retrieval_memory_bundle_links") or []):
            mid = bundle_row.get("memory_id")
            if mid:
                memory_scores[mid] = max(memory_scores.get(mid, 0.0), passage_scores[pid])

    return memory_scores, list(passage_scores.keys())


def _cosine_similarity(a: list, b: list) -> float:
    """Compute cosine similarity between two vectors."""
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    if na < 1e-10 or nb < 1e-10:
        return 0.0
    return dot / (na * nb)


def _fetch_memory_metadata_boosts(memory_ids: List[int], active_project_id: Optional[int]) -> tuple[Dict[int, float], Dict[int, float], Dict[int, float]]:
    """Fetch recency, importance, and project boosts in a single query."""
    recency = {m: 0.0 for m in memory_ids}
    importance = {m: 0.5 for m in memory_ids}
    project = {m: 0.0 for m in memory_ids}
    
    if not memory_ids:
        return recency, importance, project
        
    try:
        res = supabase.table("memories") \
            .select("id, created_at, importance_score, project_id") \
            .in_("id", memory_ids) \
            .execute()
            
        if not res or not res.data:
            return recency, importance, project
            
        now = datetime.now(timezone.utc)
        for row in res.data:
            mid = row["id"]
            
            created = row.get("created_at")
            if created:
                if isinstance(created, str):
                    created = datetime.fromisoformat(created.replace("Z", "+00:00"))
                days_old = max(0, (now - created).total_seconds() / 86400.0)
                recency[mid] = max(0.0, 1.0 - days_old / 90.0)
                
            importance[mid] = (row.get("importance_score", 5) or 5) / 10.0
            
            if active_project_id and row.get("project_id") == active_project_id:
                project[mid] = 1.0
                
    except Exception as e:
        from core.lib.audit_logger import audit_log_sync
        audit_log_sync("retrieval", "WARNING", f"_fetch_memory_metadata_boosts failed: {e}")
        
    return recency, importance, project

def _compute_semantic_scores(memory_ids: List[int], query_emb: Optional[List[float]]) -> Dict[int, float]:
    """Compute semantic (embedding) similarity between query and memory passages."""
    if not query_emb or not memory_ids:
        return {mid: 0.0 for mid in memory_ids}

    try:
        pass_res = supabase.table("retrieval_passages") \
            .select("id, memory_id, embedding") \
            .in_("memory_id", memory_ids) \
            .execute()

        if not pass_res or not pass_res.data:
            return {mid: 0.0 for mid in memory_ids}

        memory_passages: Dict[int, List[float]] = {}
        for row in pass_res.data:
            mid = row.get("memory_id")
            emb = row.get("embedding")
            if mid and emb and len(emb) == len(query_emb):
                sim = _cosine_similarity(query_emb, emb)
                memory_passages.setdefault(mid, []).append(sim)

        return {mid: max(memory_passages.get(mid, [0.0])) for mid in memory_ids}
    except Exception:
        return {mid: 0.0 for mid in memory_ids}

async def _compute_specificity_boost(phrase_node_ids: List[int], passage_ids: List[int]) -> Dict[int, float]:
    """Map phrase node specificity scores to memories via parallel queries."""
    if not phrase_node_ids:
        return {}

    node_stats_task = asyncio.to_thread(
        lambda: supabase.table("retrieval_passage_phrase_links") \
            .select("node_id, passage_id, retrieval_phrase_nodes!inner(id, retrieval_node_stats!inner(specificity_score))") \
            .in_("node_id", phrase_node_ids) \
            .limit(2000) \
            .execute()
    )

    if not passage_ids:
        async def _empty_bundle():
            class EmptyData:
                data = []
            return EmptyData()
        bundle_task = _empty_bundle()
    else:
        bundle_task = asyncio.to_thread(
            lambda: supabase.table("retrieval_memory_bundle_links") \
                .select("passage_id, memory_id") \
                .in_("passage_id", passage_ids) \
                .limit(2000) \
                .execute()
        )

    node_stats_result, bundle_result = await asyncio.gather(node_stats_task, bundle_task)

    # passage_id -> max specificity score
    passage_spec: Dict[int, float] = {}
    for row in (node_stats_result.data or []):
        pid = row.get("passage_id")
        phrase_obj = row.get("retrieval_phrase_nodes") or {}
        stats = phrase_obj.get("retrieval_node_stats") or {}
        score = stats.get("specificity_score", 0.5)
        if pid:
            if pid not in passage_spec or score > passage_spec[pid]:
                passage_spec[pid] = score

    # memory_id -> max passage score
    boost: Dict[int, float] = {}
    for row in (bundle_result.data or []):
        mid = row.get("memory_id")
        pid = row.get("passage_id")
        if mid and pid:
            s = passage_spec.get(pid, 0.5)
            if mid not in boost or s > boost[mid]:
                boost[mid] = s

    return boost




def _compute_person_boost(memory_ids: List[int], person_id: str) -> Dict[int, float]:
    """Boost memories mentioning a specific person."""
    boost = {mid: 0.0 for mid in memory_ids}
    if not memory_ids or not person_id:
        return boost

    try:
        import uuid
        is_uuid = False
        try:
            uuid.UUID(str(person_id))
            is_uuid = True
        except ValueError:
            pass

        labels = []
        if is_uuid:
            res = supabase.table("graph_nodes").select("label").eq("id", person_id).execute()
            if res and res.data:
                labels.append(res.data[0]["label"])
        else:
            try:
                res = supabase.table("people").select("name").eq("id", int(person_id)).execute()
                if res and res.data:
                    labels.append(res.data[0]["name"])
            except ValueError:
                pass

        if not labels:
            return boost

        label_clean = labels[0].lower().strip()

        # Find matching retrieval_phrase_nodes
        res = supabase.table("retrieval_phrase_nodes") \
            .select("id") \
            .eq("node_type", "person") \
            .ilike("normalized_text", f"%{label_clean}%") \
            .execute()

        if not res or not res.data:
            return boost

        node_ids = [r["id"] for r in res.data]

        # Find passage_ids linking to these node_ids
        res = supabase.table("retrieval_passage_phrase_links") \
            .select("passage_id") \
            .in_("node_id", node_ids) \
            .execute()

        if not res or not res.data:
            return boost

        passage_ids = [r["passage_id"] for r in res.data]

        # Find memory_ids linking to these passage_ids
        res = supabase.table("retrieval_memory_bundle_links") \
            .select("memory_id") \
            .in_("passage_id", passage_ids) \
            .execute()

        if not res or not res.data:
            return boost

        matched_mids = set(r["memory_id"] for r in res.data)

        # Apply boost
        for mid in memory_ids:
            if mid in matched_mids:
                boost[mid] = 1.0

    except Exception as e:
        from core.lib.audit_logger import audit_log_sync
        audit_log_sync("retrieval", "WARNING", f"_compute_person_boost failed: {e}")

    return boost


def _find_neighbor_ids(
    passage_ids: List[int],
    source_map: Dict[tuple, List[tuple]],
    passage_index_map: Dict[int, int],
    source_key_map: Dict[int, tuple],
) -> List[int]:
    """Find adjacent passages (passage_index ± 1) sharing the same source."""
    neighbor_ids = []
    seen = set(passage_ids)
    for pid in passage_ids:
        idx = passage_index_map.get(pid)
        if idx is None:
            continue
        source_key = source_key_map.get(pid)
        if source_key is None:
            continue
        for n_pid, n_idx in source_map.get(source_key, []):
            if n_pid not in seen and abs(n_idx - idx) == 1:
                neighbor_ids.append(n_pid)
                seen.add(n_pid)
    return neighbor_ids


def _assemble_bundles(
    top_memories: List[tuple],
    ppr_scores: Dict[int, float],
    seed_node_ids: List[int],
) -> List[ScoredMemory]:
    """Assemble scored memory bundles with supporting evidence using bulk queries."""
    if not top_memories:
        return []

    items = []
    mids = [int(mid) for mid, _ in top_memories]
    fetch_neighbors = config.context_neighbors

    try:
        # 1. Bulk fetch passage links
        passage_res = supabase.table("retrieval_memory_bundle_links") \
            .select("memory_id, passage_id") \
            .in_("memory_id", mids) \
            .execute()
            
        memory_to_passages = {mid: [] for mid in mids}
        all_passage_ids = set()
        
        if passage_res and passage_res.data:
            for row in passage_res.data:
                mid = row["memory_id"]
                pid = row["passage_id"]
                if len(memory_to_passages[mid]) < 5:
                    memory_to_passages[mid].append(pid)
                    all_passage_ids.add(pid)

        # 2. Bulk fetch passage text (+ metadata for neighbor lookup)
        passage_texts = {}
        passage_index_map = {}
        source_key_map = {}
        source_map: Dict[tuple, List[tuple]] = {}

        if all_passage_ids:
            # Prefer raw_text (user-facing) over text (may be enriched with metadata prefix)
            txt_col = "raw_text, text" if config.chunk_enrichment else "text"
            txt_res = supabase.table("retrieval_passages") \
                .select(f"id, {txt_col}, passage_index, source_type, source_id") \
                .in_("id", list(all_passage_ids)) \
                .execute()
            if txt_res and txt_res.data:
                for r in txt_res.data:
                    display = r.get("raw_text") or r.get("text", "")
                    passage_texts[r["id"]] = display[:200]
                    if fetch_neighbors:
                        idx = r.get("passage_index", 0)
                        st = r.get("source_type", "")
                        sid = r.get("source_id", "")
                        passage_index_map[r["id"]] = idx
                        skey = (st, sid)
                        source_key_map[r["id"]] = skey
                        source_map.setdefault(skey, []).append((r["id"], idx))

        # 2b. Fetch neighbors if enabled
        neighbor_ids = []
        neighbor_texts = {}
        if fetch_neighbors and source_map:
            # Collect initial passages per memory to find their neighbors
            initial_per_memory = {
                mid: [pid for pid in memory_to_passages.get(mid, []) if pid in passage_texts]
                for mid in mids
            }
            all_initial = set()
            for pids in initial_per_memory.values():
                all_initial.update(pids[:3])

            neighbor_ids = _find_neighbor_ids(
                list(all_initial), source_map, passage_index_map, source_key_map,
            )

            if neighbor_ids:
                new_ids = [pid for pid in neighbor_ids if pid not in all_passage_ids]
                if new_ids:
                    ntxt = supabase.table("retrieval_passages") \
                        .select("id, text") \
                        .in_("id", new_ids) \
                        .execute()
                    if ntxt and ntxt.data:
                        for r in ntxt.data:
                            neighbor_texts[r["id"]] = r["text"][:200]
                        passage_texts.update(neighbor_texts)

        # 3. Bulk fetch passage -> phrase links (includes neighbor passages)
        fetch_pids = all_passage_ids | set(neighbor_ids)
        passage_to_nodes = {pid: [] for pid in fetch_pids}
        all_node_ids = set()
        if fetch_pids:
            phrase_res = supabase.table("retrieval_passage_phrase_links") \
                .select("passage_id, node_id") \
                .in_("passage_id", list(fetch_pids)) \
                .execute()
            if phrase_res and phrase_res.data:
                for row in phrase_res.data:
                    pid = row["passage_id"]
                    nid = row["node_id"]
                    passage_to_nodes[pid].append(nid)
                    all_node_ids.add(nid)

        # 4. Bulk fetch phrase node text
        node_texts = {}
        if all_node_ids:
            name_res = supabase.table("retrieval_phrase_nodes") \
                .select("id, display_text") \
                .in_("id", list(all_node_ids)) \
                .execute()
            if name_res and name_res.data:
                node_texts = {r["id"]: r["display_text"] for r in name_res.data}

        # Assemble items
        neighbor_set = set(neighbor_ids)
        for mid, score in top_memories:
            mid = int(mid)
            passage_ids = memory_to_passages.get(mid, [])

            # Primary supporting passages: first 3 from this memory
            primary = [pid for pid in passage_ids[:3] if pid in passage_texts]
            # Neighbor passages: adjacent to primary, not already primary
            neighbors = [pid for pid in neighbor_set if pid not in primary]
            # Merge, cap at _MAX_SUPPORTING_PASSAGES
            all_supporting_ids = (primary + neighbors)[:_MAX_SUPPORTING_PASSAGES]
            supporting = [passage_texts[pid] for pid in all_supporting_ids]

            nids = []
            for pid in all_supporting_ids:
                nids.extend(passage_to_nodes.get(pid, []))
            
            nids = list(dict.fromkeys(nids))[:5] # dedup
            connected = [node_texts[nid] for nid in nids if nid in node_texts]
            
            explanation_parts = []
            if connected:
                explanation_parts.append(f"Connected to: {', '.join(connected[:3])}")
            if neighbors:
                explanation_parts.append(f"{len(neighbors)} neighbor passage(s) included")
            explanation = "; ".join(explanation_parts) if explanation_parts else "Relevant context"

            items.append(ScoredMemory(
                memory_id=mid,
                score=round(score, 4),
                passage_ids=passage_ids,
                supporting_passages=supporting,
                connected_phrases=connected,
                explanation=explanation,
            ))

    except Exception as e:
        from core.lib.audit_logger import audit_log_sync
        audit_log_sync("retrieval", "WARNING", f"_assemble_bundles failed: {e}")
        # Fallback if bulk fails
        for mid, score in top_memories:
            items.append(ScoredMemory(
                memory_id=int(mid),
                score=round(score, 4),
                explanation="Memory bundle",
            ))

    return items


async def search_memories_compat(
    query_text: str,
    top_k: int = 5,
    threshold: float = 0.6,
    recency_weight: float = 0.3,
    importance_weight: float = 0.2,
    use_associative: bool | None = None,
) -> list:
    """Unified memory retrieval — returns list of dicts compatible with match_memories_hybrid output.
    
    When use_associative is True, uses associative_retrieve.
    When use_associative is None, falls back to config.associative_enabled.
    Otherwise falls back to the legacy RPC via get_embedding.
    """
    enabled = use_associative if use_associative is not None else config.associative_enabled
    if enabled:
        bundle = await associative_retrieve(query=query_text, top_k=top_k)
        if not bundle.items:
            return []
        results = []
        now_iso = datetime.now(timezone.utc).isoformat()
        
        # Batch-fetch all memory contents in a single query instead of N sequential queries
        memory_ids = [item.memory_id for item in bundle.items]
        mem_map = {}
        try:
            mem_res = supabase.table("memories") \
                .select("id, content, memory_type, created_at, expires_at") \
                .in_("id", memory_ids) \
                .execute()
            if mem_res and mem_res.data:
                for row in mem_res.data:
                    mid = row["id"]
                    expires = row.get("expires_at")
                    if expires and expires < now_iso:
                        continue
                    mem_map[mid] = row
        except Exception:
            pass
        
        for item in bundle.items:
            row = mem_map.get(item.memory_id)
            if row:
                row["similarity"] = item.score
                results.append(row)
        return results

    from core.llm import get_embedding as _get_embedding
    embedding = (await _get_embedding(query_text)).vector
    if not embedding:
        return []
    res = supabase.rpc('match_memories_hybrid', {
        'query_embedding': embedding,
        'match_count': top_k,
        'match_threshold': threshold,
        'recency_weight': recency_weight,
        'importance_weight': importance_weight,
    }).execute()
    return res.data or []
