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
                "weight": edge.weight,
                "source_triple_id": edge.source_triple_id,
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


async def upsert_passage_triple_link(passage_id: int, triple_id: int) -> bool:
    """UPSERT a passage-triple link. Idempotent."""
    try:
        supabase.table("retrieval_passage_triple_links") \
            .upsert({
                "passage_id": passage_id,
                "triple_id": triple_id,
            }, on_conflict="passage_id,triple_id") \
            .execute()
        return True
    except Exception as e:
        audit_log_sync("retrieval", "WARNING",
                       f"upsert_passage_triple_link(passage_id={passage_id}, triple_id={triple_id}) failed: {e}")
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

        for node_id, passages in df_map.items():
            df = len(passages)
            spec = (df / n) if n > 0 else 0.5
            spec = min(1.0, max(0.01, spec))

            supabase.table("retrieval_node_stats") \
                .upsert({
                    "node_id": node_id,
                    "df": df,
                    "source_count": df,
                    "specificity_score": 1.0 - spec,
                }, on_conflict="node_id") \
                .execute()

    except Exception as e:
        audit_log_sync("retrieval", "WARNING",
                       f"update_node_stats failed: {e}")


async def build_triple_graph(triples: list, passage_id: int,
                             source_type: str, source_id: str,
                             known_types: Optional[Dict[str, str]] = None):
    """Build phrase nodes and edges from extracted triples for a passage."""
    known_types = known_types or {}

    created_nodes = {}

    for triple in triples:
        sub_id = await upsert_phrase_node(PhraseNode(
            normalized_text=triple.normalized_subject,
            display_text=triple.subject_text,
            node_type=classify_node_type(triple.subject_text, known_types),
        ))
        obj_id = await upsert_phrase_node(PhraseNode(
            normalized_text=triple.normalized_object,
            display_text=triple.object_text,
            node_type=classify_node_type(triple.object_text, known_types),
        ))

        if sub_id and obj_id:
            await upsert_retrieval_edge(RetrievalEdge(
                from_node_id=sub_id,
                to_node_id=obj_id,
                edge_type="related",
                weight=triple.confidence,
                source_triple_id=None,
                source_passage_id=passage_id,
                index_version=INDEX_VERSION,
            ))

        for nid, role in [(sub_id, "subject"), (obj_id, "object")]:
            if nid and passage_id:
                await upsert_passage_phrase_link(PassagePhraseLink(
                    passage_id=passage_id,
                    node_id=nid,
                    role=role,
                    weight=triple.confidence,
                ))

        if sub_id:
            created_nodes[sub_id] = triple.subject_text
        if obj_id:
            created_nodes[obj_id] = triple.object_text

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
