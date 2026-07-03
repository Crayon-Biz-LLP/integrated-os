import asyncio
from typing import Optional, Dict
from core.services.db import get_supabase
from core.retrieval.schema import PhraseNode, RetrievalEdge, AliasEdge, PassagePhraseLink
from core.retrieval.normalizer import classify_node_type
from core.retrieval.config import INDEX_VERSION
from core.lib.audit_logger import audit_log_sync

supabase = get_supabase()


async def upsert_phrase_node(node: PhraseNode) -> Optional[int]:
    """Upsert a phrase node by normalized_text. Returns node ID."""
    if not node.normalized_text:
        return None

    try:
        existing = supabase.table("retrieval_phrase_nodes") \
            .select("id") \
            .eq("normalized_text", node.normalized_text) \
            .maybe_single() \
            .execute()

        if existing and existing.data:
            node_id = existing.data["id"]
            supabase.table("retrieval_phrase_nodes") \
                .update({
                    "last_seen_at": "now()",
                    "display_text": node.display_text,
                }) \
                .eq("id", node_id) \
                .execute()
            return node_id

        if not node.embedding:
            from core.llm import get_embedding
            emb_res = await get_embedding(node.display_text)
            if emb_res:
                node.embedding = emb_res.vector

        payload = {
            "normalized_text": node.normalized_text,
            "display_text": node.display_text,
            "node_type": node.node_type,
            "metadata": node.metadata,
        }
        if node.embedding:
            payload["embedding"] = node.embedding

        result = supabase.table("retrieval_phrase_nodes") \
            .insert(payload) \
            .execute()

        if result and result.data:
            return result.data[0]["id"]
        return None

    except Exception as e:
        audit_log_sync("retrieval", "WARNING",
                       f"upsert_phrase_node failed for '{node.normalized_text}': {e}")
        return None


async def _resolve_node_id(normalized_text: str) -> Optional[int]:
    """Look up a phrase node ID by normalized text."""
    try:
        result = supabase.table("retrieval_phrase_nodes") \
            .select("id") \
            .eq("normalized_text", normalized_text) \
            .maybe_single() \
            .execute()
        return result.data["id"] if result and result.data else None
    except Exception as e:
        audit_log_sync("retrieval", "WARNING",
                       f"_resolve_node_id failed for '{normalized_text}': {e}")
        return None


async def upsert_retrieval_edge(edge: RetrievalEdge) -> bool:
    """UPSERT an edge between phrase nodes. Idempotent."""
    try:
        supabase.table("retrieval_edges") \
            .upsert({
                "from_node_id": edge.from_node_id,
                "to_node_id": edge.to_node_id,
                "edge_type": edge.edge_type,
                "predicate_text": edge.predicate_text,
                "weight": edge.weight,
                "source_passage_id": edge.source_passage_id,
                "index_version": edge.index_version,
            }, on_conflict="from_node_id,to_node_id,edge_type,index_version") \
            .execute()
        return True
    except Exception as e:
        audit_log_sync("retrieval", "WARNING",
                       f"upsert_retrieval_edge failed: {e}")
        return False


async def upsert_alias_edge(edge: AliasEdge) -> bool:
    """UPSERT an alias/synonym edge. Idempotent."""
    try:
        supabase.table("retrieval_alias_edges") \
            .upsert({
                "from_node_id": edge.from_node_id,
                "to_node_id": edge.to_node_id,
                "alias_type": edge.alias_type,
                "weight": edge.weight,
            }, on_conflict="from_node_id,to_node_id,alias_type") \
            .execute()
        return True
    except Exception as e:
        audit_log_sync("retrieval", "WARNING",
                       f"upsert_alias_edge failed: {e}")
        return False


async def upsert_passage_phrase_link(link: PassagePhraseLink) -> bool:
    """UPSERT a passage-phrase link. Idempotent."""
    try:
        supabase.table("retrieval_passage_phrase_links") \
            .upsert({
                "passage_id": link.passage_id,
                "node_id": link.node_id,
                "role": link.role,
                "weight": link.weight,
            }, on_conflict="passage_id,node_id,role") \
            .execute()
        return True
    except Exception as e:
        audit_log_sync("retrieval", "WARNING",
                       f"upsert_passage_phrase_link failed: {e}")
        return False


async def upsert_memory_bundle_link(memory_id: int, passage_id: int) -> bool:
    """UPSERT a memory bundle link. Idempotent."""
    try:
        supabase.table("retrieval_memory_bundle_links") \
            .upsert({
                "memory_id": memory_id,
                "passage_id": passage_id,
                "index_version": INDEX_VERSION,
            }, on_conflict="memory_id,passage_id") \
            .execute()
        return True
    except Exception as e:
        audit_log_sync("retrieval", "WARNING",
                       f"upsert_memory_bundle_link(memory_id={memory_id}, passage_id={passage_id}) failed: {e}")
        return False


async def update_node_stats():
    """Recompute DF/specificity for all phrase nodes.
    
    DF = number of distinct passages mentioning the node.
    Specificity = log(N / df) / log(N) where N = total passages.
    """
    try:
        total = supabase.table("retrieval_passages") \
            .select("id", count="exact") \
            .execute()
        n = total.count if total and total.count else 1

        links = supabase.table("retrieval_passage_phrase_links") \
            .select("node_id, passage_id", count="exact") \
            .execute()

        if not links or not links.data:
            return

        df_map: Dict[int, set] = {}
        for row in links.data:
            nid = row["node_id"]
            pid = row.get("passage_id")
            if nid not in df_map:
                df_map[nid] = set()
            df_map[nid].add(pid)

        records = []
        for node_id, passages in df_map.items():
            df = len(passages)
            spec = (df / n) if n > 0 else 0.5
            spec = min(1.0, max(0.01, spec))

            records.append({
                "node_id": node_id,
                "df": df,
                "source_count": df,
                "specificity_score": 1.0 - spec,
            })

        if records:
            supabase.table("retrieval_node_stats") \
                .upsert(records, on_conflict="node_id") \
                .execute()

    except Exception as e:
        audit_log_sync("retrieval", "WARNING",
                       f"update_node_stats failed: {e}")


async def build_triple_graph(triples: list, passage_id: int,
                             source_type: str, source_id: str,
                             known_types: Optional[Dict[str, str]] = None):
    """Build phrase nodes and edges from extracted triples for a passage.

    Batches DB operations: resolves all nodes in one query, batch-upserts
    new nodes, edges, and links to minimize sequential HTTP round-trips.
    """
    known_types = known_types or {}
    if not triples:
        return

    # Step 1: Collect all unique normalized texts for batch resolution
    all_texts: dict[str, str] = {}  # normalized_text -> display_text
    for t in triples:
        all_texts[t.normalized_subject] = t.subject_text
        all_texts[t.normalized_object] = t.object_text

    # Step 2: Batch-resolve existing nodes
    existing: dict[str, int] = {}
    try:
        rows = supabase.table("retrieval_phrase_nodes") \
            .select("id, normalized_text") \
            .in_("normalized_text", list(all_texts.keys())) \
            .execute()
        for r in (rows.data or []):
            existing[r["normalized_text"]] = r["id"]
    except Exception as e:
        audit_log_sync("retrieval", "WARNING",
                       f"build_triple_graph batch node resolve failed: {e}")

    # Step 3: Create new nodes (parallel embeddings)
    new_texts = [t for t in all_texts if t not in existing]
    new_nodes: dict[str, int] = {}
    if new_texts:
        async def create_and_get_id(text: str) -> tuple[str, int | None]:
            nid = await upsert_phrase_node(PhraseNode(
                normalized_text=text,
                display_text=all_texts[text],
                node_type=classify_node_type(all_texts[text], known_types),
            ))
            return (text, nid)

        results = await asyncio.gather(*[create_and_get_id(t) for t in new_texts],
                                       return_exceptions=True)
        for r in results:
            if isinstance(r, tuple):
                text, nid = r
                if nid:
                    new_nodes[text] = nid
            elif isinstance(r, Exception):
                audit_log_sync("retrieval", "WARNING",
                               f"build_triple_graph node creation failed: {r}")

    # Build node_id lookup
    node_ids: dict[str, int] = {**existing, **new_nodes}

    # Step 4: Batch-upsert edges
    edges = []
    for t in triples:
        sub_id = node_ids.get(t.normalized_subject)
        obj_id = node_ids.get(t.normalized_object)
        if sub_id and obj_id:
            edges.append({
                "from_node_id": sub_id,
                "to_node_id": obj_id,
                "edge_type": "related",
                "predicate_text": t.predicate_text,
                "weight": t.confidence,
                "source_passage_id": passage_id,
                "index_version": INDEX_VERSION,
            })

    if edges:
        seen = {}
        for e in edges:
            key = (e["from_node_id"], e["to_node_id"], e["edge_type"], e["index_version"])
            if key not in seen or e["weight"] > seen[key]["weight"]:
                seen[key] = e
        unique_edges = list(seen.values())
        try:
            supabase.table("retrieval_edges") \
                .upsert(unique_edges, on_conflict="from_node_id,to_node_id,edge_type,index_version") \
                .execute()
        except Exception as e:
            audit_log_sync("retrieval", "WARNING",
                           f"build_triple_graph batch edge upsert failed: {e}")

    # Step 5: Batch-upsert passage-phrase links
    links = []
    for t in triples:
        for text, role in [(t.normalized_subject, "subject"),
                           (t.normalized_object, "object")]:
            nid = node_ids.get(text)
            if nid and passage_id:
                links.append({
                    "passage_id": passage_id,
                    "node_id": nid,
                    "role": role,
                    "weight": t.confidence,
                })

    if links:
        seen = {}
        for lnk in links:
            key = (lnk["passage_id"], lnk["node_id"], lnk["role"])
            if key not in seen or lnk["weight"] > seen[key]["weight"]:
                seen[key] = lnk
        unique_links = list(seen.values())
        try:
            supabase.table("retrieval_passage_phrase_links") \
                .upsert(unique_links, on_conflict="passage_id,node_id,role") \
                .execute()
        except Exception as e:
            audit_log_sync("retrieval", "WARNING",
                           f"build_triple_graph batch link upsert failed: {e}")

    # Step 6: Alias linking (sequential, per-node)
    created_nodes: dict[int, str] = {}
    for text, nid in node_ids.items():
        if text in all_texts:
            created_nodes[nid] = all_texts[text]

    for nid, display_text in created_nodes.items():
        await _link_textual_aliases(nid, display_text)


async def _link_textual_aliases(node_id: int, display_text: str):
    """Create alias edges between a node and textually similar existing nodes.

    Finds existing phrase nodes sharing key terms and links them via
    bidirectional alias edges to improve PPR subgraph connectivity.
    """
    if not node_id or not display_text:
        return
    terms = [w for w in display_text.lower().split() if len(w) >= 3]
    if not terms:
        return
    for term in terms[:2]:
        try:
            similar = supabase.table("retrieval_phrase_nodes") \
                .select("id") \
                .ilike("normalized_text", f"%{term}%") \
                .neq("id", node_id) \
                .limit(3) \
                .execute()
            if similar and similar.data:
                for row in similar.data:
                    if row["id"] != node_id:
                        await upsert_alias_edge(AliasEdge(
                            from_node_id=node_id,
                            to_node_id=row["id"],
                            alias_type="heuristic",
                        ))
        except Exception:
            continue
