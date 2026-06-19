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

    # 1. Parse query — merge LLM entities (precision) with lexical words (recall)
    llm_phrases = await _extract_query_entities(query) or []
    lex_phrases = _parse_query(query) or []
    query_phrases = list(set(llm_phrases + lex_phrases))
    debug["query_phrases"] = query_phrases
    debug["llm_phrases"] = llm_phrases
    debug["lex_phrases"] = lex_phrases

    if not query_phrases:
        return ExplainableBundle(query=query, items=[], latency_ms=int((time.time() - start) * 1000))

    # 2. Candidate retrieval
    phrase_nodes = _retrieve_phrase_candidates(query_phrases, top_k=DEFAULT_TOP_K_PHRASES)
    debug["phrase_candidates"] = len(phrase_nodes)

    if not phrase_nodes:
        return ExplainableBundle(query=query, items=[], latency_ms=int((time.time() - start) * 1000))

    # 3. Recognition filter
    filtered_nodes = _recognition_filter(phrase_nodes, query_phrases)
    debug["filtered_nodes"] = len(filtered_nodes)

    if not filtered_nodes:
        return ExplainableBundle(query=query, items=[], latency_ms=int((time.time() - start) * 1000))

    # 4. Build adjacency and seed PPR
    seed_nodes = {n["id"]: n.get("similarity", 0.5) for n in filtered_nodes}
    edges = _fetch_subgraph_edges(list(seed_nodes.keys()))
    debug["subgraph_edges"] = len(edges)

    if not edges:
        return ExplainableBundle(query=query, items=[], latency_ms=int((time.time() - start) * 1000))

    # 5. Run PPR
    adjacency = build_adjacency_from_edges(edges)
    ppr_raw = personalized_pagerank(adjacency, seed_nodes)
    ppr_norm = normalize_scores(ppr_raw)
    debug["ppr_nodes"] = len(ppr_norm)

    # 6. Aggregate PPR → passages → memories
    memory_scores = _aggregate_to_memories(ppr_norm, list(seed_nodes.keys()))
    debug["memory_candidates"] = len(memory_scores)

    if not memory_scores:
        return ExplainableBundle(query=query, items=[], latency_ms=int((time.time() - start) * 1000))

    # 7. Blended ranking
    memory_ids = list(memory_scores.keys())
    semantic_scores = await _compute_semantic_scores(memory_ids, query)
    specificity_boost = _compute_specificity_boost(memory_ids)
    recency_boost = _compute_recency_boost(memory_ids)
    importance_boost = _compute_importance_boost(memory_ids)

    project_boost = {}
    if active_project_id:
        project_boost = _compute_project_boost(memory_ids, active_project_id)

    person_boost = {}
    if active_person_id:
        person_boost = _compute_person_boost(memory_ids, active_person_id)

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
    items = _assemble_bundles(top_memories, ppr_norm, list(seed_nodes.keys()))

    latency = int((time.time() - start) * 1000)

    return ExplainableBundle(
        query=query,
        items=items,
        total_candidates=len(ranked),
        latency_ms=latency,
        debug_trace=debug if config.debug_explanations else None,
        blended=(retrieval_mode == "blended"),
    )


QUERY_ENTITY_PROMPT = """Extract the most important entities from this query.

An entity is a specific person, project, organization, topic, or concept.

Return a JSON array of strings. Each string is a single entity using wording close to the query.
Do NOT include generic words like "me", "my", "this", "that", "things".
If there are no specific entities, return an empty array [].

Examples:
Query: "meetings with Ashraya this week"
Response: ["Ashraya"]

Query: "what did we decide about QHORD pricing"
Response: ["QHORD", "pricing"]

Query: "remind me about church admin work"
Response: ["church admin"]

Query: "anything related to SOLVSTRAT"
Response: ["SOLVSTRAT"]

Query: "{query}"
"""


async def _extract_query_entities(query: str) -> List[str]:
    """Extract entities from query using an LLM call.

    Uses CLASSIFICATION_MODEL for fast, lightweight extraction.
    Returns empty list on any failure (caller falls back to lexical).
    """
    from core.llm.fallback import generate_content_with_fallback
    from core.llm.config import WorkloadProfile
    from core.llm.constants import CLASSIFICATION_MODEL
    import json
    try:
        prompt = QUERY_ENTITY_PROMPT.format(query=query)
        response = await generate_content_with_fallback(
            prompt=prompt,
            workload=WorkloadProfile.INTERACTIVE,
            primary_model=CLASSIFICATION_MODEL,
            config={'response_mime_type': 'application/json'},
        )
        if not response or not response.text:
            return []
        raw = response.text.strip()
        if raw.startswith("```"):
            raw = raw.strip("`")
            if raw.startswith("json"):
                raw = raw[4:]
        data = json.loads(raw)
        if not isinstance(data, list):
            return []
        return [e.strip() for e in data if isinstance(e, str) and e.strip()]
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


def _retrieve_phrase_candidates(phrases: List[str], top_k: int = 30) -> List[dict]:
    """Retrieve candidate phrase nodes matching query phrases (exact ILIKE)."""
    candidates = []
    seen = set()

    for phrase in phrases:
        try:
            res = supabase.table("retrieval_phrase_nodes") \
                .select("id, normalized_text, display_text, node_type") \
                .ilike("normalized_text", f"%{phrase}%") \
                .limit(5) \
                .execute()

            if res and res.data:
                for row in res.data:
                    nid = row["id"]
                    if nid not in seen:
                        seen.add(nid)
                        row["similarity"] = 0.5
                        candidates.append(row)
        except Exception:
            continue

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


def _fetch_subgraph_edges(node_ids: List[int]) -> List[tuple]:
    """Fetch edges connecting the seeded nodes (both directions)."""
    if not node_ids:
        return []

    edges = []
    try:
        res = supabase.table("retrieval_edges") \
            .select("from_node_id, to_node_id, weight") \
            .in_("from_node_id", node_ids) \
            .limit(200) \
            .execute()

        if res and res.data:
            for row in res.data:
                edges.append((row["from_node_id"], row["to_node_id"], row.get("weight", 1.0)))

        res2 = supabase.table("retrieval_edges") \
            .select("from_node_id, to_node_id, weight") \
            .in_("to_node_id", node_ids) \
            .limit(200) \
            .execute()

        if res2 and res2.data:
            for row in res2.data:
                edges.append((row["from_node_id"], row["to_node_id"], row.get("weight", 1.0)))

        alias_res = supabase.table("retrieval_alias_edges") \
            .select("from_node_id, to_node_id, weight") \
            .in_("from_node_id", node_ids) \
            .limit(100) \
            .execute()

        if alias_res and alias_res.data:
            for row in alias_res.data:
                edges.append((row["from_node_id"], row["to_node_id"], row.get("weight", 0.8)))

    except Exception:
        pass

    return edges


def _aggregate_to_memories(ppr_scores: Dict[int, float],
                            seed_node_ids: List[int]) -> Dict[int, float]:
    """Aggregate PPR scores from phrase nodes back to memory IDs via passage links."""
    if not ppr_scores:
        return {}

    node_ids = list(ppr_scores.keys())
    memory_scores: Dict[int, float] = {}

    try:
        res = supabase.table("retrieval_passage_phrase_links") \
            .select("passage_id, node_id") \
            .in_("node_id", node_ids) \
            .limit(500) \
            .execute()

        if not res or not res.data:
            return {}

        passage_scores: Dict[int, float] = {}
        for row in res.data:
            pid = row["passage_id"]
            nid = row["node_id"]
            score = ppr_scores.get(nid, 0.0)
            passage_scores[pid] = max(passage_scores.get(pid, 0.0), score)

        if not passage_scores:
            return {}

        mem_res = supabase.table("retrieval_memory_bundle_links") \
            .select("memory_id, passage_id") \
            .in_("passage_id", list(passage_scores.keys())) \
            .limit(500) \
            .execute()

        if mem_res and mem_res.data:
            for row in mem_res.data:
                mid = row["memory_id"]
                pid = row["passage_id"]
                score = passage_scores.get(pid, 0.0)
                memory_scores[mid] = max(memory_scores.get(mid, 0.0), score)

    except Exception:
        pass

    return memory_scores


def _cosine_similarity(a: list, b: list) -> float:
    """Compute cosine similarity between two vectors."""
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    if na < 1e-10 or nb < 1e-10:
        return 0.0
    return dot / (na * nb)


async def _compute_semantic_scores(memory_ids: List[int], query: str) -> Dict[int, float]:
    """Compute semantic (embedding) similarity between query and memory passages.

    Gets query embedding, finds passages for each memory, computes cosine
    similarity, and aggregates to memory level via max.
    """
    from core.llm import get_embedding as _get_embedding
    query_emb = (await _get_embedding(query)).vector
    if not query_emb:
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


def _compute_specificity_boost(memory_ids: List[int]) -> Dict[int, float]:
    """Map phrase node IDF-based specificity scores to memories.

    Specificity captures how rare/unique the terms in a memory are.
    Higher specificity → more focused, informational content.
    """
    if not memory_ids:
        return {}
    try:
        mem_res = supabase.table("retrieval_memory_bundle_links") \
            .select("memory_id, passage_id") \
            .in_("memory_id", memory_ids) \
            .limit(500) \
            .execute()
        if not mem_res or not mem_res.data:
            return {}

        pids = list(set(r["passage_id"] for r in mem_res.data))
        if not pids:
            return {}

        link_res = supabase.table("retrieval_passage_phrase_links") \
            .select("passage_id, node_id") \
            .in_("passage_id", pids) \
            .limit(1000) \
            .execute()
        if not link_res or not link_res.data:
            return {}

        nids = list(set(r["node_id"] for r in link_res.data))
        if not nids:
            return {}

        spec_res = supabase.table("retrieval_node_stats") \
            .select("node_id, specificity_score") \
            .in_("node_id", nids) \
            .execute()

        node_spec = {}
        if spec_res and spec_res.data:
            for r in spec_res.data:
                node_spec[r["node_id"]] = r.get("specificity_score", 0.5)

        passage_spec: Dict[int, float] = {}
        for r in link_res.data:
            pid = r["passage_id"]
            nid = r["node_id"]
            s = node_spec.get(nid, 0.5)
            if pid not in passage_spec or s > passage_spec[pid]:
                passage_spec[pid] = s

        boost: Dict[int, float] = {}
        for r in mem_res.data:
            mid = r["memory_id"]
            pid = r["passage_id"]
            s = passage_spec.get(pid, 0.5)
            if mid not in boost or s > boost[mid]:
                boost[mid] = s

        return boost
    except Exception:
        return {}


def _compute_recency_boost(memory_ids: List[int]) -> Dict[int, float]:
    """Compute recency boost for memories (newer = higher)."""
    if not memory_ids:
        return {}
    try:
        res = supabase.table("memories") \
            .select("id, created_at") \
            .in_("id", memory_ids) \
            .execute()

        if not res or not res.data:
            return {}

        now = datetime.now(timezone.utc)
        boosts = {}
        for row in res.data:
            created = row.get("created_at")
            if created:
                if isinstance(created, str):
                    created = datetime.fromisoformat(created.replace("Z", "+00:00"))
                days_old = max(0, (now - created).total_seconds() / 86400.0)
                boosts[row["id"]] = max(0.0, 1.0 - days_old / 90.0)
            else:
                boosts[row["id"]] = 0.0
        return boosts
    except Exception:
        return {}


def _compute_importance_boost(memory_ids: List[int]) -> Dict[int, float]:
    """Importance score normalized to 0-1."""
    if not memory_ids:
        return {}
    try:
        res = supabase.table("memories") \
            .select("id, importance_score") \
            .in_("id", memory_ids) \
            .execute()

        if not res or not res.data:
            return {}
        return {row["id"]: (row.get("importance_score", 5) or 5) / 10.0 for row in res.data}
    except Exception:
        return {}


def _compute_project_boost(memory_ids: List[int], project_id: int) -> Dict[int, float]:
    """Boost memories matching a specific project (via graph_nodes link)."""
    if not memory_ids:
        return {}
    boost = {}
    try:
        res = supabase.table("memories") \
            .select("id, project_id") \
            .in_("id", memory_ids) \
            .execute()
        if res and res.data:
            for row in res.data:
                boost[row["id"]] = 1.0 if row.get("project_id") == project_id else 0.0
    except Exception:
        pass
    return boost


def _compute_person_boost(memory_ids: List[int], person_id: str) -> Dict[int, float]:
    """Boost memories mentioning a specific person.
    # TODO: person_boost — returns 0.0, 5% weight unpopulated
    Needs integration with main KG person nodes to resolve person_id
    against graph_nodes and cross-reference with retrieval phrase nodes.
    """
    boost = {}
    for mid in memory_ids:
        boost[mid] = 0.0
    return boost


def _assemble_bundles(
    top_memories: List[tuple],
    ppr_scores: Dict[int, float],
    seed_node_ids: List[int],
) -> List[ScoredMemory]:
    """Assemble scored memory bundles with supporting evidence."""
    items = []

    for mid, score in top_memories:
        mid = int(mid)

        try:
            passage_res = supabase.table("retrieval_memory_bundle_links") \
                .select("passage_id") \
                .eq("memory_id", mid) \
                .limit(5) \
                .execute()

            passage_ids = []
            supporting = []
            if passage_res and passage_res.data:
                passage_ids = [r["passage_id"] for r in passage_res.data]

                if passage_ids:
                    txt_res = supabase.table("retrieval_passages") \
                        .select("text") \
                        .in_("id", passage_ids[:3]) \
                        .execute()
                    if txt_res and txt_res.data:
                        supporting = [r["text"][:200] for r in txt_res.data]

            phrase_res = supabase.table("retrieval_passage_phrase_links") \
                .select("node_id") \
                .in_("passage_id", passage_ids[:3]) \
                .limit(10) \
                .execute()

            connected = []
            if phrase_res and phrase_res.data:
                nids = list(set(r["node_id"] for r in phrase_res.data))
                if nids:
                    name_res = supabase.table("retrieval_phrase_nodes") \
                        .select("display_text") \
                        .in_("id", nids[:5]) \
                        .execute()
                    if name_res and name_res.data:
                        connected = [r["display_text"] for r in name_res.data]

            explanation_parts = []
            if connected:
                explanation_parts.append(f"Connected to: {', '.join(connected[:3])}")
            explanation = "; ".join(explanation_parts) if explanation_parts else "Relevant context"

            items.append(ScoredMemory(
                memory_id=mid,
                score=round(score, 4),
                passage_ids=passage_ids,
                supporting_passages=supporting,
                connected_phrases=connected,
                explanation=explanation,
            ))

        except Exception:
            items.append(ScoredMemory(
                memory_id=mid,
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
        results = []
        for item in bundle.items:
            mem = supabase.table("memories") \
                .select("id, content, memory_type, created_at") \
                .eq("id", item.memory_id) \
                .maybe_single() \
                .execute()
            if mem and mem.data:
                mem.data["similarity"] = item.score
                results.append(mem.data)
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
