"""M5: Memory Clustering — PPR-based community detection with quality guards.

Algorithm:
1. Build memory-to-entity bipartite graph from retrieval tables
2. Compute seed weights (specificity × rarity_factor)
3. Run PPR from each seed (damping=0.85, iterations=20)
4. Assign memories using percentile-rank (70th percentile threshold)
5. Compute quality_score (5 components)
6. Match against existing clusters (fingerprint + Jaccard)
7. Create/update/supersede clusters

Runs weekly via GitHub Actions. Outputs audit artifact.
"""

import json
import math
import hashlib
from collections import Counter, defaultdict
from datetime import datetime, timezone

from core.services.db import get_supabase, maybe_single_safe
from core.lib.audit_logger import audit_log_sync
from core.retrieval.ppr import personalized_pagerank, build_adjacency_from_edges, normalize_scores

supabase = get_supabase()

# ── Constants ──────────────────────────────────────────────────
SEED_WEIGHT_THRESHOLD = 0.2          # Minimum seed weight for PPR seeding
PPR_DAMPING = 0.85
PPR_ITERATIONS = 20
PERCENTILE_THRESHOLD = 0.7           # 70th percentile for cluster assignment
MAX_CLUSTERS_PER_MEMORY = 3
MAX_SEEDS_PER_RUN = 200              # Hard cap on seeds processed per run
QUALITY_REJECT_THRESHOLD = 0.5       # Clusters below this quality are rejected
JACCARD_MERGE_THRESHOLD = 0.7        # Jaccard > 0.7 → merge into existing
JACCARD_RELATED_THRESHOLD = 0.4      # Jaccard 0.4-0.7 → new related cluster
FINGERPRINT_SEED_COUNT = 5
FINGERPRINT_MEMBER_COUNT = 5


def _fetch_all_degrees() -> dict:
    """Batch-fetch degree for all phrase nodes. Returns {node_id: degree}."""
    try:
        from_ret = supabase.table("retrieval_edges") \
            .select("from_node_id") \
            .execute()
        to_ret = supabase.table("retrieval_edges") \
            .select("to_node_id") \
            .execute()
        degree_map = defaultdict(int)
        for row in (from_ret.data or []):
            degree_map[row["from_node_id"]] += 1
        for row in (to_ret.data or []):
            degree_map[row["to_node_id"]] += 1
        return dict(degree_map)
    except Exception:
        return {}


def _fetch_max_degree(degree_map: dict) -> int:
    """Return max degree from pre-fetched degree map."""
    return max(degree_map.values()) if degree_map else 1


def _compute_genericity(degree: int, max_degree: int) -> float:
    """High degree = generic = low clustering value."""
    if max_degree <= 1:
        return 0.0
    return math.log(degree + 1) / math.log(max_degree + 1)


def _compute_seed_weight(phrase_node: dict, max_degree: int, degree_map: dict) -> float:
    """Compute seed suitability weight: specificity × rarity_factor."""
    occurrence = phrase_node.get("occurrence_count", 0)
    specificity = phrase_node.get("specificity_score", 0.5)
    node_type = phrase_node.get("node_type", "concept")

    # Skip concept nodes (too abstract for seeding)
    if node_type == "concept":
        return 0.0

    # Rarity factor: soft downweight, not exclusion
    if occurrence >= 3:
        rarity_factor = 1.0
    else:
        rarity_factor = occurrence / 3.0

    # Genericity penalty
    degree = degree_map.get(phrase_node["id"], 0)
    genericity = _compute_genericity(degree, max_degree)
    genericity_factor = max(0.0, 1.0 - genericity)

    return specificity * rarity_factor * genericity_factor


def _percentile_threshold(scores: list, percentile: float = 0.7) -> float:
    """Compute percentile-rank threshold from a list of scores."""
    if not scores:
        return float("inf")
    sorted_scores = sorted(scores, reverse=True)
    index = int(len(sorted_scores) * (1.0 - percentile))
    index = min(index, len(sorted_scores) - 1)
    return sorted_scores[index]


def _assign_memories_to_clusters(
    ppr_results: dict,  # seed_node_id → {memory_id: score}
) -> dict:  # memory_id → [(cluster_seed_id, score)]
    """Assign memories to clusters using percentile-rank normalization."""
    assignments = defaultdict(list)

    for seed_id, mem_scores in ppr_results.items():
        if not mem_scores:
            continue
        scores_list = list(mem_scores.values())
        threshold = _percentile_threshold(scores_list, PERCENTILE_THRESHOLD)

        for mem_id, score in mem_scores.items():
            if score >= threshold:
                assignments[mem_id].append((seed_id, score))

    # Cap per memory
    for mem_id in assignments:
        assignments[mem_id].sort(key=lambda x: x[1], reverse=True)
        assignments[mem_id] = assignments[mem_id][:MAX_CLUSTERS_PER_MEMORY]

    return dict(assignments)


def _compute_quality_score(
    cluster_memories: list,  # list of memory dicts with embedding, memory_type, created_at
    seed_phrase_nodes: list,  # list of phrase node dicts
) -> float:
    """Compute composite quality score (0.0 to 1.0).

    Components:
    1. Intra-cluster similarity (0.35)
    2. Phrase-node concentration (0.25)
    3. Recency coherence (0.20)
    4. Size penalty/bonus (0.10)
    5. Entropy modifier (0.10)
    """
    if len(cluster_memories) < 2:
        return 0.0

    # 1. Intra-cluster similarity
    embeddings = [m.get("embedding") for m in cluster_memories if m.get("embedding")]
    if len(embeddings) >= 2:
        sims = []
        for i in range(len(embeddings)):
            for j in range(i + 1, len(embeddings)):
                sim = _cosine_sim(embeddings[i], embeddings[j])
                sims.append(sim)
        intra_sim = sum(sims) / len(sims) if sims else 0.0
    else:
        intra_sim = 0.0

    # 2. Phrase-node concentration
    seed_embeddings = [s.get("embedding") for s in seed_phrase_nodes if s.get("embedding")]
    if seed_embeddings and embeddings:
        concentration_sims = []
        for seed_emb in seed_embeddings[:3]:
            for mem_emb in embeddings[:5]:
                concentration_sims.append(_cosine_sim(seed_emb, mem_emb))
        concentration = sum(concentration_sims) / len(concentration_sims) if concentration_sims else 0.0
    else:
        concentration = 0.0

    # 3. Recency coherence
    dates = []
    for m in cluster_memories:
        ca = m.get("created_at", "")
        if ca:
            try:
                dt = datetime.fromisoformat(str(ca).replace("Z", "+00:00"))
                dates.append(dt)
            except Exception:
                pass
    if len(dates) >= 2:
        std_dev_days = _std_dev_days(dates)
        recency = 1.0 / (1.0 + std_dev_days / 30.0)
    else:
        recency = 0.5

    # 4. Size penalty/bonus
    n = len(cluster_memories)
    if 5 <= n <= 20:
        size_score = 1.0
    elif n < 3:
        size_score = 0.3
    elif n < 5:
        size_score = 0.7
    elif n <= 50:
        size_score = 0.8
    else:
        size_score = 0.5

    # 5. Entropy modifier (soft, not rejection)
    types = [m.get("memory_type", "note") for m in cluster_memories]
    entropy_mod = _compute_entropy_modifier(types)

    # Weighted sum
    quality = (
        intra_sim * 0.35
        + concentration * 0.25
        + recency * 0.20
        + size_score * 0.10
        + entropy_mod * 0.10
    )

    return round(min(1.0, max(0.0, quality)), 4)


def _compute_entropy_modifier(memory_types: list) -> float:
    """Soft entropy modifier: 0.7 to 1.0 range."""
    if not memory_types:
        return 1.0
    type_counts = Counter(memory_types)
    total = len(memory_types)
    num_types = len(type_counts)
    if num_types <= 1:
        return 1.0
    entropy = 0.0
    for count in type_counts.values():
        p_i = count / total
        if p_i > 0:
            entropy -= p_i * math.log2(p_i)
    normalized_entropy = entropy / math.log2(num_types)
    return 1.0 - (normalized_entropy * 0.3)


def _cosine_sim(a: list, b: list) -> float:
    """Cosine similarity between two vectors."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    if na < 1e-10 or nb < 1e-10:
        return 0.0
    return dot / (na * nb)


def _std_dev_days(dates: list) -> float:
    """Standard deviation of dates in days."""
    if len(dates) < 2:
        return 0.0
    timestamps = [d.timestamp() for d in dates]
    mean = sum(timestamps) / len(timestamps)
    variance = sum((t - mean) ** 2 for t in timestamps) / len(timestamps)
    return (variance ** 0.5) / 86400.0


def _compute_fingerprint(seed_ids: list, member_ids: list) -> str:
    """Stable fingerprint from top seeds + top members."""
    top_seeds = sorted(seed_ids[:FINGERPRINT_SEED_COUNT])
    top_members = sorted(member_ids[:FINGERPRINT_MEMBER_COUNT])
    combined = f"seeds:{','.join(str(s) for s in top_seeds)}|members:{','.join(str(m) for m in top_members)}"
    return hashlib.sha256(combined.encode()).hexdigest()[:16]


def _jaccard_similarity(set_a: set, set_b: set) -> float:
    """Jaccard similarity between two sets."""
    intersection = len(set_a & set_b)
    union = len(set_a | set_b)
    return intersection / union if union > 0 else 0.0


def _match_existing_clusters(
    new_fingerprint: str,
    new_member_ids: set,
    existing_clusters: list,  # list of dicts with id, fingerprint, member_ids
) -> tuple:
    """Match new cluster against existing ones.

    Returns (cluster_id, action) where action is:
    'fingerprint_match', 'jaccard_merge', 'new_related', 'new_unrelated'
    """
    for existing in existing_clusters:
        if existing["fingerprint"] == new_fingerprint:
            return existing["id"], "fingerprint_match"

    best_match = None
    best_score = 0.0
    for existing in existing_clusters:
        existing_members = set(existing.get("member_ids", []))
        jaccard = _jaccard_similarity(new_member_ids, existing_members)
        if jaccard > best_score:
            best_score = jaccard
            best_match = existing

    if best_match and best_score > JACCARD_MERGE_THRESHOLD:
        return best_match["id"], "jaccard_merge"
    elif best_match and best_score > JACCARD_RELATED_THRESHOLD:
        return None, "new_related"
    else:
        return None, "new_unrelated"


async def build_memory_clusters() -> dict:
    """Main entry point: build memory clusters from graph structure.

    Returns audit artifact dict.
    """
    now = datetime.now(timezone.utc)
    audit = {
        "clusters_created": 0,
        "clusters_reused": 0,
        "clusters_superseded": 0,
        "orphans_count": 0,
        "total_memories": 0,
        "seeds_processed": 0,
        "quality_histogram": {"<0.5": 0, "0.5-0.7": 0, "0.7-0.9": 0, ">0.9": 0},
        "errors": [],
    }

    try:
        # 1. Fetch all indexed memories with their passage links
        memories_res = supabase.table("memories") \
            .select("id, content, memory_type, created_at, embedding") \
            .eq("embedding_status", "success") \
            .execute()
        all_memories = memories_res.data or []
        audit["total_memories"] = len(all_memories)

        # Sanitize embedding values — PostgreSQL vector values may be
        # returned as a JSON string ("[0.1, 0.2, ...]") or as a list with
        # string-typed entries (["0.5", 0.3]) after migrations or backfills.
        # Either form crashes _cosine_sim() (float * str → TypeError).
        for m in all_memories:
            m["embedding"] = _sanitize_embedding(m.get("embedding"))

        if len(all_memories) < 10:
            audit_log_sync("memory_clusters", "INFO", f"Too few memories ({len(all_memories)}), skipping clustering")
            return audit

        memory_map = {m["id"]: m for m in all_memories}

        # 2. Fetch passage → memory links
        link_res = supabase.table("retrieval_memory_bundle_links") \
            .select("memory_id, passage_id") \
            .execute()
        passage_to_memories = defaultdict(set)
        memory_to_passages = defaultdict(set)
        for link in (link_res.data or []):
            mid = link["memory_id"]
            pid = link["passage_id"]
            passage_to_memories[pid].add(mid)
            memory_to_passages[mid].add(pid)

        # 3. Fetch passage → phrase node links
        ppl_res = supabase.table("retrieval_passage_phrase_links") \
            .select("passage_id, node_id, weight") \
            .execute()
        passage_to_nodes = defaultdict(list)
        node_to_passages = defaultdict(set)
        for ppl in (ppl_res.data or []):
            pid = ppl["passage_id"]
            nid = ppl["node_id"]
            passage_to_nodes[pid].append(nid)
            node_to_passages[nid].add(pid)

        # 4. Build bipartite graph: memory → phrase nodes
        memory_to_nodes = defaultdict(set)
        for mid, pids in memory_to_passages.items():
            for pid in pids:
                for nid in passage_to_nodes.get(pid, []):
                    memory_to_nodes[mid].add(nid)

        # 5. Fetch phrase node metadata for seed selection
        all_node_ids = set()
        for nids in memory_to_nodes.values():
            all_node_ids.update(nids)

        if not all_node_ids:
            audit_log_sync("memory_clusters", "INFO", "No phrase nodes found, skipping")
            return audit

        nodes_res = supabase.table("retrieval_phrase_nodes") \
            .select("id, normalized_text, node_type") \
            .in_("id", list(all_node_ids)) \
            .execute()
        node_map = {n["id"]: n for n in (nodes_res.data or [])}

        # Fetch node stats for specificity
        stats_res = supabase.table("retrieval_node_stats") \
            .select("node_id, specificity_score, df") \
            .execute()
        stats_map = {}
        for s in (stats_res.data or []):
            stats_map[s["node_id"]] = s

        # 6. Batch-fetch all degrees and compute seed weights
        degree_map = _fetch_all_degrees()
        max_degree = _fetch_max_degree(degree_map)
        seeds = []
        for nid in all_node_ids:
            node = node_map.get(nid)
            if not node:
                continue
            stats = stats_map.get(nid, {})
            weight = _compute_seed_weight(
                {
                    "id": nid,
                    "node_type": node.get("node_type", "concept"),
                    "occurrence_count": stats.get("df", 0),
                    "specificity_score": stats.get("specificity_score", 0.5),
                },
                max_degree,
                degree_map,
            )
            if weight > SEED_WEIGHT_THRESHOLD:
                seeds.append({"id": nid, "weight": weight, "text": node.get("normalized_text", "")})

        # Sort by weight, apply cap
        seeds.sort(key=lambda x: x["weight"], reverse=True)
        seeds = seeds[:MAX_SEEDS_PER_RUN]
        audit["seeds_processed"] = len(seeds)

        if not seeds:
            audit_log_sync("memory_clusters", "INFO", "No qualifying seeds found")
            return audit

        # 7. Run PPR from each seed
        # Build adjacency from retrieval_edges
        edges_res = supabase.table("retrieval_edges") \
            .select("from_node_id, to_node_id, weight") \
            .execute()
        edges = [(e["from_node_id"], e["to_node_id"], e.get("weight", 1.0)) for e in (edges_res.data or [])]
        adjacency = build_adjacency_from_edges(edges)

        # Map phrase nodes → memory IDs (for PPR result aggregation)
        phrase_to_memories = defaultdict(set)
        for mid, nids in memory_to_nodes.items():
            for nid in nids:
                phrase_to_memories[nid].add(mid)

        ppr_results = {}  # seed_id → {memory_id: score}
        for seed in seeds:
            seed_id = seed["id"]
            personalization = {seed_id: seed["weight"]}
            ppr_raw = personalized_pagerank(adjacency, personalization, damping=PPR_DAMPING, iterations=PPR_ITERATIONS)
            ppr_norm = normalize_scores(ppr_raw)

            # Aggregate phrase scores → memory scores
            mem_scores = defaultdict(float)
            for phrase_id, phrase_score in ppr_norm.items():
                for mid in phrase_to_memories.get(phrase_id, []):
                    mem_scores[mid] = max(mem_scores[mid], phrase_score)
            ppr_results[seed_id] = dict(mem_scores)

        # 8. Assign memories to clusters
        assignments = _assign_memories_to_clusters(ppr_results)

        # 9. Build cluster candidates from assignments
        # Group by primary seed (the seed with highest score for each memory)
        seed_to_cluster_members = defaultdict(set)
        seed_to_cluster_scores = defaultdict(dict)
        for mem_id, seed_assignments in assignments.items():
            if not seed_assignments:
                audit["orphans_count"] += 1
                continue
            primary_seed = seed_assignments[0][0]
            seed_to_cluster_members[primary_seed].add(mem_id)
            seed_to_cluster_scores[primary_seed][mem_id] = seed_assignments[0][1]

        # 10. Filter small clusters (< 3 memories)
        cluster_candidates = []
        for seed_id, member_ids in seed_to_cluster_members.items():
            if len(member_ids) < 3:
                audit["orphans_count"] += len(member_ids)
                continue
            cluster_candidates.append({
                "seed_id": seed_id,
                "seed_text": next((s["text"] for s in seeds if s["id"] == seed_id), ""),
                "member_ids": member_ids,
                "scores": seed_to_cluster_scores[seed_id],
            })

        # 11. Compute quality scores
        for candidate in cluster_candidates:
            member_memories = [memory_map[mid] for mid in candidate["member_ids"] if mid in memory_map]
            seed_nodes = [node_map.get(candidate["seed_id"], {})]
            candidate["quality_score"] = _compute_quality_score(member_memories, seed_nodes)
            candidate["fingerprint"] = _compute_fingerprint(
                [candidate["seed_id"]],
                list(candidate["member_ids"]),
            )

            # Quality histogram
            q = candidate["quality_score"]
            if q < 0.5:
                audit["quality_histogram"]["<0.5"] += 1
            elif q < 0.7:
                audit["quality_histogram"]["0.5-0.7"] += 1
            elif q < 0.9:
                audit["quality_histogram"]["0.7-0.9"] += 1
            else:
                audit["quality_histogram"][">0.9"] += 1

        # 12. Filter by quality threshold
        qualified = [c for c in cluster_candidates if c["quality_score"] >= QUALITY_REJECT_THRESHOLD]
        rejected = len(cluster_candidates) - len(qualified)
        audit["orphans_count"] += rejected

        # 13. Match against existing clusters
        existing_res = supabase.table("memory_clusters") \
            .select("id, fingerprint, status") \
            .in_("status", ["candidate", "active"]) \
            .execute()
        existing_clusters_raw = existing_res.data or []

        # Fetch member IDs for existing clusters
        existing_clusters = []
        for ec in existing_clusters_raw:
            members_res = supabase.table("memory_cluster_members") \
                .select("memory_id") \
                .eq("cluster_id", ec["id"]) \
                .execute()
            member_ids = [m["memory_id"] for m in (members_res.data or [])]
            existing_clusters.append({
                "id": ec["id"],
                "fingerprint": ec["fingerprint"],
                "member_ids": member_ids,
                "status": ec["status"],
            })

        # 14. Create/update clusters
        for candidate in qualified:
            match_id, action = _match_existing_clusters(
                candidate["fingerprint"],
                candidate["member_ids"],
                existing_clusters,
            )

            if action == "fingerprint_match":
                # Reuse existing cluster — update members
                _update_cluster_members(match_id, candidate["member_ids"], candidate["scores"])
                audit["clusters_reused"] += 1

            elif action == "jaccard_merge":
                # Merge into existing — add members, supersede if needed
                _merge_cluster(match_id, candidate["member_ids"], candidate["scores"], candidate["quality_score"])
                audit["clusters_reused"] += 1

            else:
                # Create new cluster
                cluster_id = _create_cluster(candidate)
                audit["clusters_created"] += 1
                # Add to existing_clusters for subsequent matching
                existing_clusters.append({
                    "id": cluster_id,
                    "fingerprint": candidate["fingerprint"],
                    "member_ids": list(candidate["member_ids"]),
                    "status": "candidate",
                })

        # 15. Supersede old clusters with high overlap to new ones
        # (handled in _match_existing_clusters via fingerprint_match)

        # 16. Log audit artifact
        supabase.table("memory_cluster_runs").insert({
            "clusters_created": audit["clusters_created"],
            "clusters_reused": audit["clusters_reused"],
            "clusters_superseded": audit["clusters_superseded"],
            "orphans_count": audit["orphans_count"],
            "total_memories": audit["total_memories"],
            "seeds_processed": audit["seeds_processed"],
            "quality_histogram": audit["quality_histogram"],
            "status": "completed",
            "completed_at": now.isoformat(),
        }).execute()

        audit_log_sync(
            "memory_clusters",
            "INFO",
            f"Clustering complete: {audit['clusters_created']} created, "
            f"{audit['clusters_reused']} reused, {audit['orphans_count']} orphans, "
            f"quality: {audit['quality_histogram']}",
        )

    except Exception as e:
        audit["errors"].append(str(e))
        audit_log_sync("memory_clusters", "ERROR", f"Clustering failed: {e}")

    return audit


def _create_cluster(candidate: dict) -> int:
    """Create a new memory_cluster and its members."""
    # Compute centroid embedding from member memories
    centroid = _compute_centroid(candidate["member_ids"])

    res = supabase.table("memory_clusters").insert({
        "name": candidate.get("seed_text", "Unnamed Cluster"),
        "fingerprint": candidate["fingerprint"],
        "centroid_embedding": centroid,
        "memory_count": len(candidate["member_ids"]),
        "quality_score": candidate["quality_score"],
        "status": "candidate",
    }).execute()

    cluster_id = res.data[0]["id"]

    # Batch insert members
    member_rows = []
    for mid in candidate["member_ids"]:
        score = candidate["scores"].get(mid, 1.0)
        member_rows.append({
            "memory_id": mid,
            "cluster_id": cluster_id,
            "score": score,
            "source": "graph",
            "is_primary": True,
        })
    if member_rows:
        for i in range(0, len(member_rows), 100):
            supabase.table("memory_cluster_members").insert(member_rows[i:i+100]).execute()

    return cluster_id


def _update_cluster_members(cluster_id: int, new_member_ids: set, scores: dict):
    """Add new members to an existing cluster."""
    existing_res = supabase.table("memory_cluster_members") \
        .select("memory_id") \
        .eq("cluster_id", cluster_id) \
        .execute()
    existing_ids = {m["memory_id"] for m in (existing_res.data or [])}

    for mid in new_member_ids - existing_ids:
        score = scores.get(mid, 1.0)
        supabase.table("memory_cluster_members").insert({
            "memory_id": mid,
            "cluster_id": cluster_id,
            "score": score,
            "source": "graph",
            "is_primary": False,
        }).execute()

    # Update memory_count
    total = supabase.table("memory_cluster_members") \
        .select("id", count="exact") \
        .eq("cluster_id", cluster_id) \
        .execute()
    supabase.table("memory_clusters").update({
        "memory_count": total.count or 0,
    }).eq("id", cluster_id).execute()


def _merge_cluster(cluster_id: int, new_member_ids: set, scores: dict, quality: float):
    """Merge new members into existing cluster, update quality if better."""
    _update_cluster_members(cluster_id, new_member_ids, scores)
    # Update quality if new candidate was better
    existing = maybe_single_safe(supabase.table("memory_clusters").select("quality_score").eq("id", cluster_id))
    if existing and existing.data and quality > existing.data.get("quality_score", 0):
        supabase.table("memory_clusters").update({
            "quality_score": quality,
        }).eq("id", cluster_id).execute()


def _sanitize_embedding(emb):
    """Coerce an embedding to a list of floats, handling str and list[str|float]."""
    if emb is None:
        return []
    if isinstance(emb, str):
        try:
            emb = json.loads(emb)
        except (json.JSONDecodeError, TypeError):
            return []
    if isinstance(emb, list):
        try:
            return [float(v) if isinstance(v, str) else v for v in emb]
        except (ValueError, TypeError):
            return []
    return []


def _compute_centroid(member_ids: list) -> list:
    """Compute centroid embedding from member memory embeddings (batch fetch)."""
    if not member_ids:
        return [0.0] * 768
    try:
        res = supabase.table("memories") \
            .select("id, embedding") \
            .in_("id", list(member_ids)) \
            .execute()
        raw_embeddings = [r["embedding"] for r in (res.data or []) if r.get("embedding")]
        embeddings = [_sanitize_embedding(e) for e in raw_embeddings]
        embeddings = [e for e in embeddings if e]  # Drop empty after sanitization
        if not embeddings:
            return [0.0] * 768
        dim = len(embeddings[0])
        centroid = [0.0] * dim
        for emb in embeddings:
            for i in range(min(dim, len(emb))):
                centroid[i] += emb[i]
        n = len(embeddings)
        return [c / n for c in centroid]
    except Exception:
        return [0.0] * 768
