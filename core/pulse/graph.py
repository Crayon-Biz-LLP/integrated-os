from core.llm.constants import SYNTHESIS_MODEL
from core.services.db import exec_query, maybe_single_safe, tenant_aware_client
from core.llm import get_embedding
from core.llm.fallback import generate_content_with_fallback
import json
import asyncio
import uuid
import difflib
import re
from typing import Optional
from core.lib.audit_logger import audit_log_sync
from core.lib.telemetry import emit_observation
from core.services.briefing_refresh import fire_briefing_refresh
from core.lib.graph_rules import find_similar_node, resolve_alias, canonicalize_relationship, normalize_label_display, get_canonical_id, normalize_label, NOISE_LABELS, insert_pending_edge, make_memory_preview
from core.decisions import record_decision
from core.lib.node_tables import resolve_merge_proposal

supabase = tenant_aware_client()


def is_valid_uuid(val: str) -> bool:
    """Check if a value is a valid UUID string."""
    if not val:
        return False
    try:
        uuid.UUID(str(val))
        return True
    except ValueError:
        return False



TYPE_TO_DANNY_EDGE = {
    'person': 'KNOWS',
    'organization': 'WORKS_WITH',
    'place': 'RELATES_TO',
    'event': 'ATTENDED',
    'emotional_state': 'FEELS',
}


def _person_org_from_source_text(source_text: str, live_orgs: list[str]) -> tuple[Optional[str], Optional[str]]:
    """Resolve a person's org from message text alone (Bug 2 fix, tiers 2+3).

    Tier 2: affiliation pattern — "<Person> ... from/at/of <Org>" wins over any
            other mention of a different org elsewhere in the text.
    Tier 3: hardened word-boundary containment, longest label first, with
            canonical-alias fallback.

    Returns (org_label | None, match_source | None).
    """
    if not source_text or not source_text.strip():
        return None, None
    source_lower = source_text.lower()
    # Tier 2: affiliation pattern, longest label first
    for oname in sorted(live_orgs, key=len, reverse=True):
        if re.search(rf'\b(?:from|at|of)\s+{re.escape(oname.lower())}\b', source_lower):
            return oname, "affiliation_pattern"
    # Tier 3: word-boundary containment, longest label first
    for oname in sorted(live_orgs, key=len, reverse=True):
        o_lower = oname.lower()
        if re.search(rf'\b{re.escape(o_lower)}\b', source_lower):
            return oname, "substring"
        canonical = resolve_alias(oname)
        if canonical != oname and re.search(rf'\b{re.escape(canonical.lower())}\b', source_lower):
            return oname, "substring_alias"
    return None, None


def _label_word_regex(label: str) -> "re.Pattern":
    """Whole-word matcher for an entity label.

    Bug 9 hardening: SQL ilike('%label%') also matches substrings — "David"
    hitting "Davidson". Every backfill scan must post-filter through this.
    """
    return re.compile(rf'\b{re.escape(label.lower())}\b')


def match_existing_nodes(entities: list[dict], owner_id: str) -> list[dict]:
    """Find existing graph nodes (and pending nodes) that match suggested entities.
    Filters out the owner's own person nodes.
    Returns entities enriched with `existing_matches` array.
    """
    if not entities:
        return []

    # Get owner name to filter it out
    owner_name_lower = ""
    res_user = supabase.table("users").select("name").eq("id", owner_id).execute()
    if res_user.data and res_user.data[0].get("name"):
        owner_name_lower = str(res_user.data[0]["name"]).lower().strip()

    # Fetch live nodes — entity types only. graph_nodes also carries task and
    # memory nodes (thousands, ever-growing); fetching them unfiltered hits
    # PostgREST's default 1000-row page cap and silently truncates the result,
    # so older org/person nodes vanish from matching entirely (Aug 25 root
    # cause: Solvstrat unmatched despite existing). The matcher loop below can
    # only ever use these types anyway.
    #
    # Pagination: even with the entity-type filter, a tenant with >1000
    # person+org+place+event+emotional_state nodes would be silently
    # truncated. We page through results in chunks of 1000 to guarantee
    # completeness. (Aug 27 hardening.)
    _ENTITY_NODE_TYPES = ["person", "organization", "place", "event", "emotional_state"]
    _PAGE_SIZE = 1000
    live_nodes = []
    offset = 0
    while True:
        page = (supabase.table("graph_nodes")
                .select("id, label, type")
                .in_("type", _ENTITY_NODE_TYPES)
                .eq("is_current", True)
                .range(offset, offset + _PAGE_SIZE - 1)
                .execute())
        rows = page.data or []
        live_nodes.extend(rows)
        if len(rows) < _PAGE_SIZE:
            break
        offset += _PAGE_SIZE

    # Fetch pending nodes — same entity-type restriction and pagination.
    pending_nodes = []
    offset = 0
    while True:
        page = (supabase.table("pending_nodes")
                .select("id, label, node_type")
                .in_("node_type", _ENTITY_NODE_TYPES)
                .in_("status", ["pending", "flagged"])
                .range(offset, offset + _PAGE_SIZE - 1)
                .execute())
        rows = page.data or []
        pending_nodes.extend(rows)
        if len(rows) < _PAGE_SIZE:
            break
        offset += _PAGE_SIZE

    enriched = []
    for ent in entities:
        label = ent.get("label", "")
        node_type = ent.get("type", "")
        if not label:
            enriched.append(ent)
            continue
            
        target_lower = label.lower().strip()
        
        # Auto-exclude owner by label or canonical alias
        if owner_name_lower and (target_lower == owner_name_lower or target_lower == resolve_alias(owner_name_lower).lower()):
            continue

        # ── Tiered matching (Aug 27 hardening) ─────────────────────────
        # 1. Exact match (case-insensitive) → auto-link, zero cost
        # 2. Multiple exact matches → ambiguous, show options in card
        # 3. Fuzzy match (threshold 0.85) → auto-link, no substring boost
        # 4. Semantic match (embeddings) → for genuinely ambiguous cases
        #
        # Never guess when there's ambiguity — show options, let user pick.
        # (Pattern from Notion, Mem.ai, Tana: exact match → options → never guess.)

        exact_matches = []
        fuzzy_matches = []

        for n in live_nodes:
            n_type = n.get("type", "")
            if n_type == node_type or n_type == "person":
                c = n.get("label", "")
                c_lower = c.lower().strip()
                if target_lower == c_lower:
                    exact_matches.append({
                        "id": str(n["id"]),
                        "label": c,
                        "type": n_type,
                        "scope": "live",
                        "score": 1.0,
                        "match_type": "exact",
                    })
                else:
                    ratio = difflib.SequenceMatcher(None, target_lower, c_lower).ratio()
                    if ratio >= 0.85:
                        fuzzy_matches.append({
                            "id": str(n["id"]),
                            "label": c,
                            "type": n_type,
                            "scope": "live",
                            "score": round(ratio, 3),
                            "match_type": "fuzzy",
                        })

        for p in pending_nodes:
            p_type = p.get("node_type", "")
            if p_type == node_type or p_type == "person":
                c = p.get("label", "")
                c_lower = c.lower().strip()
                if target_lower == c_lower:
                    exact_matches.append({
                        "id": str(p["id"]),
                        "label": c,
                        "type": p_type,
                        "scope": "pending",
                        "score": 1.0,
                        "match_type": "exact",
                    })
                else:
                    ratio = difflib.SequenceMatcher(None, target_lower, c_lower).ratio()
                    if ratio >= 0.85:
                        fuzzy_matches.append({
                            "id": str(p["id"]),
                            "label": c,
                            "type": p_type,
                            "scope": "pending",
                            "score": round(ratio, 3),
                            "match_type": "fuzzy",
                        })

        # Deduplicate by ID
        def _dedup(matches):
            seen = set()
            result = []
            for m in matches:
                if m["id"] not in seen:
                    seen.add(m["id"])
                    result.append(m)
            return result

        exact_matches = _dedup(exact_matches)
        fuzzy_matches = _dedup(fuzzy_matches)

        # Tier 1: Single exact match → auto-link
        if len(exact_matches) == 1:
            ent_copy = dict(ent)
            ent_copy["existing_matches"] = exact_matches
            enriched.append(ent_copy)
            continue

        # Tier 2: Multiple exact matches → ambiguous, show all options
        if len(exact_matches) > 1:
            ent_copy = dict(ent)
            ent_copy["existing_matches"] = exact_matches
            ent_copy["ambiguous"] = True
            ent_copy["ambiguous_reason"] = f"{len(exact_matches)} nodes named '{label}'"
            enriched.append(ent_copy)
            continue

        # Tier 3: Fuzzy match (threshold 0.85)
        if fuzzy_matches:
            ent_copy = dict(ent)
            ent_copy["existing_matches"] = fuzzy_matches
            enriched.append(ent_copy)
            continue

        # Tier 4: No match → entity is new (semantic matching deferred to caller)
        ent_copy = dict(ent)
        ent_copy["existing_matches"] = []
        enriched.append(ent_copy)

    # Observability: the fetch/match boundary was invisible for weeks — a
    # silent 1000-row truncation took multi-round archaeology to diagnose.
    # One line makes empty-fetch vs no-match instantly distinguishable.
    matched_count = sum(1 for e in enriched if e.get('existing_matches'))
    audit_log_sync(
        "pulse", "INFO",
        f"match_existing_nodes: entities={len(entities)} "
        f"live_nodes={len(live_nodes)} pending_nodes={len(pending_nodes)} "
        f"matched={matched_count}"
    )
    # Early warning: if entity node count approaches the page size, flag it
    # before silent truncation recurs. (Aug 27 hardening.)
    if len(live_nodes) >= _PAGE_SIZE * 0.8:
        audit_log_sync(
            "pulse", "WARNING",
            f"match_existing_nodes: live_nodes={len(live_nodes)} "
            f"approaching page cap ({_PAGE_SIZE}) — consider archiving old entities"
        )

    return enriched


async def disambiguate_entity(label: str, candidates: list[dict], source_text: str = "") -> list[dict]:
    """Disambiguate multiple nodes with the same name using semantic similarity.

    Called when match_existing_nodes returns ambiguous=True (multiple exact matches).
    Uses embedding similarity to rank candidates by relevance to the message context.

    Returns candidates sorted by semantic score (highest first).
    Each candidate gets a `semantic_score` field.
    """
    if len(candidates) <= 1:
        return candidates

    if not source_text:
        # No context to disambiguate — return all with equal score
        for c in candidates:
            c["semantic_score"] = 0.5
        return candidates

    try:
        from core.llm import get_embedding
        import numpy as np

        # Embed the source text (the message context)
        msg_embedding = await get_embedding(source_text)
        if msg_embedding is None:
            for c in candidates:
                c["semantic_score"] = 0.5
            return candidates

        msg_vec = np.array(msg_embedding)

        # For each candidate, build a context string from their graph edges
        ranked = []
        for c in candidates:
            node_id = c["id"]
            # Build context: label + connected nodes (org, people)
            context_parts = [c["label"]]
            try:
                edges_res = supabase.table("graph_edges").select(
                    "relationship, target_node_id"
                ).eq("source_node_id", node_id).eq("is_current", True).execute()
                for edge in (edges_res.data or []):
                    rel = edge.get("relationship", "")
                    target_id = edge.get("target_node_id", "")
                    if target_id:
                        node_res = supabase.table("graph_nodes").select("label").eq("id", target_id).execute()
                        if node_res.data:
                            context_parts.append(f"{rel} {node_res.data[0]['label']}")
            except Exception:
                pass

            context_str = ", ".join(context_parts)
            node_embedding = await get_embedding(context_str)
            if node_embedding is None:
                c["semantic_score"] = 0.5
            else:
                node_vec = np.array(node_embedding)
                # Cosine similarity
                similarity = float(np.dot(msg_vec, node_vec) / (np.linalg.norm(msg_vec) * np.linalg.norm(node_vec)))
                c["semantic_score"] = round(similarity, 3)

            ranked.append(c)

        # Sort by semantic score (highest first)
        ranked.sort(key=lambda x: x.get("semantic_score", 0), reverse=True)
        return ranked

    except Exception as e:
        audit_log_sync("pulse", "WARNING", f"disambiguate_entity failed: {e}")
        for c in candidates:
            c["semantic_score"] = 0.5
        return candidates


def resolve_matching_pending_nodes(
    label: str,
    node_type: str,
    graph_node_id: str,
    owner_id: str = None,
) -> int:
    """Resolve same-label pending rows now that a live node exists (Step 2 orphan fix).

    Every path that creates a live graph node funnels through
    create_graph_node_with_db_record; any pending row with the same label/type
    was queued by an earlier decision-gated step (or left over from legacy
    extraction). Resolving it here guarantees a pending row can never stay
    'pending' once its label is live: it stops re-surfacing in Quick
    Confirmation, and its pending_org_id links (tasks/memories/enrichment
    jobs) are re-pointed to the live node via _resolve_pending_org_on_approval.

    Idempotent: rows already approved/merged/rejected are skipped, and callers
    that later re-mark their own row (process_graph_pending_decision) find a
    no-op.

    Returns the number of pending rows resolved.
    """
    resolved = 0
    try:
        from core.services.db import get_tenant
        owner = owner_id or get_tenant()
        p_res = supabase.table('pending_nodes') \
            .select('id, label, node_type') \
            .eq('owner_id', owner) \
            .eq('node_type', node_type) \
            .ilike('label', label) \
            .in_('status', ['pending', 'flagged', 'awaiting_details']) \
            .execute()
        for p in (p_res.data or []):
            p_id = p['id']
            supabase.table('pending_nodes').update({'status': 'approved'}).eq('id', p_id).execute()
            if node_type == 'organization':
                _resolve_pending_org_on_approval(p_id, graph_node_id)
            audit_log_sync("pulse", "INFO",
                f"Resolved same-label pending #{p_id} ('{label}') -> live node {graph_node_id}")
            resolved += 1
    except Exception as e:
        audit_log_sync("pulse", "WARNING",
            f"Failed to resolve matching pending nodes for '{label}': {e}")
    return resolved


async def create_graph_node_with_db_record(
    label: str,
    node_type: str,
    source_text: str = "",
    context: str = None,
    source_tag: str = "pending_approval",
    force: bool = False,
    entity_context=None
) -> dict:
    """Create a domain table row + graph_nodes entry + root edge.

    Two modes:
    - Person: creates people row → graph_nodes with people_id → root KNOWS edge
    - Organization: creates organizations row → graph_nodes with org_id → root MEMBER_OF edge
    - Other (event, place, etc.): graph_nodes only, no domain table
    """
    try:
        label = label.strip()
        
        # Apply alias resolution (e.g. Yashwant Daniel -> Danny)
        if node_type == 'person':
            label = label.title()
            label = resolve_alias(label)

        if not force:
            similar = find_similar_node(label, node_type)
            if similar:
                top = similar[0]
                return {"success": True, "action": "merge_proposed",
                        "message": f"Found similar {node_type} '{top['label']}' (score={top['score']}). "
                                   f"Merge proposed — review in Decisions UI.",
                        "merge_candidate_id": top["id"]}

        if node_type == 'person':
            # Graph node only — the people mirror table was removed (migration 75).
            # The node's own UUID is now the person's canonical id.
            # Read the existing node FIRST: on re-approval the upsert below would
            # otherwise replace metadata wholesale and wipe previously-learned
            # enrichment (organization_name, last_interaction_date).
            existing_meta = {}
            try:
                ex_res = maybe_single_safe(supabase.table('graph_nodes').select('metadata').eq('type', 'person').eq('normalized_label', normalize_label(label)))
                if ex_res and ex_res.data:
                    em = ex_res.data.get('metadata') or {}
                    if isinstance(em, str):
                        try:
                            em = json.loads(em)
                        except Exception:
                            em = {}
                    existing_meta = em
            except Exception:
                pass

            upsert_res = supabase.table("graph_nodes").upsert(
                {
                    "label": label,
                    "type": "person",
                    "epistemic_status": "asserted",
                    "normalized_label": normalize_label(label),
                    "db_record_id": None,
                    "metadata": {
                        **existing_meta,
                        "source": source_tag,
                        "memory_id": source_text,
                    }
                },
                on_conflict="owner_id, normalized_label, type"
            ).execute()

            if not upsert_res or not upsert_res.data:
                raise Exception("Supabase upsert returned no data for graph_nodes")
            graph_node_id = upsert_res.data[0].get('id')
            if not graph_node_id:
                raise Exception("Graph node id missing after upsert")
            audit_log_sync("pulse", "INFO", f"Person node ready: '{label}' (node {graph_node_id})")

            # Resolve org for the pending edge + enrichment (Bug 2 fix).
            # Priority: 1) entity_context's primary org (LLM already disambiguated),
            # 2) affiliation pattern ("<Person> ... from/at/of <Org>"),
            # 3) hardened substring match (longest-label-first, word-boundary).
            matched_org_name = None
            match_source = None
            ec_org_name = (getattr(entity_context, "organization_name", None) or "").strip()
            if ec_org_name:
                matched_org_name = ec_org_name
                match_source = "entity_context"
            if not matched_org_name and source_text and source_text.strip() not in ("", "batch"):
                orgs_res = supabase.table('graph_nodes').select('label').eq('type', 'organization').eq('is_current', True).execute()
                live_orgs = []
                for o in (orgs_res.data or []):
                    oname = (o.get('label') or '').strip()
                    if oname and oname.lower() not in NOISE_LABELS:
                        live_orgs.append(oname)
                matched_org_name, match_source = _person_org_from_source_text(source_text, live_orgs)

            if matched_org_name:
                res = insert_pending_edge(
                    label,
                    matched_org_name,
                    "WORKS_AT",
                    {
                        "source_text": f"post_creation_hook:{source_text[:50]}",
                        "source_table": "graph_nodes",
                        "source_type": "person",
                        "target_type": "organization"
                    }
                )
                audit_log_sync("pulse", "INFO", f"Post-creation hook: Set org '{matched_org_name}' on person '{label}' + proposed WORKS_AT via {match_source} (status: {res.get('status')})")
            else:
                audit_log_sync("pulse", "INFO", f"Post-creation hook: No confident org match found for person {label}.")

            # ── Consolidation (migration 74): merge enrichment onto the node ──
            # Read-modify-write so re-approval never wipes a previously-set
            # organization_name / last_interaction_date.
            try:
                node_meta_res = maybe_single_safe(supabase.table('graph_nodes').select('metadata').eq('id', graph_node_id))
                node_meta = (node_meta_res.data.get('metadata') or {}) if node_meta_res and node_meta_res.data else {}
                if isinstance(node_meta, str):
                    try:
                        node_meta = json.loads(node_meta)
                    except Exception:
                        node_meta = {}
                enrich = dict(node_meta.get('enrichment') or {})
                if context and context.strip():
                    enrich['role'] = context.strip()
                if matched_org_name:
                    enrich['organization_name'] = matched_org_name
                enrich.setdefault('strategic_weight', 5)
                enrich.setdefault('is_active', True)
                # Self-canonical identity (migration 75): the node's own UUID
                # is the person id everywhere — no legacy mirror id exists.
                node_meta['people_id'] = graph_node_id
                node_meta['enrichment'] = enrich
                supabase.table('graph_nodes').update({'metadata': node_meta, 'db_record_id': graph_node_id}).eq('id', graph_node_id).execute()
            except Exception:
                pass

            # Step 2 (orphan fix): a live node now exists for this label — resolve
            # any same-label pending rows so they stop surfacing in Quick Confirmation.
            resolve_matching_pending_nodes(label, node_type, graph_node_id)

            await _ensure_danny_edge(label, node_type)

            # Bridge C: Backfill existing notes/tasks that mention this person
            await _backfill_existing_content_for_entity(
                label=label, node_type='person', db_record_id=graph_node_id
            )

            inferred = []
            if source_text and source_text.strip() not in ("", "batch"):
                inferred = await _infer_additional_edges(label, node_type, source_text)

            msg = f"Approved person '{label}'"
            if matched_org_name:
                msg += f" ({matched_org_name})"
            if context:
                msg += f" ({context.strip()})"
            return {"success": True, "action": "approved", "node_id": graph_node_id, "message": msg, "inferred_edges": inferred}

        else:
            # Organizations: graph node only — the organizations mirror table was
            # removed (migration 75). The node's own UUID is the org id.
            # Merge with existing metadata on re-approval (never wipe enrichment).
            existing_meta = {}
            try:
                ex_res = maybe_single_safe(supabase.table('graph_nodes').select('metadata').eq('type', node_type).eq('normalized_label', normalize_label(label)))
                if ex_res and ex_res.data:
                    em = ex_res.data.get('metadata') or {}
                    if isinstance(em, str):
                        try:
                            em = json.loads(em)
                        except Exception:
                            em = {}
                    existing_meta = em
            except Exception:
                pass
            enrich = dict(existing_meta.get('enrichment') or {})
            enrich.setdefault('is_active', True)

            # Change 2: Register leading token as alias for multi-word orgs
            aliases = list(existing_meta.get('aliases') or [])
            words = label.split()
            if len(words) > 1:
                short_form = words[0]
                # Guard: only add if short_form isn't already a live org
                if short_form.lower() not in [a.lower() for a in aliases] and short_form.lower() != label.lower():
                    try:
                        from core.services.db import get_tenant
                        check_res = supabase.table('graph_nodes').select('id').eq('type', 'organization').ilike('label', short_form).eq('is_current', True).eq('owner_id', get_tenant()).limit(1).execute()
                        if not check_res or not check_res.data:
                            aliases.append(short_form)
                    except Exception:
                        pass

            upsert_res = supabase.table("graph_nodes").upsert(
                {
                    "label": label,
                    "type": node_type,
                    "epistemic_status": "asserted",
                    "normalized_label": normalize_label(label),
                    "db_record_id": None,
                    "metadata": {
                        **existing_meta,
                        "source": source_tag,
                        "memory_id": source_text,
                        "enrichment": enrich,
                        "aliases": aliases,
                    },
                },
                on_conflict="owner_id, normalized_label, type"
            ).execute()

            if not upsert_res or not upsert_res.data:
                raise Exception("Supabase upsert returned no data for graph_nodes")
            graph_node_id = upsert_res.data[0].get('id')
            if not graph_node_id:
                raise Exception("Graph node id missing after upsert")
            audit_log_sync("pulse", "INFO", f"Org node ready: '{label}' (node {graph_node_id})")

            # Self-canonical identity: node's own UUID is the org id
            try:
                node_meta_res = maybe_single_safe(supabase.table('graph_nodes').select('metadata').eq('id', graph_node_id))
                node_meta = (node_meta_res.data.get('metadata') or {}) if node_meta_res and node_meta_res.data else {}
                if isinstance(node_meta, str):
                    try:
                        node_meta = json.loads(node_meta)
                    except Exception:
                        node_meta = {}
                node_meta['organization_id'] = graph_node_id
                supabase.table('graph_nodes').update({'metadata': node_meta, 'db_record_id': graph_node_id}).eq('id', graph_node_id).execute()
            except Exception:
                pass

            # Step 2 (orphan fix): resolve any same-label pending org rows — a live
            # node now exists; re-point pending_org_id on tasks/memories/jobs.
            resolve_matching_pending_nodes(label, node_type, graph_node_id)

            await _ensure_danny_edge(label, node_type)

            # Bridge C: Backfill existing notes/tasks that mention this organization
            if node_type == 'organization':
                await _backfill_existing_content_for_entity(
                    label=label, node_type='organization', db_record_id=graph_node_id
                )

            inferred = []
            if source_text and source_text.strip() not in ("", "batch"):
                inferred = await _infer_additional_edges(label, node_type, source_text)

            msg = f"Approved node '{label}' ({node_type})"
            if node_type == 'organization':
                msg = f"Approved organization '{label}'"
            return {"success": True, "action": "approved", "message": msg, "node_id": graph_node_id, "inferred_edges": inferred}

    except Exception as e:
        audit_log_sync("pulse", "ERROR", f"Error creating graph node with DB record: {e}")
        return {"success": False, "action": "error", "message": str(e)}


async def _backfill_existing_content_for_entity(
    label: str,
    node_type: str,
    db_record_id: str = None,
):
    """Bridge C: After a project/org/node is approved, backfill existing notes and
    tasks that mention this entity's label with the correct metadata.

    Layer 3 of defense in depth:
    Layer 1: entity_linker.resolve_entities() at creation time (create_note_direct / create_task_direct)
    Layer 2: Enrichment queue backfill (_process_note_enrichment / _process_task_graph_enrichment)
    Layer 3: Graph node approval triggers retroactive backfill of all existing content

    Scans memories and tasks (max 100 each) that contain the entity label,
    then updates their metadata to include the newly-created entity IDs.
    This closes the entity lifecycle loop: when a project/org gets a formal
    identity in the graph, ALL existing content referencing it gets linked.
    """
    try:
        if not label or not db_record_id:
            return

        label_lower = label.lower()
        entity_id_field = None
        id_value = None

        if node_type == 'organization':
            entity_id_field = 'organization_id'
            id_value = db_record_id
        elif node_type == 'person':
            entity_id_field = 'people_id'
            id_value = db_record_id
        else:
            return  # Only backfill for entity types that have domain tables

        # ── Backfill memories (notes) that mention this entity label ──
        try:
            mem_res = supabase.table('memories') \
                .select('id, metadata, content') \
                .eq('is_current', True) \
                .eq('memory_type', 'note') \
                .ilike('content', f'%{label_lower}%') \
                .limit(100) \
                .execute()

            if mem_res and mem_res.data:
                backfilled_count = 0
                label_word_pat = _label_word_regex(label_lower)
                for mem in mem_res.data:
                    # Word-boundary check — ilike('%label%') also matches substrings ("David" in "Davidson")
                    if not label_word_pat.search((mem.get('content') or '').lower()):
                        continue
                    current_meta = mem.get('metadata') or {}
                    if isinstance(current_meta, str):
                        try:
                            current_meta = json.loads(current_meta)
                        except Exception:
                            current_meta = {}

                    # Only backfill if this entity ID is not already set
                    existing_val = current_meta.get(entity_id_field)
                    if existing_val and str(existing_val) == str(id_value):
                        continue  # Already has this exact ID

                    # Don't overwrite a different ID (another project/org was already linked)
                    if existing_val and str(existing_val) != str(id_value):
                        continue

                    current_meta[entity_id_field] = str(id_value)
                    # Hardened Sep 2026: no metadata.organization_name write — the
                    # name is a redundant second copy of org identity that
                    # historically diverged from the resolved id (Plumfleet id +
                    # 'Qhord' name). Consumers join to graph_nodes for the label.

                    try:
                        supabase.table('memories') \
                            .update({'metadata': current_meta}) \
                            .eq('id', mem['id']) \
                            .eq('is_current', True) \
                            .execute()
                        backfilled_count += 1
                    except Exception:
                        pass

                if backfilled_count > 0:
                    audit_log_sync(
                        "pulse", "INFO",
                        f"Bridge C: Backfilled {backfilled_count} note(s) with {entity_id_field}={id_value} "
                        f"for '{label}' ({node_type})"
                    )
        except Exception as mem_err:
            audit_log_sync(
                "pulse", "WARNING",
                f"Bridge C: Memory backfill scan failed for '{label}': {mem_err}"
            )

        # ── Backfill open tasks that mention this entity label ──
        try:
            task_res = supabase.table('tasks') \
                .select('id, organization_id, title') \
                .eq('is_current', True) \
                .not_.in_('status', ['done', 'cancelled']) \
                .ilike('title', f'%{label_lower}%') \
                .limit(100) \
                .execute()

            if task_res and task_res.data:
                backfilled_count = 0
                label_word_pat = _label_word_regex(label_lower)
                for task in task_res.data:
                    # Word-boundary check — same substring guard as memory backfill
                    if not label_word_pat.search((task.get('title') or '').lower()):
                        continue
                    update_data = {}

                    if entity_id_field == 'organization_id':
                        existing_org = task.get('organization_id')
                        if existing_org and str(existing_org) == str(id_value):
                            continue
                        if existing_org:
                            continue  # Don't overwrite different org
                        update_data['organization_id'] = id_value

                    if update_data:
                        try:
                            supabase.table('tasks') \
                                .update(update_data) \
                                .eq('id', task['id']) \
                                .execute()
                            backfilled_count += 1
                        except Exception:
                            pass

                if backfilled_count > 0:
                    audit_log_sync(
                        "pulse", "INFO",
                        f"Bridge C: Backfilled {backfilled_count} task(s) with {entity_id_field}={id_value} "
                        f"for '{label}' ({node_type})"
                    )
        except Exception as task_err:
            audit_log_sync(
                "pulse", "WARNING",
                f"Bridge C: Task backfill scan failed for '{label}': {task_err}"
            )

    except Exception as e:
        audit_log_sync(
            "pulse", "WARNING",
            f"Bridge C: Backfill failed for '{label}' ({node_type}): {e}"
        )


def _resolve_pending_org_on_approval(pending_node_id: int, graph_node_id: str):
    """Resolve pending_org_id → organization_id on all tasks and memories.

    Called when a pending org node is approved. Updates all tasks/memories
    that have pending_org_id pointing to this pending node.
    Also creates BELONGS_TO edges for tasks.
    """
    try:
        # Resolve tasks
        tasks = supabase.table('tasks').select('id, title').eq(
            'pending_org_id', pending_node_id
        ).execute()

        for task in (tasks.data or []):
            supabase.table('tasks').update({
                'organization_id': graph_node_id,
                'pending_org_id': None,
            }).eq('id', task['id']).execute()

            # Create BELONGS_TO edge
            from core.lib.graph_rules import insert_pending_edge
            org_res = supabase.table('graph_nodes').select('label').eq(
                'id', graph_node_id
            ).single().execute()
            if org_res and org_res.data:
                insert_pending_edge(
                    task.get('title', ''),
                    org_res.data['label'],
                    "BELONGS_TO",
                    {
                        "source_type": "task",
                        "target_type": "organization",
                        "source_table": "approval_resolution",
                        "source_text": f"resolved from pending_node #{pending_node_id}",
                    }
                )

            audit_log_sync("pulse", "INFO",
                f"Resolved pending_org for task {task['id']}: pending#{pending_node_id} → graph#{graph_node_id}")

        # Resolve memories
        memories = supabase.table('memories').select('id').eq(
            'pending_org_id', pending_node_id
        ).execute()

        for mem in (memories.data or []):
            supabase.table('memories').update({
                'organization_id': graph_node_id,
                'pending_org_id': None,
            }).eq('id', mem['id']).execute()

            audit_log_sync("pulse", "INFO",
                f"Resolved pending_org for memory {mem['id']}: pending#{pending_node_id} → graph#{graph_node_id}")

        # Resolve enrichment jobs
        jobs = supabase.table('pending_enrichment_jobs').select('id').eq(
            'pending_org_id', pending_node_id
        ).execute()

        for job in (jobs.data or []):
            supabase.table('pending_enrichment_jobs').update({
                'related_org_id': graph_node_id,
                'pending_org_id': None,
            }).eq('id', job['id']).execute()

    except Exception as e:
        audit_log_sync("pulse", "WARNING",
            f"Failed to resolve pending_org on approval: {e}")


def _handle_rejected_pending_org(pending_node_id: int):
    """Handle rejected pending org: clear pending_org_id on linked tasks/memories.

    Tries to find a fallback org from the task's notes field.
    If no fallback, clears the link and surfaces in decision pulse.
    """
    try:
        supabase_client = tenant_aware_client()

        # Find tasks with this pending org
        tasks = supabase_client.table('tasks').select('id, title, notes').eq(
            'pending_org_id', pending_node_id
        ).execute()

        for task in (tasks.data or []):
            text = task.get('notes') or task.get('title') or ""
            fallback_org = _find_fallback_org(text, exclude_pending_id=pending_node_id)

            if fallback_org:
                supabase_client.table('tasks').update({
                    'organization_id': fallback_org,
                    'pending_org_id': None,
                }).eq('id', task['id']).execute()
                audit_log_sync("pulse", "INFO",
                    f"Task {task['id']}: fallback org {fallback_org} after rejection of pending#{pending_node_id}")
            else:
                supabase_client.table('tasks').update({
                    'pending_org_id': None,
                }).eq('id', task['id']).execute()
                audit_log_sync("pulse", "WARNING",
                    f"Task {task['id']} needs org after rejection of pending#{pending_node_id}")

        # Find memories with this pending org
        memories = supabase_client.table('memories').select('id').eq(
            'pending_org_id', pending_node_id
        ).execute()

        for mem in (memories.data or []):
            supabase_client.table('memories').update({
                'pending_org_id': None,
            }).eq('id', mem['id']).execute()

        # Find enrichment jobs with this pending org
        jobs = supabase_client.table('pending_enrichment_jobs').select('id').eq(
            'pending_org_id', pending_node_id
        ).execute()

        for job in (jobs.data or []):
            supabase_client.table('pending_enrichment_jobs').update({
                'pending_org_id': None,
            }).eq('id', job['id']).execute()

    except Exception as e:
        audit_log_sync("pulse", "WARNING",
            f"Failed to handle rejected pending org: {e}")


def _find_fallback_org(text: str, exclude_pending_id: int = None) -> Optional[str]:
    """Try to find an existing org from text as a fallback after rejection.

    Uses deterministic entity detection. Returns organization graph_node_id or None.
    """
    if not text:
        return None
    try:
        from core.lib.entity_detector import detect_entities
        entities = detect_entities(text)
        for e in entities:
            if e.type == 'organization' and e.db_id and e.db_id != str(exclude_pending_id):
                return e.db_id
    except Exception:
        pass
    return None


def _root_person_label() -> str | None:
    """The tenant's root person label (their own name), or None.

    Resolution order (mirrors archive_ingest.resolve_root_label): core_config
    'archive_root_label' (admin override) → user_settings name → None. Never
    a hardcoded name — a tenant without a resolvable root simply gets no
    root-anchored edges.
    """
    try:
        cfg = maybe_single_safe(supabase.table("core_config").select("content").eq("key", "archive_root_label"))
        if cfg and cfg.data and cfg.data.get("content"):
            return str(cfg.data["content"]).strip() or None
    except Exception:
        pass
    try:
        from core.services.user_settings import resolve_user_name, current_user_id
        name = resolve_user_name(current_user_id())
        if name:
            return name
    except Exception:
        pass
    return None


async def _ensure_danny_edge(label: str, node_type: str):
    """Create OWNS/KNOWS edge from the ROOT person to the node.

    M5: the root person is the tenant's own (users.name — bootstrap_tenant
    creates their person node), resolved per-tenant — never hardcoded.
    """
    rel = TYPE_TO_DANNY_EDGE.get(node_type)
    if not rel:
        return
    try:
        root_name = _root_person_label()
        if not root_name:
            return  # no root person resolvable → no root edge
        root_res = maybe_single_safe(supabase.table("graph_nodes").select("id").eq("type", "person").ilike("label", root_name).eq('is_current', True))
        if not root_res or not root_res.data:
            return
        danny_id = root_res.data["id"]

        label = normalize_label_display(label)
        # Resolve through aliases table (e.g. Sunju → Sunjula Daniel)
        label = resolve_alias(label)
        target_res = maybe_single_safe(supabase.table("graph_nodes").select("id, canonical_id").ilike("label", label).eq('is_current', True))
        if not target_res or not target_res.data:
            return
        target_id = target_res.data["id"]
        # Follow canonical_id chain if this node has been merged into another
        if target_res.data.get("canonical_id"):
            target_id = get_canonical_id(target_id)

        existing = maybe_single_safe(
            supabase.table("graph_edges").select("id")
            .eq("source_node_id", danny_id)
            .eq("target_node_id", target_id)
            .eq("relationship", rel)
            .eq('is_current', True)
        )

        if not existing or not existing.data:
            supabase.table("graph_edges").insert({
                "source_node_id": danny_id,
                "target_node_id": target_id,
                "relationship": rel,
                "weight": 1.0,
                "epistemic_status": "asserted",
                "metadata": {"source": "graph_approval"}
            }).execute()
            # Invalidate edge cache for both nodes (Aug 27 hardening)
            from core.lib.edge_cache import invalidate_node_edges
            from core.services.db import get_tenant
            invalidate_node_edges(get_tenant(), [danny_id, target_id])
    except Exception as e:
        audit_log_sync("pulse", "WARNING", f"Failed to create root edge: {e}")


def _extract_mentioned_labels(source_text: str, known_labels: list[str]) -> list[str]:
    """Return only the known labels that appear (case-insensitive substring) in source_text."""
    source_lower = source_text.lower()
    return [lbl for lbl in known_labels if lbl.lower() in source_lower]


async def _infer_additional_edges(label: str, node_type: str, source_text: str) -> list[str]:
    """Call Gemini to extract additional relationships from the source text involving the new node or mentioned entities."""
    try:
        nodes_res = supabase.table("graph_nodes").select("label").eq('is_current', True).execute()
        if not nodes_res or not nodes_res.data:
            return []
            
        all_labels = [n['label'] for n in nodes_res.data if n.get('label')]
        mentioned = _extract_mentioned_labels(source_text, all_labels)
        
        if not mentioned:
            return []
            
        prompt = f"""
Source text: "{source_text}"
New node being approved: {label} ({node_type})
Other entities mentioned: {json.dumps(mentioned)}

PROJECT DEFINITION:
- What is NOT a project: GitHub repos, open-source libraries (e.g. Supabase, React), theoretical concepts, events/conferences, generic work terms (e.g. 'code review', 'frontend').
- What IS a project: Specific professional work streams, client engagements, side projects with structure (e.g. 'Acme website redesign', 'Q4 client engagement').

Return a JSON array of edges these entities have with each other or the new node. 
Only include relationships explicitly stated or very strongly implied by the source text.

Existing relationship types include: DISCUSSED_WITH, WORKS_AT, WORKS_ON,
CLIENT_OF, VENDOR_TO, MEMBER_OF, PARENT_OF, SPOUSE_OF, SIBLING_OF,
FAMILY_OF, PET_OF, FRIEND_OF, MET_WITH, INTRODUCED, MENTORS, SERVES_AT.
You can invent new types only if none of these fit — prefer reuse.

Format:
[
  {{"source_label": "...", "target_label": "...", "relationship": "..."}}
]
"""
        response = await generate_content_with_fallback(
            prompt=prompt,
            system_instruction="You are a graph extraction engine. Output raw JSON array only. No markdown formatting. No explanation.",
            model=SYNTHESIS_MODEL,
            temperature=0.0
        )
        
        # Clean response and parse (generate_content_with_fallback returns LLMResponse, not str)
        content = (getattr(response, "text", None) or "").strip()
        if not content:
            audit_log_sync("pulse", "WARNING",
                f"Gap D: LLM edge inference returned empty response for {label} ({node_type}). "
                f"provider={getattr(response, 'provider', '?')}, model={getattr(response, 'model', '?')}, "
                f"success={getattr(response, 'success', '?')}, degraded={getattr(response, 'degraded', '?')}, "
                f"reason={getattr(response, 'degraded_reason', '?')}, attempts={getattr(response, 'attempts', '?')}")
            return []
        if content.startswith("```json"):
            content = content[7:-3]
        elif content.startswith("```"):
            content = content[3:-3]
            
        try:
            edges_to_create = json.loads(content)
        except json.JSONDecodeError:
            audit_log_sync("pulse", "WARNING", f"Gap D: LLM inference returned unparseable JSON for {label} ({node_type}): {content[:200]}")
            return []
            
        inferred = []
        for e in edges_to_create:
            s_label = normalize_label_display(e.get('source_label'))
            t_label = normalize_label_display(e.get('target_label'))
            rel = e.get('relationship')
            if not s_label or not t_label or not rel:
                continue
                
            if s_label == t_label:
                continue
                
            rel = rel.upper()
                
            from core.lib.graph_rules import insert_pending_edge
            insert_pending_edge(
                s_label,
                t_label,
                rel,
                {
                    "source_text": "graph_approval_inference",
                    "source_table": "pulse_inference",
                    # type gets resolved inside insert_pending_edge if not provided, or we can look it up
                }
            )
            inferred.append(f"{s_label} → {rel} → {t_label}")
                
        return inferred
    except Exception as err:
        audit_log_sync("pulse", "WARNING", f"Error inferring edges: {err}")
        return []


async def process_graph_pending_decision(pending_id: int, decision: str, context: str = None, new_label: str = None, auto_decided: bool = False) -> dict:
    """Process a pending node decision (approve/reject/unreject)."""
    try:
        pending_res = maybe_single_safe(supabase.table('pending_nodes').select('*').eq('id', pending_id))
        if not pending_res or not pending_res.data:
            return {"success": False, "action": "not_found", "message": "Graph item not found."}
        pending_item = pending_res.data

        raw_type = pending_item.get('node_type', 'concept')
        status = pending_item.get('status', 'pending')

        if status not in ('pending', 'awaiting_details', 'flagged', 'merge_proposed') and decision != 'unreject':
            return {"success": False, "action": "already_processed", "message": "Already processed."}

        # ── Unreject ──
        if decision == 'unreject':
            if status != 'rejected':
                return {"success": False, "action": "not_rejected", "message": "Item is not rejected."}
            supabase.table('pending_nodes').update({'status': 'pending'}).eq('id', pending_id).execute()
            return {"success": True, "action": "unrejected", "message": f"Un-rejected node {pending_item['label']}"}

        # ── Reject ──
        if decision == 'reject':
            label = pending_item['label']
            supabase.table('pending_nodes').update({'status': 'rejected'}).eq('id', pending_id).execute()
            # Cascade reject edges
            supabase.table('pending_graph_edges').update({'status': 'rejected'}).eq('source_label', label).execute()
            supabase.table('pending_graph_edges').update({'status': 'rejected'}).eq('target_label', label).execute()
            try:
                record_decision(
                    decision_type="graph_node_rejection",
                    title=f"Rejected {raw_type}: {label}",
                    context=f"Pending node #{pending_id} rejected.",
                    entity_type="graph_node",
                    entity_id=str(pending_id),
                    confidence=1.0,
                    source="decision_pulse",
                    auto_decided=auto_decided,
                    # Vision #4: persist the EXACT decision-time features so
                    # emit_undo_correction can demote the pattern on undo
                    # (must match the emit_observation call below).
                    metadata={
                        'learn_features': {"node_type": raw_type},
                        'learn_subsystem': 'entity_extraction',
                    },
                )
            except Exception as dec_err:
                audit_log_sync("pulse", "WARNING", f"Failed to record graph node rejection: {dec_err}")
            await emit_observation(
                subsystem='entity_extraction',
                event_type='rejection',
                features={"node_type": raw_type},
                predicted=raw_type,
                actual='rejected',
                outcome='rejected',
                source='decision_pulse'
            )
            fire_briefing_refresh(source="graph_node_decision")

            # ── Handle rejected pending org: clear pending_org_id on linked tasks/memories ──
            if raw_type == 'organization':
                _handle_rejected_pending_org(pending_id)

            return {"success": True, "action": "rejected", "message": f"Rejected node and related edges for {label}"}

        # ── Merge Proposed: Approve = accept merge, Reject = create standalone ──
        if status == 'merge_proposed':
            merge_proposals_res = supabase.table('merge_proposals').select('*').eq('origin_table', 'pending_nodes').eq('origin_id', pending_id).eq('status', 'proposed').limit(1).execute()
            mp = (merge_proposals_res.data or [None])[0]
            if decision == 'approve':
                if mp:
                    from core.lib.graph_rules import execute_graph_node_merge, get_canonical_id
                    label = pending_item['label']
                    node_res = maybe_single_safe(supabase.table('graph_nodes').select('id').ilike('label', label).eq('is_current', True))
                    source_node_id = node_res.data['id'] if node_res and node_res.data else None
                    if source_node_id:
                        winner_id = get_canonical_id(mp['target_node_id'])
                        execute_graph_node_merge(source_node_id, winner_id, 'merge_accept')
                    supabase.table('pending_nodes').update({'status': 'approved'}).eq('id', pending_id).execute()
                    resolve_merge_proposal(mp['id'], 'accepted')
                fire_briefing_refresh(source="graph_node_decision")
                return {"success": True, "action": "merged", "message": f"Merged '{pending_item['label']}' into target node."}
            elif decision == 'reject':
                label = pending_item['label']
                node_type = raw_type
                result = await create_graph_node_with_db_record(label=label, node_type=node_type,
                    source_text=pending_item.get('source_text', ''), context=context, source_tag='pending_approval', force=True)
                if result.get('success'):
                    supabase.table('pending_nodes').update({'status': 'approved'}).eq('id', pending_id).execute()
                    if mp:
                        resolve_merge_proposal(mp['id'], 'rejected')
                    fire_briefing_refresh(source="graph_node_decision")
                return result

        # ── Approve ──
        if decision == 'approve':
            label = pending_item['label']
            node_type = raw_type
            source_text = pending_item.get('source_text', '')

            # If label was edited, rewrite pending_graph_edges and pending_nodes
            if new_label and new_label.strip() and new_label.strip() != label:
                old_label = label
                label = new_label.strip()
                supabase.table('pending_graph_edges').update({'source_label': label}).eq('source_label', old_label).execute()
                supabase.table('pending_graph_edges').update({'target_label': label}).eq('target_label', old_label).execute()
                supabase.table('pending_nodes').update({'label': label, 'status': status}).eq('id', pending_id).execute()

            # Auto-approve any pending root→KNOWS edge for this label
            root_label = _root_person_label()
            danny_edge_res = (
                maybe_single_safe(
                    supabase.table("pending_graph_edges")
                    .select("id")
                    .eq("source_label", root_label)
                    .eq("target_label", label)
                    .eq("relationship", "KNOWS")
                    .eq("status", "pending")
                )
                if root_label
                else None
            )
            if danny_edge_res and danny_edge_res.data:
                await process_pending_edge_decision(danny_edge_res.data["id"], "approve", auto_decided=True)

            result = await create_graph_node_with_db_record(
                label=label,
                node_type=node_type,
                source_text=source_text,
                context=context,
                source_tag="pending_approval"
            )

            if result.get('success'):
                if result.get('action') == 'merge_proposed':
                    merge_target_id = result.get('merge_candidate_id')
                    # Get target label from graph_nodes
                    target_res = supabase.table('graph_nodes').select('label').eq('id', merge_target_id).single().execute()
                    target_label = target_res.data['label'] if target_res and target_res.data else merge_target_id
                    # Insert merge_proposal row so the Merges tab shows it
                    supabase.table('merge_proposals').insert({
                        'source_label': label,
                        'source_type': node_type,
                        'target_node_id': merge_target_id,
                        'target_label': target_label,
                        'status': 'proposed',
                        'rationale': f'Auto-proposed: similar {node_type} found during approval of pending node #{pending_id}',
                        'origin_table': 'pending_nodes',
                        'origin_id': pending_id,
                    }).execute()
                    supabase.table('pending_nodes').update({'status': 'merge_proposed'}).eq('id', pending_id).execute()
                else:
                    supabase.table('pending_nodes').update({'status': 'approved'}).eq('id', pending_id).execute()

                    # ── Resolve pending_org_id on tasks and memories ──
                    if node_type == 'organization' and result.get('node_id'):
                        _resolve_pending_org_on_approval(pending_id, result['node_id'])

                    try:
                        record_decision(
                            decision_type="graph_node_approval",
                            title=f"Approved {node_type}: {label}",
                            context=f"Pending node #{pending_id} approved. Source: {source_text[:200] if source_text else 'N/A'}",
                            entity_type="graph_node",
                            entity_id=str(pending_id),
                            confidence=1.0,
                            source="decision_pulse",
                            auto_decided=auto_decided,
                            # Vision #4: persist the EXACT decision-time features so
                            # emit_undo_correction can demote the pattern on undo
                            # (must match the emit_observation call below).
                            metadata={
                                'learn_features': {"node_type": node_type, "has_context": bool(context), "source": pending_item.get('source_tag', 'pending_approval')},
                                'learn_subsystem': 'entity_extraction',
                            },
                        )
                    except Exception as dec_err:
                        audit_log_sync("pulse", "WARNING", f"Failed to record graph node decision: {dec_err}")

            await emit_observation(
                subsystem='entity_extraction',
                event_type='approval',
                features={"node_type": node_type, "has_context": bool(context), "source": pending_item.get('source_tag', 'pending_approval')},
                predicted=node_type,
                actual=node_type,
                outcome='confirmed',
                source='decision_pulse'
            )
            if result.get("success"):
                fire_briefing_refresh(source="graph_node_decision")
                # After org approval, add follow_up signal for relationship linking
                if node_type == 'organization' and result.get('action') != 'merge_proposed':
                    try:
                        orgs_res = supabase.table('graph_nodes') \
                            .select('id, label') \
                            .eq('type', 'organization') \
                            .eq('is_current', True) \
                            .execute()
                        known_orgs = [o for o in (orgs_res.data or []) if o.get('label') != label]
                        if known_orgs:
                            result['follow_up'] = {
                                'type': 'org_relationship',
                                'new_org': {'id': result.get('node_id'), 'label': label},
                                'known_orgs': known_orgs,
                                'message': f'How does {label} relate to your orgs?',
                                'options': ['Vendor', 'Client', 'Partner', 'Standalone']
                            }
                    except Exception as fu_err:
                        audit_log_sync("pulse", "WARNING", f"Failed to build org follow_up: {fu_err}")
            return result

    except Exception as e:
        audit_log_sync("pulse", "ERROR", f"Error processing graph decision: {e}")
        return {"success": False, "action": "error", "message": str(e)}

async def process_pending_edge_decision(pending_id: int, decision: str, new_source: str = None, new_target: str = None, new_rel: str = None, context: str | None = None, auto_decided: bool = False) -> dict:
    try:
        pe_res = maybe_single_safe(supabase.table('pending_graph_edges').select('*').eq('id', pending_id))
        if not pe_res or not pe_res.data:
            return {"success": False, "action": "not_found", "message": "Pending edge not found."}
            
        pe = pe_res.data
        if pe.get('status') != 'pending':
            return {"success": False, "action": "already_processed", "message": "Already processed."}
            
        if decision == 'reject':
            supabase.table('pending_graph_edges').update({
                'status': 'rejected',
                'approval_source': 'auto_approve' if auto_decided else 'hitl'
            }).eq('id', pending_id).execute()
            # Record rejection decision
            try:
                record_decision(
                    decision_type="graph_edge_rejection",
                    title=f"Rejected edge: {pe['source_label']} → {pe['relationship']} → {pe['target_label']}",
                    context=f"Pending edge #{pending_id} rejected.",
                    entity_type="graph_edge",
                    entity_id=str(pending_id),
                    confidence=1.0,
                    source="decision_pulse",
                    auto_decided=auto_decided,
                    # Vision #4: persist the EXACT decision-time features so
                    # emit_undo_correction can demote the pattern on undo
                    # (must match the emit_observation call below).
                    metadata={
                        'learn_features': {"relationship": pe['relationship'], "source_type": pe.get('source_type'), "target_type": pe.get('target_type')},
                        'learn_subsystem': 'entity_extraction',
                    },
                )
            except Exception as dec_err:
                audit_log_sync("pulse", "WARNING", f"Failed to record graph edge rejection: {dec_err}")
            await emit_observation(
                subsystem='entity_extraction',
                event_type='rejection',
                features={"relationship": pe['relationship'], "source_type": pe.get('source_type'), "target_type": pe.get('target_type')},
                predicted=pe['relationship'],
                actual='rejected',
                outcome='rejected',
                source='decision_pulse'
            )
            fire_briefing_refresh(source="graph_edge_decision")
            return {"success": True, "action": "rejected", "message": "Rejected edge."}
            
        if decision == 'approve':
            s_label = normalize_label_display(new_source or pe['source_label'])
            t_label = normalize_label_display(new_target or pe['target_label'])
            rel = (new_rel or pe['relationship']).upper()

            from core.lib.graph_rules import validate_edge
            s_node_res = maybe_single_safe(supabase.table('graph_nodes').select('id, type, label').ilike('label', s_label).eq('is_current', True))
            t_node_res = maybe_single_safe(supabase.table('graph_nodes').select('id, type, label').ilike('label', t_label).eq('is_current', True))

            s_data = getattr(s_node_res, 'data', None)
            t_data = getattr(t_node_res, 'data', None)

            # FUZZY MATCH FALLBACK for person/org (if exact match fails)
            if not s_data and pe.get('source_type') in ('person', 'organization') and len(s_label) > 3:
                fuzzy_res = supabase.table('graph_nodes').select('id, type, label').eq('type', pe['source_type']).ilike('label', f"{s_label} %").eq('is_current', True).execute()
                if fuzzy_res and fuzzy_res.data and len(fuzzy_res.data) == 1:
                    s_data = fuzzy_res.data[0]
                    s_label = s_data['label']
                    audit_log_sync("pulse", "INFO", f"Fuzzy matched source '{pe['source_label']}' to '{s_label}'")
                    
            if not t_data and pe.get('target_type') in ('person', 'organization') and len(t_label) > 3:
                fuzzy_res = supabase.table('graph_nodes').select('id, type, label').eq('type', pe['target_type']).ilike('label', f"{t_label} %").eq('is_current', True).execute()
                if fuzzy_res and fuzzy_res.data and len(fuzzy_res.data) == 1:
                    t_data = fuzzy_res.data[0]
                    t_label = t_data['label']
                    audit_log_sync("pulse", "INFO", f"Fuzzy matched target '{pe['target_label']}' to '{t_label}'")

            if not s_data or not t_data:
                missing = s_label if not s_data else t_label
                supabase.table('pending_graph_edges').update({
                    'status': 'rejected',
                    'approval_source': 'auto_approve' # validation failed
                }).eq('id', pending_id).execute()
                return {"success": False, "action": "missing_node", "message": f"Node '{missing}' doesn't exist."}

            s_id = s_data['id']
            t_id = t_data['id']
            s_type = s_data.get('type')
            t_type = t_data.get('type')

            if s_type and t_type:
                rel = canonicalize_relationship(rel, s_type, t_type)
                vr = validate_edge(s_type, rel, t_type)
                if vr["action"] == "auto_reject":
                    supabase.table('pending_graph_edges').update({
                        'status': 'rejected',
                        'approval_source': 'auto_approve'
                    }).eq('id', pending_id).execute()
                    return {"success": False, "action": "rejected", "message": f"Auto-rejected: {vr['reason']}"}
                elif vr["action"] == "auto_correct":
                    rel = vr["reason"]

            meta = {"source": "pending_edge_approval", "pending_id": pending_id}
            if context:
                meta["context"] = context
                
            # Preserve the origin memory/task references into the permanent graph metadata
            if pe.get('source_text'):
                memories = [m.strip() for m in pe['source_text'].split(',') if m.strip()]
                if memories:
                    meta["contributing_memories"] = memories

            supabase.table('graph_edges').upsert({
                'source_node_id': s_id,
                'target_node_id': t_id,
                'relationship': rel,
                'weight': 1.0,
                'source_ref': pe.get('source_text') or f"pending_edge:{pending_id}",
                'metadata': meta
            }, on_conflict="source_node_id,relationship,target_node_id", ignore_duplicates=True).execute()
            # Invalidate edge cache for both endpoints (Aug 27 hardening)
            from core.lib.edge_cache import invalidate_node_edges
            from core.services.db import get_tenant
            invalidate_node_edges(get_tenant(), [s_id, t_id])
            
            supabase.table('pending_graph_edges').update({
                'status': 'approved',
                'approval_source': 'auto_approve' if auto_decided else 'hitl',
                'source_label': s_label,
                'target_label': t_label,
                'relationship': rel,
                'source_node_id': s_id,
                'target_node_id': t_id
            }).eq('id', pending_id).execute()

            # Record decision
            try:
                record_decision(
                    decision_type="graph_edge_approval",
                    title=f"Approved edge: {s_label} → {rel} → {t_label}",
                    context=f"Pending edge #{pending_id} approved. Source: {(pe.get('source_text') or '')[:200]}",
                    entity_type="graph_edge",
                    entity_id=str(pending_id),
                    confidence=1.0,
                    source="decision_pulse",
                    auto_decided=auto_decided,
                    # Vision #4: persist the EXACT decision-time features so
                    # emit_undo_correction can demote the pattern on undo
                    # (must match the emit_observation call below).
                    metadata={
                        'learn_features': {"relationship": rel, "source_type": s_type or pe.get('source_type'), "target_type": t_type or pe.get('target_type')},
                        'learn_subsystem': 'entity_extraction',
                    },
                )
            except Exception as dec_err:
                audit_log_sync("pulse", "WARNING", f"Failed to record graph edge decision: {dec_err}")

            await emit_observation(
                subsystem='entity_extraction',
                event_type='approval',
                features={"relationship": rel, "source_type": s_type or pe.get('source_type'), "target_type": t_type or pe.get('target_type')},
                predicted=pe['relationship'],
                actual=rel,
                outcome='confirmed'
            )

            # ── Edge approval backfill: Keep DB rows in sync with graph ──
            # When a WORKS_AT edge for a person is approved, backfill
            # people.organization_name.
            try:
                if rel == "WORKS_AT" and pe.get('source_type') == 'person':
                    # Backfill organization_name into the person node's enrichment
                    t_node_res = supabase.table('graph_nodes').select('label').eq('id', t_id).limit(1).execute()
                    t_label = t_node_res.data[0]['label'] if t_node_res and t_node_res.data and t_node_res.data[0].get('label') else None
                    if t_label:
                        node_res = maybe_single_safe(supabase.table('graph_nodes').select('metadata').eq('id', s_id))
                        if node_res and node_res.data:
                            node_meta = node_res.data.get('metadata') or {}
                            if isinstance(node_meta, str):
                                try:
                                    node_meta = json.loads(node_meta)
                                except Exception:
                                    node_meta = {}
                            enrich = node_meta.get('enrichment') or {}
                            enrich['organization_name'] = t_label
                            node_meta['enrichment'] = enrich
                            supabase.table('graph_nodes').update({'metadata': node_meta}).eq('id', s_id).execute()
                            audit_log_sync("pulse", "INFO", f"Backfill: Set enrichment.organization_name for '{s_label}' via WORKS_AT approval")
            except Exception as backfill_err:
                audit_log_sync("pulse", "WARNING", f"Edge approval backfill failed for {rel} '{s_label}': {backfill_err}")

            fire_briefing_refresh(source="graph_edge_decision")
            return {"success": True, "action": "approved", "message": f"Approved edge: {s_label} → {rel} → {t_label}"}
            
    except Exception as e:
        audit_log_sync("pulse", "ERROR", f"Error processing edge decision: {e}")
        return {"success": False, "action": "error", "message": str(e)}

async def write_graph_edges_for_task(task_id: int, task_title: str, task_description: str = None, people_cache=None, organization_id: str = None):
    """
    Add-on: Writes graph edges after a task is created.
    Non-blocking. If this fails, the task is already saved — no rollback needed.
    Now also creates task→org BELONGS_TO edge when organization_id is provided.
    """
    try:
        supabase.table('graph_nodes').upsert({
            "label": task_title,
            "type": "task",
            "normalized_label": normalize_label(task_title),
            "metadata": {
                "source": "tasks_table",
                "task_id": task_id,

            }
        }, on_conflict="owner_id, normalized_label, type").execute()

        # Task→Organization BELONGS_TO edge
        if organization_id:
            org_node = supabase.table('graph_nodes') \
                .select('id, label') \
                .eq('type', 'organization') \
                .filter('metadata->>organization_id', 'eq', str(organization_id)) \
                .maybe_single() \
                .execute()

            if not org_node or not org_node.data:
                # Fallback: match by db_record_id
                org_node = supabase.table('graph_nodes') \
                    .select('id, label') \
                    .eq('type', 'organization') \
                    .eq('db_record_id', str(organization_id)) \
                    .maybe_single() \
                    .execute()

            if org_node and org_node.data:
                from core.lib.graph_rules import insert_pending_edge
                insert_pending_edge(
                    task_title,
                    org_node.data.get('label', str(organization_id)),
                    "BELONGS_TO",
                    {
                        "source_text": f"tasks:{task_id}",
                        "source_table": "task_engine",
                        "source_type": "task",
                        "target_type": "organization"
                    }
                )

        search_text = f"{task_title} {task_description or ''}".lower()

        # Use cache if provided, otherwise fetch person nodes directly
        # (consolidation: the graph node is the source of truth)
        if people_cache is not None:
            all_people = people_cache
        else:
            all_people = supabase.table('graph_nodes') \
                .select('id, label') \
                .eq('type', 'person') \
                .eq('is_current', True) \
                .execute().data or []

        for person in (all_people or []):
            pname = person.get('name') or person.get('label') or ''
            if pname.lower() in search_text:
                person_node = supabase.table('graph_nodes') \
                    .select('id') \
                    .eq('type', 'person') \
                    .eq('is_current', True) \
                    .eq('id', person.get('id')) \
                    .maybe_single() \
                    .execute()

                if person_node and person_node.data:
                    from core.lib.graph_rules import insert_pending_edge
                    insert_pending_edge(
                        task_title,
                        pname,
                        "INVOLVES",
                        {
                            "source_text": f"tasks:{task_id}",
                            "source_table": "task_engine",
                            "source_type": "task",
                            "target_type": "person"
                        }
                    )

        print(f"🕸️ Graph edges written for task {task_id}: '{task_title}'")

    except Exception as e:
        audit_log_sync("pulse", "WARNING", f"⚠️ Graph edge write failed (non-critical): {e}")

async def hybrid_search_graph(query: str, node_id: str = None) -> str:
    """Graph-first search: Find primary entity and its connections."""
    try:
        nodes_res = None
        if node_id:
            nodes_res = supabase.table('graph_nodes').select('id, label').eq('id', node_id).limit(1).execute()
            
        if not nodes_res or not nodes_res.data:
            nodes_res = supabase.table('graph_nodes').select('id, label').ilike('label', f'%{query}%').eq('is_current', True).limit(1).execute()

        if not nodes_res.data:
            try:
                query_embedding = (await get_embedding(query)).vector
                vector_res = supabase.rpc('match_graph_nodes', {
                    'query_embedding': query_embedding,
                    'match_count': 1,
                    'match_threshold': 0.65
                }).execute()
                if vector_res.data:
                    nodes_res = vector_res
            except Exception as vector_err:
                audit_log_sync("graph", "WARNING", f"Vector fallback search failed (RPC may not exist): {vector_err}")

        if not nodes_res.data:
            return ""

        primary_node = nodes_res.data[0]
        primary_id = primary_node['id']

        edges_res = supabase.table('graph_edges').select('source_node_id, target_node_id, relationship').or_(f'source_node_id.eq.{primary_id},target_node_id.eq.{primary_id}').eq('is_current', True).execute()

        if not edges_res.data:
            return ""

        connected_ids = set()

        for edge in edges_res.data:
            if edge['source_node_id'] == primary_id:
                connected_ids.add(edge['target_node_id'])
            elif edge['target_node_id'] == primary_id:
                connected_ids.add(edge['source_node_id'])

        if connected_ids:
            labels_res = supabase.table('graph_nodes').select('id, label').in_('id', list(connected_ids)).execute()
            if not labels_res.data:
                return ""
            label_map = {str(n['id']): n['label'] for n in labels_res.data}

            labeled_map = []
            for edge in edges_res.data:
                src_label = label_map.get(str(edge['source_node_id']), "Unknown")
                tgt_label = label_map.get(str(edge['target_node_id']), "Unknown")

                if edge['source_node_id'] == primary_id:
                    labeled_map.append(f"[{primary_node['label']}] -> [{edge['relationship']}] -> [{tgt_label}]")
                elif edge['target_node_id'] == primary_id:
                    labeled_map.append(f"[{src_label}] -> [{edge['relationship']}] -> [{primary_node['label']}]")

            return "\n".join(labeled_map)

        return ""

    except Exception as e:
        audit_log_sync("pulse", "WARNING", f"⚠️ Graph task context fetch failed (non-critical): {e}")
        return ""

async def get_graph_centrality_context() -> str:
    """
    GRAPH CENTRALITY: Analyzes the knowledge graph to find the most connected hubs.
    Highlights people or topics bridging different domains.
    """
    try:
        # Get the top 5 most connected nodes
        res = supabase.rpc('get_most_connected_nodes', {'limit_count': 3}).execute()
        
        if not res.data:
            return ""
            
        lines = ["🕸️ GRAPH CENTRALITY (Top Hubs):"]
        for node in res.data:
            lines.append(f"  - {node.get('label')} ({node.get('type')}): {node.get('edge_count')} connections")
            
        return "\n".join(lines)
    except Exception as e:
        audit_log_sync("pulse", "WARNING", f"⚠️ Centrality detection failed: {e}")
        return ""

async def check_task_dependencies(active_tasks: list) -> str:
    """
    DEPENDENCY AGENT: Uses graph_edges to detect when a task (B) has an uncompleted
    dependency on another task (A). Flags blockers before the user starts work.
    """
    try:
        if not active_tasks:
            return ""

        lines = []
        blocked_tasks = []

        # Build task_id → task map
        task_map = {t['id']: t for t in active_tasks}

        for task in active_tasks:
            task_id = task.get('id')
            task_title = task.get('title', '')

            # Get the graph node for this task
            task_node_res = supabase.table('graph_nodes') \
                .select('id') \
                .eq('type', 'task') \
                .filter('metadata->>task_id', 'eq', str(task_id)) \
                .eq('is_current', True) \
                .maybe_single() \
                .execute()

            if not task_node_res or not task_node_res.data:
                continue

            task_node_id = task_node_res.data['id']

            # Find edges where this task DEPENDS_ON another task
            dep_edges = supabase.table('graph_edges') \
                .select('source_node_id, target_node_id, relationship, metadata') \
                .eq('source_node_id', task_node_id) \
                .eq('is_current', True) \
                .execute()

            for edge in (dep_edges.data or []):
                relationship = edge.get('relationship', '').upper()
                # Look for dependency relationships
                if relationship in ['DEPENDS_ON', 'BLOCKED_BY', 'REQUIRES']:
                    target_id = edge.get('target_node_id')

                    # Find the target node's task_id from metadata
                    target_node_res = supabase.table('graph_nodes') \
                        .select('id, label, metadata') \
                        .eq('id', target_id) \
                        .maybe_single() \
                        .execute()

                    if target_node_res and target_node_res.data:
                        meta = target_node_res.data.get('metadata') or {}
                        if isinstance(meta, str):
                            try:
                                meta = json.loads(meta)
                            except Exception:
                                meta = {}
                        dep_task_id = meta.get('task_id')

                        if dep_task_id:
                            try:
                                dep_task_id_int = int(dep_task_id)
                                if dep_task_id_int in task_map:
                                    dep_task = task_map[dep_task_id_int]
                                    dep_status = dep_task.get('status', '')

                                    if dep_status not in ['done', 'cancelled']:
                                        blocked_tasks.append({
                                            'task': task_title,
                                            'depends_on': dep_task.get('title', ''),
                                            'dep_status': dep_status
                                        })
                            except (ValueError, TypeError):
                                pass

        if blocked_tasks:
            lines.append("⚠️ DEPENDENCY ALERTS (from graph_edges):")
            for b in blocked_tasks[:5]:  # Cap at 5
                lines.append(f"  - {b['task']} BLOCKED by '{b['depends_on']}' (status: {b['dep_status']})")
            return "\n".join(lines)

        return ""

    except Exception as e:
        audit_log_sync("pulse", "WARNING", f"⚠️ Dependency Agent failed (non-critical): {e}")
        return ""

async def analyze_communication_patterns(people: list) -> str:
    """
    SOCIAL GRAPH OPTIMIZER: Analyzes people + graph_edges to suggest communication
    batching and identify over/under-communicated relationships.
    """
    try:
        if not people:
            return ""

        lines = []
        comm_suggestions = []

        for person in people:
            person_name = person.get('name', '')
            person_id = person.get('id')
            strategic_weight = person.get('strategic_weight', 5)

            if not person_name or not person_id:
                continue

            # Person node: people now come from graph_nodes (consolidation),
            # so person_id IS the node id itself.
            person_node_id = str(person_id)
            person_node_res = supabase.table('graph_nodes') \
                .select('id') \
                .eq('id', person_node_id) \
                .eq('type', 'person') \
                .eq('is_current', True) \
                .maybe_single() \
                .execute()

            if not person_node_res or not person_node_res.data:
                continue

            # Count INVOLVES edges (task involvements)
            involves_edges = supabase.table('graph_edges') \
                .select('source_node_id, target_node_id') \
                .eq('relationship', 'INVOLVES') \
                .or_(f'source_node_id.eq.{person_node_id},target_node_id.eq.{person_node_id}') \
                .eq('is_current', True) \
                .execute()

            task_count = len(involves_edges.data or [])

            # Get recent email count for this person
            email_count = 0
            try:
                linked = person.get('people_id') or person.get('db_record_id')
                email_res = supabase.table('messages') \
                    .select('id', count='exact') \
                    .eq('channel', 'email') \
                    .or_(f'sender_name.ilike.%{person_name}%' + (f',linked_person_id.eq.{linked}' if linked else '')) \
                    .execute()
                email_count = email_res.count or 0
            except Exception:
                pass

            # High-strategic person with low communication = suggestion
            if strategic_weight >= 7 and email_count < 3 and task_count < 3:
                comm_suggestions.append(f"  - {person_name}: Low communication (emails: {email_count}, tasks: {task_count}). Consider a sync.")
            elif strategic_weight >= 5 and email_count == 0 and task_count > 0:
                comm_suggestions.append(f"  - {person_name}: Has {task_count} tasks but no recent emails. May need update.")

        if comm_suggestions:
            lines.append("👥 SOCIAL GRAPH INSIGHTS:")
            lines.extend(comm_suggestions[:5])  # Cap at 5
            return "\n".join(lines)

        return ""

    except Exception as e:
        audit_log_sync("pulse", "WARNING", f"⚠️ Social Graph Optimizer failed (non-critical): {e}")
        return ""

async def fetch_hybrid_graph_context(people: list, graph_node_projects: list, task_inputs: list) -> str:
    """Hybrid graph search using entity terms from people+organizations, filtering by task_inputs."""
    try:
        entity_terms = [p['name'] for p in people if p.get('name')] + [p.get('name') for p in graph_node_projects if p.get('name')]

        if not entity_terms or not task_inputs:
            return ""

        dump_text = " ".join(task_inputs).lower()

        matched_terms = [term for term in entity_terms if term.lower() in dump_text]

        query_terms = matched_terms if matched_terms else entity_terms[:8]

        results = await asyncio.gather(*[hybrid_search_graph(term) for term in query_terms])

        all_lines = []
        for result in results:
            if result:
                all_lines.extend(result.split("\n"))

        if not all_lines:
            return ""

        deduplicated = list(dict.fromkeys(all_lines))
        return "GRAPH CONTEXT (routing awareness only — do NOT list in briefing):\n" + "\n".join(deduplicated)

    except Exception as e:
        audit_log_sync("pulse", "WARNING", f"⚠️ Hybrid graph context fetch failed (non-critical): {e}")
        return ""

async def fetch_graph_task_context(people: list, active_tasks: list) -> str:
    """
    Fetches graph edges connecting people to active tasks.
    Returns formatted context showing who is involved in which tasks.
    """
    try:
        if not people or not active_tasks:
            return ""

        task_map = {t['id']: t for t in active_tasks if t and isinstance(t, dict) and 'id' in t}

        # Get all person nodes — match by legacy bigint people_id (mirror) or
        # by the node id itself (consolidated graph-first shape).
        people_ids = {p['id']: p['name'] for p in people if p and isinstance(p, dict) and 'id' in p and 'name' in p}
        for p in people:
            if not p or not isinstance(p, dict):
                continue
            legacy = p.get('people_id')
            if legacy and p.get('name'):
                people_ids[str(legacy)] = p['name']
        person_nodes = supabase.table('graph_nodes') \
            .select('id, label, metadata') \
            .eq('type', 'person') \
            .eq('is_current', True) \
            .execute()

        # Build node_id → person_name map
        node_to_person = {}
        for node in (person_nodes.data or []):
            meta = node.get('metadata') or {}
            if isinstance(meta, str):
                try:
                    meta = json.loads(meta)
                except Exception:
                    continue
            people_id = meta.get('people_id')
            if people_id and str(people_id) in people_ids:
                # Migration 75: metadata.people_id is the node's own UUID —
                # match as a string, never int() (legacy bigint ids are gone).
                node_to_person[node['id']] = people_ids[str(people_id)]

        # Find INVOLVES edges linking person nodes to task nodes
        task_nodes = supabase.table('graph_nodes') \
            .select('id, metadata') \
            .eq('type', 'task') \
            .eq('is_current', True) \
            .execute()

        task_node_ids = []
        task_node_map = {}  # node_id → task_id
        for node in (task_nodes.data or []):
            meta = node.get('metadata') or {}
            if isinstance(meta, str):
                try:
                    meta = json.loads(meta)
                except Exception:
                    continue
            task_id = meta.get('task_id')
            if task_id:
                try:
                    task_id_int = int(task_id)
                    if task_id_int in task_map:
                        task_node_ids.append(node['id'])
                        task_node_map[node['id']] = task_id_int
                except (ValueError, TypeError):
                    pass

        if not task_node_ids or not node_to_person:
            return ""

        # Get INVOLVES edges
        edges_res = supabase.table('graph_edges') \
            .select('source_node_id, target_node_id, relationship') \
            .in_('relationship', ['INVOLVES', 'MANAGES', 'ASSIGNED_TO']) \
            .eq('is_current', True) \
            .execute()

        context_lines = []
        seen = set()

        for edge in (edges_res.data or []):
            if not edge:
                continue
            source = edge.get('source_node_id')
            target = edge.get('target_node_id')
            rel = edge.get('relationship')

            # Check if this connects a person to a task
            person_name = None
            task_id = None

            if source in node_to_person and target in task_node_map:
                person_name = node_to_person[source]
                task_id = task_node_map[target]
            elif target in node_to_person and source in task_node_map:
                person_name = node_to_person[target]
                task_id = task_node_map[source]

            if person_name and task_id and task_id in task_map:
                task_title = task_map[task_id]['title']
                key = f"{person_name}:{task_id}"
                if key not in seen:
                    seen.add(key)
                    context_lines.append(f"[{person_name}] --{rel}--> [{task_title}]")

        if context_lines:
            return "GRAPH TASK CONTEXT:\n" + "\n".join(context_lines[:10])  # Cap at 10
        return ""

    except Exception as e:
        audit_log_sync("pulse", "WARNING", f"⚠️ Graph task context fetch failed (non-critical): {e}")
        return ""

async def enrich_pending_edges_with_conflicts(rows: list) -> list:
    """Attach an existing conflicting relationship to each pending edge row.

    Replaces the retired clarifier's edge_contradiction question (plans/73):
    instead of asking "which is right?", the Quick Confirmation edge card shows
    "⚠️ conflicts with existing KNOWS edge" so the user decides in context.

    For each pending edge (source_label → relationship → target_label), find
    the live graph_edges between the same node pair and record a different,
    non-MENTIONS relationship as `conflict_with`. Fail-open: any query problem
    returns the rows unchanged (a hint problem must never break the feed).
    """
    if not rows:
        return rows
    try:
        labels = []
        seen_labels = set()
        for r in rows:
            for k in ("source_label", "target_label"):
                lbl = (r.get(k) or "").strip()
                if lbl and lbl.lower() not in seen_labels:
                    seen_labels.add(lbl.lower())
                    labels.append(lbl)
        if not labels:
            return rows

        by_label = {}
        for i in range(0, len(labels), 50):
            chunk = labels[i:i + 50]
            n_res = await exec_query(
                supabase.table("graph_nodes")
                .select("id, label")
                .in_("label", chunk)
                .eq("is_current", True)
                .limit(200)
            )
            for n in (n_res.data or []):
                by_label.setdefault((n.get("label") or "").strip().lower(), n.get("id"))

        node_ids = [i for i in by_label.values() if i]
        pair_rels = {}  # (src_id, tgt_id) -> {relationship}
        for i in range(0, len(node_ids), 50):
            chunk = node_ids[i:i + 50]
            e_res = await exec_query(
                supabase.table("graph_edges")
                .select("source_node_id, target_node_id, relationship")
                .in_("source_node_id", chunk)
                .eq("is_current", True)
                .limit(500)
            )
            for e in (e_res.data or []):
                rel = (e.get("relationship") or "").upper()
                if rel == "MENTIONS":
                    continue
                key = (e.get("source_node_id"), e.get("target_node_id"))
                pair_rels.setdefault(key, set()).add(rel)

        for r in rows:
            s_id = by_label.get((r.get("source_label") or "").strip().lower())
            t_id = by_label.get((r.get("target_label") or "").strip().lower())
            if not s_id or not t_id:
                continue
            rel = (r.get("relationship") or "").upper()
            conflicts = {x for x in pair_rels.get((s_id, t_id), set()) if x != rel}
            if conflicts:
                r["conflict_with"] = ", ".join(sorted(conflicts))
    except Exception as e:
        audit_log_sync("graph_pipeline", "WARNING", f"Edge conflict enrichment failed: {e}")
    return rows


def insert_extracted_entities(nodes: list, edges: list, source_id: str, source_type: str, source_content: str = ""):
    """
    Unified extraction insertion pipeline.
    source_type: 'task', 'memory', 'raw_dump'
    """
    # 1. Resolve source node (the memory/task itself)
    source_label = f"{source_type.capitalize()}_{source_id}"
    try:
        source_node_res = supabase.table('graph_nodes') \
            .select('id') \
            .eq('type', source_type) \
            .filter(f'metadata->>{source_type}_id', 'eq', str(source_id)) \
            .eq('is_current', True) \
            .maybe_single() \
            .execute()
            
        if source_node_res and source_node_res.data:
            root_node_id = source_node_res.data['id']
        else:
            meta = {f"{source_type}_id": source_id, "source": "insert_extracted_entities"}
            if source_content:
                preview = make_memory_preview(source_content)
                if preview:
                    meta["preview"] = preview

            new_node = supabase.table('graph_nodes').insert({
                "label": source_label,
                "type": source_type,
                "normalized_label": normalize_label(source_label),
                "metadata": meta
            }).execute()
            root_node_id = new_node.data[0]['id']
    except Exception:
        # If we can't create/find root node, we can still process edges
        root_node_id = None

    # Build unique nodes map from extracted nodes
    extracted_nodes = {}
    extracted_conflicts = {}
    
    # Fetch type overrides
    overrides_res = supabase.table('graph_type_overrides').select('*').execute()
    overrides_map = {r['label'].lower(): r['node_type'] for r in overrides_res.data} if overrides_res.data else {}

    for n in nodes:
        lbl = n.get("label", "")
        if isinstance(lbl, str):
            lbl = lbl.strip()
            typ = n.get("type") or ""
            if not typ:
                # Hardening: a detected node with no type is not persisted —
                # never guess 'concept' for an untyped label.
                if lbl:
                    audit_log_sync(
                        "graph_pipeline", "WARNING",
                        f"label_skipped_no_type: {lbl!r} produced without a type — not persisted"
                    )
                continue
            if typ == 'task':
                audit_log_sync(
                    "graph_pipeline", "INFO",
                    f"label_skipped_task_type: {lbl!r} is a task — tasks flow via suggestion/review, not pending_nodes"
                )
                continue
            if lbl:
                # Apply type override if exists
                if lbl.lower() in overrides_map:
                    typ = overrides_map[lbl.lower()]
                extracted_nodes[lbl] = typ
                if n.get("type_conflict"):
                    extracted_conflicts[lbl] = True

    # 2. Sanitize LLM edge endpoints. The relationship LLM can echo
    #    ' {label} ({type})' strings back as endpoints (e.g. 'Pup (animal)').
    #    Strip the echo artifact and canonicalize endpoints that match a
    #    detected entity (case-insensitive). Endpoints that do NOT match a
    #    detected entity are KEPT with their sanitized label so the unified
    #    pipeline below can still resolve them against the DB (known entities
    #    with name variants must not lose their edges) — but if neither the
    #    detector nor the DB provides a type, the label is skipped instead of
    #    being auto-vivified as 'concept' (the Aug 6 root cause).
    from core.lib.graph_rules import resolve_edge_label, sanitize_edge_label
    for e in edges:
        s_raw = e.get("source", "")
        t_raw = e.get("target", "")
        # Always strip echo artifacts ('Pup (animal)' -> 'Pup'); canonicalize
        # to the detected label when one matches (case-insensitive).
        s_clean = sanitize_edge_label(s_raw)
        t_clean = sanitize_edge_label(t_raw)
        e["source"] = resolve_edge_label(s_raw, extracted_nodes) or s_clean
        e["target"] = resolve_edge_label(t_raw, extracted_nodes) or t_clean

    all_labels = set(extracted_nodes.keys())
    for e in edges:
        s_lbl = e.get("source", "")
        t_lbl = e.get("target", "")
        if isinstance(s_lbl, str) and s_lbl.strip():
            all_labels.add(s_lbl.strip())
        if isinstance(t_lbl, str) and t_lbl.strip():
            all_labels.add(t_lbl.strip())

    from core.lib.graph_rules import validate_label, resolve_candidate, route_label, persist_label, insert_pending_edge

    # 3 & 4. Process all labels through the unified pipeline
    node_id_map = {}
    resolved_labels = {}
    
    # We can inject lightweight hints if needed, currently empty
    hints = {}
    
    for raw_lbl in all_labels:
        # 1. Validation
        val = validate_label(raw_lbl, hints)
        
        # 2. Resolution
        res = resolve_candidate(raw_lbl)
        if not res.get("node_type"):
            # Hardening: never fabricate a type. A label only gets a type here
            # if the detector actually produced it (extracted_nodes). Any label
            # without a detected type is a non-entity — skip it rather than
            # persist a guessed 'concept' node.
            detected_type = extracted_nodes.get(raw_lbl)
            if detected_type:
                res["node_type"] = detected_type
            else:
                audit_log_sync(
                    "graph_pipeline", "WARNING",
                    f"label_skipped_no_type: {raw_lbl!r} has no detected type — "
                    f"not persisted ({source_type}:{source_id})"
                )
                continue
            
        # 3. Route
        route = route_label(res, val)
        if extracted_conflicts.get(raw_lbl):
            route = "pending"
            val["reason"] = (val.get("reason", "") + " | type_conflict").strip(" |")
        
        res["route"] = route
        resolved_labels[raw_lbl] = res
        
        audit_log_sync(
            "graph_pipeline", 
            "INFO", 
            "Routing entity candidate",
            metadata={
                "event": "entity_routing",
                "source_path": f"{source_type}:{source_id}",
                "route": route,
                "verdict": val.get("verdict"),
                "reason": val.get("reason"),
                "label_hash": hash(raw_lbl) % 1000000,
                "label": raw_lbl
            }
        )
        
        # 4. Persist
        source_info = {"source_text": f"{source_type}:{source_id}", "flag_reason": val.get("reason", "")}
        node_id = persist_label(route, res, source_info)
        
        if node_id:
            c_lbl = res["label"]
            node_id_map[c_lbl] = node_id
            
            # If person and pending, add KNOWS edge
            if route == "pending" and res["node_type"] == "person":
                root_label = _root_person_label()
                if root_label:
                    insert_pending_edge(
                        root_label, 
                        c_lbl, 
                        "KNOWS", 
                        {
                            "source_text": f"{source_type}:{source_id}", 
                            "source_table": source_type,
                            "source_type": "person", 
                            "target_type": "person"
                        }
                    )

    # 5. Link extracted nodes to root_node (MENTIONS edges)
    mentions_to_insert = []
    if root_node_id:
        for raw_lbl in extracted_nodes.keys():
            r = resolved_labels[raw_lbl]
            c_lbl = r["label"]
            if r["confidence"] > 0 and c_lbl in node_id_map:
                mentions_to_insert.append({
                    "source_node_id": root_node_id,
                    "target_node_id": node_id_map[c_lbl],
                    "relationship": "MENTIONS",
                    "weight": 1.0,
                    "metadata": {"source": "insert_extracted_entities"}
                })
        if mentions_to_insert:
            # Dedup MENTIONS before insert to prevent whole-batch constraint failures
            seen_mentions = set()
            unique_mentions = []
            for m in mentions_to_insert:
                key = (m["source_node_id"], m["relationship"], m["target_node_id"])
                if key not in seen_mentions:
                    seen_mentions.add(key)
                    unique_mentions.append(m)

            try:
                for i in range(0, len(unique_mentions), 50):
                    batch = unique_mentions[i:i+50]
                    # Use upsert to handle cross-batch duplicates gracefully
                    supabase.table('graph_edges').upsert(batch, on_conflict="source_node_id,relationship,target_node_id", ignore_duplicates=True).execute()
                    # Also log in pending_graph_edges for audit trail
                    # MENTIONS are structural meta-edges (provenance), exempt from HITL
                    s_ids = list(set(m["source_node_id"] for m in batch))
                    t_ids = list(set(m["target_node_id"] for m in batch))
                    s_res = supabase.table('graph_nodes').select('id, label').in_('id', s_ids).execute()
                    t_res = supabase.table('graph_nodes').select('id, label').in_('id', t_ids).execute()
                    s_labels = {n['id']: n['label'] for n in (s_res.data or [])}
                    t_labels = {n['id']: n['label'] for n in (t_res.data or [])}
                    for m in batch:
                        try:
                            supabase.table('pending_graph_edges').insert({
                                "source_label": s_labels.get(m["source_node_id"], ""),
                                "target_label": t_labels.get(m["target_node_id"], ""),
                                "relationship": "MENTIONS",
                                "status": "approved",
                                "approval_source": "provenance",
                                "source_text": "insert_extracted_entities"
                            }).execute()
                        except Exception:
                            pass # 23505 is fine
            except Exception as e:
                if hasattr(e, "code") and e.code == "23505":
                    audit_log_sync("entity_extraction", "INFO", "MENTIONS edge already exists")
                else:
                    audit_log_sync("entity_extraction", "ERROR", f"Failed to insert MENTIONS edge: {e}")

    # 6. Create pending edges
    for e in edges:
        s_raw = e.get("source", "")
        t_raw = e.get("target", "")
        if not isinstance(s_raw, str) or not isinstance(t_raw, str):
            continue
            
        s_raw = s_raw.strip()
        t_raw = t_raw.strip()
        rel = e.get("relationship", "RELATES_TO")
        if not isinstance(rel, str):
            rel = "RELATES_TO"

        if not s_raw or not t_raw:
            continue

        s_res = resolved_labels.get(s_raw)
        t_res = resolved_labels.get(t_raw)

        if not s_res or not t_res:
            continue

        if s_res.get("route") == "discard" or t_res.get("route") == "discard":
            continue

        s_c = s_res.get("label", s_raw)
        t_c = t_res.get("label", t_raw)
        
        # Check permanent edge skip
        s_id = s_res.get("node_id") or node_id_map.get(s_c)
        t_id = t_res.get("node_id") or node_id_map.get(t_c)
        if is_valid_uuid(s_id) and is_valid_uuid(t_id):
            try:
                from core.lib.graph_rules import canonicalize_relationship
                st = s_res.get("node_type", "concept")
                tt = t_res.get("node_type", "concept")
                crel = canonicalize_relationship(rel, st, tt)
                
                permanent_edge_res = supabase.table("graph_edges")\
                    .select("id")\
                    .eq("source_node_id", str(s_id))\
                    .eq("target_node_id", str(t_id))\
                    .eq("relationship", crel)\
                    .eq('is_current', True)\
                    .limit(1).execute()
                if permanent_edge_res and permanent_edge_res.data:
                    # Silently skip creating a pending edge since we already know this permanently
                    continue
            except Exception:
                pass
        
        insert_pending_edge(
            s_c, 
            t_c, 
            rel, 
            {
                "source_text": f"{source_type}:{source_id}",
                "source_table": source_type,
                "source_type": s_res.get("node_type", "concept"),
                "target_type": t_res.get("node_type", "concept")
            }
        )
        


    # 7. Layer 2: Deterministic pattern backstop for NEW persons -> orgs
    if source_content:
        import re
        new_persons = [raw for raw, res in resolved_labels.items() if res.get("route") == "pending" and res.get("node_type") == "person"]
        orgs = [raw for raw, res in resolved_labels.items() if res.get("node_type") == "organization" and res.get("route") != "discard"]
        
        for p_raw in new_persons:
            for o_raw in orgs:
                p_c = resolved_labels[p_raw]["label"]
                o_c = resolved_labels[o_raw]["label"]
                
                # Linguistic pattern match (e.g. "Marcus from Ashraya", "Binu at Equisoft")
                p_esc = re.escape(p_raw)
                o_esc = re.escape(o_raw)
                pattern = rf'\b{p_esc}\b.{{0,30}}?\b(?:from|at|of|works?\s+(?:for|at))\b.{{0,30}}?\b{o_esc}\b'
                
                if re.search(pattern, source_content, re.IGNORECASE):
                    insert_pending_edge(
                        p_c, o_c, "WORKS_AT",
                        {
                            "source_text": f"pattern_backstop:{source_type}:{source_id}",
                            "source_table": source_type,
                            "source_type": "person",
                            "target_type": "organization"
                        }
                    )
                    audit_log_sync("entity_extraction", "INFO", f"Pattern backstop: Proposed {p_c} WORKS_AT {o_c}")

