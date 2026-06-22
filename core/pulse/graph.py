from core.llm.constants import SYNTHESIS_MODEL
from core.services.db import get_supabase
from core.llm import get_embedding
from core.llm.fallback import generate_content_with_fallback
import json
import asyncio
from core.lib.audit_logger import audit_log_sync
from core.lib.people_utils import normalize_person_name
from core.lib.graph_rules import find_similar_node, validate_edge, resolve_alias, resolve_canonical_label

supabase = get_supabase()

VALID_ORG_TAGS = {'SOLVSTRAT', 'QHORD', 'PERSONAL', 'CRAYON', 'ASHRAYA'}
TYPE_TO_DANNY_EDGE = {
    'project': 'OWNS',
    'person': 'KNOWS',
    'organization': 'MEMBER_OF',
    'concept': 'EVOKES',
    'place': 'RELATES_TO',
    'event': 'ATTENDED',
    'emotional_state': 'RELATES_TO',
}


async def create_graph_node_with_db_record(
    label: str,
    node_type: str,
    source_text: str = "",
    org_tag: str = None,
    context: str = None,
    source_tag: str = "pending_approval",
    force: bool = False
) -> dict:
    """Create a people/projects table row + graph_nodes entry + Danny edge.
    
    Three modes:
    - Person: creates people row → graph_nodes with people_id → Danny KNOWS edge
    - Project: creates projects row (requires org_tag) → graph_nodes with project_id → Danny OWNS edge
    - Other (org, concept, etc.): graph_nodes only, no DB table
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

        if node_type == 'project':
            if not org_tag:
                return {
                    "success": False, "action": "needs_org_tag",
                    "message": f"Project '{label}' needs an org tag ({', '.join(sorted(VALID_ORG_TAGS))})"
                }
            org_tag_upper = org_tag.upper().strip()
            if org_tag_upper not in VALID_ORG_TAGS:
                return {
                    "success": False, "action": "invalid_org_tag",
                    "message": f"Invalid org tag '{org_tag}'. Must be one of: {', '.join(sorted(VALID_ORG_TAGS))}"
                }

            existing = supabase.table('projects').select('id, name').ilike('name', label).maybe_single().execute()
            if existing and existing.data:
                project_id = existing.data['id']
                audit_log_sync("pulse", "INFO", f"Reusing existing project '{label}' (ID {project_id})")
            else:
                result = supabase.table('projects').insert({
                    "name": label,
                    "org_tag": org_tag_upper,
                    "status": "active",
                    "context": context or "from graph_approval",
                    "is_active": True,
                }).execute()
                if not result or not result.data:
                    raise Exception("Supabase insert returned no data for projects")
                project_id = result.data[0]['id']

            supabase.table("graph_nodes").upsert(
                {
                    "label": label,
                    "type": "project",
                    "epistemic_status": "asserted",
                    "db_record_id": str(project_id),
                    "metadata": {
                        "source": source_tag,
                        "project_id": str(project_id),
                        "org_tag": org_tag_upper,
                        "memory_id": source_text,
                    }
                },
                on_conflict="label"
            ).execute()

            await _ensure_danny_edge(label, node_type)

            inferred = []
            if source_text and source_text.strip() not in ("", "batch"):
                inferred = await _infer_additional_edges(label, node_type, source_text)

            return {
                "success": True, "action": "approved",
                "message": f"Approved project '{label}' ({org_tag_upper})",
                "inferred_edges": inferred
            }

        elif node_type == 'person':
            norm_name = normalize_person_name(label)
            existing_resp = supabase.table('people').select('id, name').execute()
            existing_people = existing_resp.data if existing_resp else []
            matched_id = None
            for p in existing_people:
                if normalize_person_name(p['name']) == norm_name or p['name'].lower() == label.lower():
                    matched_id = p['id']
                    break

            if matched_id:
                people_id = matched_id
                audit_log_sync("pulse", "INFO", f"Reusing existing person '{label}' (ID {people_id})")
            else:
                insert_data = {"name": label, "source": "graph_approval", "strategic_weight": 5}
                if context:
                    insert_data["role"] = context.strip()
                result = supabase.table('people').insert(insert_data).execute()
                if not result or not result.data:
                    raise Exception("Supabase insert returned no data for people")
                people_id = result.data[0]['id']

            supabase.table("graph_nodes").upsert(
                {
                    "label": label,
                    "type": "person",
                    "epistemic_status": "asserted",
                    "db_record_id": str(people_id),
                    "metadata": {
                        "source": source_tag,
                        "people_id": str(people_id),
                        "memory_id": source_text,
                    }
                },
                on_conflict="label"
            ).execute()

            await _ensure_danny_edge(label, node_type)

            inferred = []
            if source_text and source_text.strip() not in ("", "batch"):
                inferred = await _infer_additional_edges(label, node_type, source_text)

            msg = f"Approved person '{label}'"
            if context:
                msg += f" ({context.strip()})"
            return {"success": True, "action": "approved", "message": msg, "inferred_edges": inferred}

        else:
            supabase.table("graph_nodes").upsert(
                {
                    "label": label,
                    "type": node_type,
                    "epistemic_status": "asserted",
                    "metadata": {
                        "source": source_tag,
                        "memory_id": source_text,
                    }
                },
                on_conflict="label"
            ).execute()

            await _ensure_danny_edge(label, node_type)

            inferred = []
            if source_text and source_text.strip() not in ("", "batch"):
                inferred = await _infer_additional_edges(label, node_type, source_text)

            return {"success": True, "action": "approved", "message": f"Approved node '{label}' ({node_type})", "inferred_edges": inferred}

    except Exception as e:
        audit_log_sync("pulse", "ERROR", f"Error creating graph node with DB record: {e}")
        return {"success": False, "action": "error", "message": str(e)}


async def _ensure_danny_edge(label: str, node_type: str):
    """Create OWNS/KNOWS edge from Danny to the node if one doesn't exist."""
    rel = TYPE_TO_DANNY_EDGE.get(node_type)
    if not rel:
        return
    try:
        danny_res = supabase.table("graph_nodes").select("id").eq("type", "person").ilike("label", "Danny").maybe_single().execute()
        if not danny_res or not danny_res.data:
            return
        danny_id = danny_res.data["id"]

        target_res = supabase.table("graph_nodes").select("id").eq("label", label).maybe_single().execute()
        if not target_res or not target_res.data:
            return
        target_id = target_res.data["id"]

        existing = supabase.table("graph_edges").select("id")\
            .eq("source_node_id", danny_id)\
            .eq("target_node_id", target_id)\
            .eq("relationship", rel)\
            .maybe_single().execute()

        if not existing or not existing.data:
            supabase.table("graph_edges").insert({
                "source_node_id": danny_id,
                "target_node_id": target_id,
                "relationship": rel,
                "weight": 1.0,
                "epistemic_status": "asserted",
                "metadata": {"source": "graph_approval"}
            }).execute()
    except Exception as e:
        audit_log_sync("pulse", "WARNING", f"Failed to create Danny edge: {e}")


def _extract_mentioned_labels(source_text: str, known_labels: list[str]) -> list[str]:
    """Return only the known labels that appear (case-insensitive substring) in source_text."""
    source_lower = source_text.lower()
    return [lbl for lbl in known_labels if lbl.lower() in source_lower]


async def _infer_additional_edges(label: str, node_type: str, source_text: str) -> list[str]:
    """Call Gemini to extract additional relationships from the source text involving the new node or mentioned entities."""
    try:
        nodes_res = supabase.table("graph_nodes").select("label").execute()
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
- What IS a project: Specific professional work streams, client engagements, side projects with structure (e.g. Qhord, SOLVSTRAT, Ashraya, Integrated OS).

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
        
        # Clean response and parse
        content = response.strip()
        if content.startswith("```json"):
            content = content[7:-3]
        elif content.startswith("```"):
            content = content[3:-3]
            
        try:
            edges_to_create = json.loads(content)
        except json.JSONDecodeError:
            audit_log_sync("pulse", "WARNING", f"Failed to parse inferred edges JSON: {content}")
            return []
            
        inferred = []
        for e in edges_to_create:
            s_label = e.get('source_label')
            t_label = e.get('target_label')
            rel = e.get('relationship')
            if not s_label or not t_label or not rel:
                continue
                
            # PHASE 2 HOOK
            from core.clarifier import evaluate_edge
            evaluate_edge(e, batch_mode=True)
                
            if s_label == t_label:
                continue
                
            rel = rel.upper()
                
            # Check existing pending edge
            existing = supabase.table("pending_graph_edges").select("id")\
                .eq("source_label", s_label)\
                .eq("target_label", t_label)\
                .eq("relationship", rel)\
                .in_("status", ["pending"])\
                .maybe_single().execute()
                
            if not existing or not existing.data:
                s_node_res = supabase.table("graph_nodes").select("type").eq("label", s_label).maybe_single().execute()
                t_node_res = supabase.table("graph_nodes").select("type").eq("label", t_label).maybe_single().execute()
                s_type = s_node_res.data.get("type") if s_node_res.data else None
                t_type = t_node_res.data.get("type") if t_node_res.data else None
                if s_type and t_type:
                    vr = validate_edge(s_type, rel, t_type)
                    if vr["action"] == "auto_reject":
                        audit_log_sync("pulse", "INFO", f"Auto-rejected inferred edge {s_label} --[{rel}]--> {t_label}: {vr['reason']}")
                        continue
                    elif vr["action"] == "auto_correct":
                        rel = vr["reason"]
                supabase.table("pending_graph_edges").insert({
                    "source_label": s_label,
                    "target_label": t_label,
                    "relationship": rel,
                    "source_text": "graph_approval_inference",
                    "status": "pending"
                }).execute()
                
            inferred.append(f"{s_label} → {rel} → {t_label}")
                
        return inferred
    except Exception as err:
        audit_log_sync("pulse", "WARNING", f"Error inferring edges: {err}")
        return []


async def process_graph_pending_decision(pending_id: int, decision: str, org_tag: str = None, context: str = None, new_label: str = None) -> dict:
    try:
        pending_res = supabase.table('pending_graph_nodes').select('*').eq('id', pending_id).maybe_single().execute()
        if not pending_res or not pending_res.data:
            return {"success": False, "action": "not_found", "message": "Graph item not found."}

        pending_item = pending_res.data
        if pending_item.get('status') not in ('pending', 'awaiting_details', 'flagged') and decision != 'unreject':
            return {"success": False, "action": "already_processed", "message": "Already processed."}

        if decision == 'unreject':
            if pending_item.get('status') != 'rejected':
                return {"success": False, "action": "not_rejected", "message": "Item is not rejected."}
            supabase.table('pending_graph_nodes').update({'status': 'pending', 'merge_candidate_id': None}).eq('id', pending_id).execute()
            return {"success": True, "action": "unrejected", "message": f"Un-rejected node {pending_item['label']}"}

        if decision == 'reject':
            label = pending_item['label']
            supabase.table('pending_graph_nodes').update({'status': 'rejected'}).eq('id', pending_id).execute()
            # Cascade reject edges
            supabase.table('pending_graph_edges').update({'status': 'rejected'}).eq('source_label', label).execute()
            supabase.table('pending_graph_edges').update({'status': 'rejected'}).eq('target_label', label).execute()
            return {"success": True, "action": "rejected", "message": f"Rejected node and related edges for {label}"}

        if decision == 'approve':
            label = pending_item['label']
            node_type = pending_item['type']
            source_text = pending_item.get('source_text', '')

            # If label was edited, rewrite pending_graph_edges first
            if new_label and new_label.strip() and new_label.strip() != label:
                old_label = label
                label = new_label.strip()
                supabase.table('pending_graph_edges').update({'source_label': label}).eq('source_label', old_label).execute()
                supabase.table('pending_graph_edges').update({'target_label': label}).eq('target_label', old_label).execute()
                supabase.table('pending_graph_nodes').update({'label': label}).eq('id', pending_id).execute()

            # Auto-approve any pending Danny→KNOWS edge for this label before creating node
            # (so _ensure_danny_edge sees the edge already exists and skips)
            danny_edge_res = supabase.table("pending_graph_edges")\
                .select("id, source_label, target_label, source_text")\
                .eq("source_label", "Danny")\
                .eq("target_label", label)\
                .eq("relationship", "KNOWS")\
                .eq("status", "pending")\
                .maybe_single().execute()
            if danny_edge_res and danny_edge_res.data:
                pe = danny_edge_res.data
                s_node = supabase.table("graph_nodes").select("id").eq("label", pe["source_label"]).maybe_single().execute()
                t_node = supabase.table("graph_nodes").select("id").eq("label", pe["target_label"]).maybe_single().execute()
                if s_node and s_node.data and t_node and t_node.data:
                    supabase.table("graph_edges").upsert({
                        "source_node_id": s_node.data["id"],
                        "target_node_id": t_node.data["id"],
                        "relationship": "KNOWS",
                        "weight": 1.0,
                        "metadata": {"source": "pending_edge_approval", "pending_id": pe["id"]}
                    }, on_conflict="source_node_id,relationship,target_node_id", ignore_duplicates=True).execute()
                supabase.table("pending_graph_edges").update({"status": "approved"}).eq("id", pe["id"]).execute()

            result = await create_graph_node_with_db_record(
                label=label,
                node_type=node_type,
                source_text=source_text,
                org_tag=org_tag,
                context=context,
                source_tag="pending_approval"
            )

            if result.get('success'):
                if result.get('action') == 'merge_proposed':
                    supabase.table('pending_graph_nodes').update({
                        'status': 'merge_proposed',
                        'merge_candidate_id': result.get('merge_candidate_id')
                    }).eq('id', pending_id).execute()
                else:
                    supabase.table('pending_graph_nodes').update({'status': 'approved'}).eq('id', pending_id).execute()
                    # Cascade auto-approve related concepts and EVOKES edges
                    from core.pulse.auto_approve import auto_approve_concepts_and_evokes
                    import asyncio
                    asyncio.create_task(auto_approve_concepts_and_evokes(label))

            return result

    except Exception as e:
        audit_log_sync("pulse", "ERROR", f"Error processing graph decision: {e}")
        return {"success": False, "action": "error", "message": str(e)}

async def process_pending_edge_decision(pending_id: int, decision: str, new_source: str = None, new_target: str = None, new_rel: str = None, context: str | None = None) -> dict:
    try:
        pe_res = supabase.table('pending_graph_edges').select('*').eq('id', pending_id).maybe_single().execute()
        if not pe_res or not pe_res.data:
            return {"success": False, "action": "not_found", "message": "Pending edge not found."}
            
        pe = pe_res.data
        if pe.get('status') != 'pending':
            return {"success": False, "action": "already_processed", "message": "Already processed."}
            
        if decision == 'reject':
            supabase.table('pending_graph_edges').update({'status': 'rejected'}).eq('id', pending_id).execute()
            return {"success": True, "action": "rejected", "message": "Rejected edge."}
            
        if decision == 'approve':
            s_label = new_source or pe['source_label']
            t_label = new_target or pe['target_label']
            rel = (new_rel or pe['relationship']).upper()

            from core.lib.graph_rules import validate_edge
            s_node_res = supabase.table('graph_nodes').select('id, type').eq('label', s_label).maybe_single().execute()
            t_node_res = supabase.table('graph_nodes').select('id, type').eq('label', t_label).maybe_single().execute()

            s_data = getattr(s_node_res, 'data', None)
            t_data = getattr(t_node_res, 'data', None)

            if not s_data or not t_data:
                missing = s_label if not s_data else t_label
                supabase.table('pending_graph_edges').update({'status': 'rejected'}).eq('id', pending_id).execute()
                return {"success": False, "action": "missing_node", "message": f"Node '{missing}' doesn't exist."}

            s_id = s_data['id']
            t_id = t_data['id']
            s_type = s_data.get('type')
            t_type = t_data.get('type')

            if s_type and t_type:
                vr = validate_edge(s_type, rel, t_type)
                if vr["action"] == "auto_reject":
                    supabase.table('pending_graph_edges').update({'status': 'rejected'}).eq('id', pending_id).execute()
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
                'metadata': meta
            }, on_conflict="source_node_id,relationship,target_node_id", ignore_duplicates=True).execute()
            
            supabase.table('pending_graph_edges').update({
                'status': 'approved',
                'source_label': s_label,
                'target_label': t_label,
                'relationship': rel,
                'source_node_id': s_id,
                'target_node_id': t_id
            }).eq('id', pending_id).execute()
            
            return {"success": True, "action": "approved", "message": f"Approved edge: {s_label} → {rel} → {t_label}"}
            
    except Exception as e:
        audit_log_sync("pulse", "ERROR", f"Error processing edge decision: {e}")
        return {"success": False, "action": "error", "message": str(e)}

async def write_graph_edges_for_task(task_id: int, task_title: str, project_id: int = None, task_description: str = None, people_cache=None):
    """
    Add-on: Writes graph edges after a task is created.
    Non-blocking. If this fails, the task is already saved — no rollback needed.
    """
    try:
        supabase.table('graph_nodes').upsert({
            "label": task_title,
            "type": "task",
            "metadata": {
                "source": "tasks_table",
                "task_id": task_id,
                "project_id": project_id
            }
        }, on_conflict="label").execute()

        if project_id:
            proj_node = supabase.table('graph_nodes') \
                .select('id, label') \
                .eq('type', 'project') \
                .filter('metadata->>project_id', 'eq', str(project_id)) \
                .maybe_single() \
                .execute()

            if proj_node and proj_node.data:
                try:
                    supabase.table("pending_graph_edges").insert({
                        "source_label": task_title,
                        "target_label": proj_node.data.get('label', str(project_id)),
                        "relationship": "WORKS_ON",
                        "source_text": f"tasks:{task_id}",
                        "source_table": "task_engine",
                        "status": "pending"
                    }).execute()
                except Exception as e:
                    audit_log_sync("pulse", "WARNING", f"Failed to insert WORKS_ON pending edge: {e}")

        search_text = f"{task_title} {task_description or ''}".lower()

        # Use cache if provided, otherwise fetch
        if people_cache is not None:
            all_people = people_cache
        else:
            all_people = supabase.table('people').select('id, name').execute().data or []

        for person in (all_people or []):
            if person['name'].lower() in search_text:
                person_node = supabase.table('graph_nodes') \
                    .select('id') \
                    .eq('type', 'person') \
                    .filter('metadata->>people_id', 'eq', str(person['id'])) \
                    .maybe_single() \
                    .execute()

                if person_node and person_node.data:
                    try:
                        supabase.table("pending_graph_edges").insert({
                            "source_label": task_title,
                            "target_label": person['name'],
                            "relationship": "INVOLVES",
                            "source_text": f"tasks:{task_id}",
                            "source_table": "task_engine",
                            "status": "pending"
                        }).execute()
                    except Exception as e:
                        audit_log_sync("pulse", "WARNING", f"Failed to insert INVOLVES pending edge: {e}")

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
            nodes_res = supabase.table('graph_nodes').select('id, label').ilike('label', f'%{query}%').limit(1).execute()

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
                print(f"Vector fallback search failed (RPC may not exist): {vector_err}")

        if not nodes_res.data:
            return ""

        primary_node = nodes_res.data[0]
        primary_id = primary_node['id']

        edges_res = supabase.table('graph_edges').select('source_node_id, target_node_id, relationship').or_(f'source_node_id.eq.{primary_id},target_node_id.eq.{primary_id}').execute()

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
    dependency on another task (A). Flags blockers before Danny starts work.
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
                .maybe_single() \
                .execute()

            if not task_node_res or not task_node_res.data:
                continue

            task_node_id = task_node_res.data['id']

            # Find edges where this task DEPENDS_ON another task
            dep_edges = supabase.table('graph_edges') \
                .select('source_node_id, target_node_id, relationship, metadata') \
                .eq('source_node_id', task_node_id) \
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

            # Get person node
            person_node_res = supabase.table('graph_nodes') \
                .select('id') \
                .eq('type', 'person') \
                .filter('metadata->>people_id', 'eq', str(person_id)) \
                .maybe_single() \
                .execute()

            if not person_node_res or not person_node_res.data:
                continue

            person_node_id = person_node_res.data['id']

            # Count INVOLVES edges (task involvements)
            involves_edges = supabase.table('graph_edges') \
                .select('source_node_id, target_node_id') \
                .eq('relationship', 'INVOLVES') \
                .or_(f'source_node_id.eq.{person_node_id},target_node_id.eq.{person_node_id}') \
                .execute()

            task_count = len(involves_edges.data or [])

            # Get recent email count for this person
            email_count = 0
            try:
                email_res = supabase.table('messages') \
                    .select('id', count='exact') \
                    .eq('channel', 'email') \
                    .or_(f'sender_name.ilike.%{person_name}%,linked_person_id.eq.{person_id}') \
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
    """Hybrid graph search using entity terms from people+projects, filtering by task_inputs."""
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

        # Get all person nodes
        people_ids = {p['id']: p['name'] for p in people if p and isinstance(p, dict) and 'id' in p and 'name' in p}
        person_nodes = supabase.table('graph_nodes') \
            .select('id, label, metadata') \
            .eq('type', 'person') \
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
            if people_id:
                try:
                    people_id_int = int(people_id)
                    if people_id_int in people_ids:
                        node_to_person[node['id']] = people_ids[people_id_int]
                except (ValueError, TypeError):
                    pass

        # Find INVOLVES edges linking person nodes to task nodes
        task_nodes = supabase.table('graph_nodes') \
            .select('id, metadata') \
            .eq('type', 'task') \
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

BYPASS_APPROVAL_TYPES = {'concept'}

def insert_extracted_entities(nodes: list, edges: list, source_id: str, source_type: str):
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
            .maybe_single() \
            .execute()
            
        if source_node_res and source_node_res.data:
            root_node_id = source_node_res.data['id']
        else:
            new_node = supabase.table('graph_nodes').insert({
                "label": source_label,
                "type": source_type,
                "metadata": {f"{source_type}_id": source_id, "source": "insert_extracted_entities"}
            }).execute()
            root_node_id = new_node.data[0]['id']
    except Exception:
        # If we can't create/find root node, we can still process edges
        root_node_id = None

    # Build unique nodes map from extracted nodes
    extracted_nodes = {}
    
    # Fetch type overrides
    overrides_res = supabase.table('graph_type_overrides').select('*').execute()
    overrides_map = {r['label'].lower(): r['node_type'] for r in overrides_res.data} if overrides_res.data else {}

    for n in nodes:
        lbl = n.get("label", "")
        if isinstance(lbl, str):
            lbl = lbl.strip()
            typ = n.get("type", "concept")
            if lbl:
                # Apply type override if exists
                if lbl.lower() in overrides_map:
                    typ = overrides_map[lbl.lower()]
                extracted_nodes[lbl] = typ

    # 2. Process all edges to find edge-only entities
    pending_edges_batch = []
    
    all_labels = set(extracted_nodes.keys())
    for e in edges:
        s_lbl = e.get("source", "")
        t_lbl = e.get("target", "")
        if isinstance(s_lbl, str) and s_lbl.strip():
            all_labels.add(s_lbl.strip())
        if isinstance(t_lbl, str) and t_lbl.strip():
            all_labels.add(t_lbl.strip())

    # 3. Resolve all labels
    resolved_labels = {}
    for lbl in all_labels:
        resolved = resolve_canonical_label(lbl)
        if resolved["confidence"] == 0.0 and len(resolved["label"]) >= 3:
            # Looks real but has no match anywhere. Determine type.
            typ = extracted_nodes.get(lbl, "concept")
            resolved["node_type"] = typ
            resolved["needs_creation"] = True
        else:
            resolved["needs_creation"] = False
        resolved_labels[lbl] = resolved

    # 4. Create missing nodes
    node_id_map = {}
    for raw_lbl, r in resolved_labels.items():
        c_lbl = r["label"]
        if r["confidence"] == 0.0 and r.get("needs_creation"):
            typ = r["node_type"]
            if typ in BYPASS_APPROVAL_TYPES:
                # Direct to graph_nodes
                try:
                    ins_res = supabase.table("graph_nodes").insert({
                        "label": c_lbl,
                        "type": typ,
                        "metadata": {"source": "insert_extracted_entities"}
                    }).execute()
                    if ins_res.data:
                        node_id_map[c_lbl] = ins_res.data[0]["id"]
                except Exception:
                    pass
            else:
                # To pending
                status = "pending" if typ in ['person', 'project', 'organization'] else "flagged"
                try:
                    pend_check = supabase.table('pending_graph_nodes').select('id').eq('label', c_lbl).maybe_single().execute()
                    if not pend_check.data:
                        ins_res = supabase.table('pending_graph_nodes').insert({
                            "label": c_lbl,
                            "type": typ,
                            "source_text": f"{source_type}:{source_id}",
                            "status": status
                        }).execute()
                        
                        if ins_res and ins_res.data:
                            new_node_id = ins_res.data[0]['id']
                            from core.clarifier import evaluate_node, store_and_send_clarification
                            clar = evaluate_node({"label": c_lbl, "type": typ}, batch_mode=True)
                            if clar:
                                import asyncio
                                loop = asyncio.get_event_loop()
                                if loop.is_running():
                                    loop.create_task(store_and_send_clarification(clar, "pending_graph_nodes", new_node_id))
                                else:
                                    asyncio.run(store_and_send_clarification(clar, "pending_graph_nodes", new_node_id))
                        # If person, also add a pending KNOWS edge from Danny
                        if typ == 'person':
                            danny_edge_exists = supabase.table("pending_graph_edges")\
                                .select("id, source_text")\
                                .eq("source_label", "Danny")\
                                .eq("target_label", c_lbl)\
                                .eq("relationship", "KNOWS")\
                                .maybe_single().execute()
                            if not danny_edge_exists or not danny_edge_exists.data:
                                supabase.table("pending_graph_edges").insert({
                                    "source_label": "Danny",
                                    "target_label": c_lbl,
                                    "relationship": "KNOWS",
                                    "source_text": f"{source_type}:{source_id}",
                                    "source_table": source_type,
                                    "status": "pending"
                                }).execute()
                            else:
                                # Merge provenance
                                existing = danny_edge_exists.data
                                current_sources = [s.strip() for s in (existing.get('source_text') or '').split(',') if s.strip()]
                                new_source = f"{source_type}:{source_id}"
                                if new_source not in current_sources:
                                    current_sources.append(new_source)
                                    supabase.table("pending_graph_edges").update({"source_text": ", ".join(current_sources)}).eq("id", existing['id']).execute()
                except Exception:
                    pass
        elif r["node_id"]:
            # Known node, store ID for MENTIONS link
            # Only if it's a UUID (live graph node)
            try:
                import uuid
                uuid.UUID(str(r["node_id"]))
                node_id_map[c_lbl] = r["node_id"]
            except ValueError:
                pass

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
            try:
                for i in range(0, len(mentions_to_insert), 50):
                    batch = mentions_to_insert[i:i+50]
                    supabase.table('graph_edges').insert(batch).execute()
                    # Also log in pending_graph_edges for audit trail
                    # MENTIONS are structural meta-edges (provenance), exempt from HITL
                    s_ids = list(set(m["source_node_id"] for m in batch))
                    t_ids = list(set(m["target_node_id"] for m in batch))
                    s_res = supabase.table('graph_nodes').select('id, label').in_('id', s_ids).execute()
                    t_res = supabase.table('graph_nodes').select('id, label').in_('id', t_ids).execute()
                    s_labels = {n['id']: n['label'] for n in (s_res.data or [])}
                    t_labels = {n['id']: n['label'] for n in (t_res.data or [])}
                    for m in batch:
                        supabase.table('pending_graph_edges').upsert({
                            "source_label": s_labels.get(m["source_node_id"], ""),
                            "target_label": t_labels.get(m["target_node_id"], ""),
                            "relationship": "MENTIONS",
                            "status": "approved",
                            "source_text": "insert_extracted_entities"
                        }, on_conflict="source_label,relationship,target_label", ignore_duplicates=True).execute()
            except Exception:
                pass

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
        rel = rel.upper()

        if not s_raw or not t_raw:
            continue

        s_res = resolved_labels.get(s_raw)
        t_res = resolved_labels.get(t_raw)

        if not s_res or not t_res:
            continue

        # Skip noise
        if s_res["confidence"] == 0.0 and not s_res.get("needs_creation"):
            continue
        if t_res["confidence"] == 0.0 and not t_res.get("needs_creation"):
            continue

        s_c = s_res["label"]
        t_c = t_res["label"]

        pending_edges_batch.append({
            "source_label": s_c,
            "target_label": t_c,
            "relationship": rel,
            "source_text": f"{source_type}:{source_id}",
            "source_table": source_type,
            "status": "pending"
        })

    if pending_edges_batch:
        try:
            source_labels = list(set([e['source_label'] for e in pending_edges_batch]))
            
            existing_map = {}
            for i in range(0, len(source_labels), 20):
                batch_labels = source_labels[i:i+20]
                res = supabase.table("pending_graph_edges").select("id, source_label, target_label, relationship, source_text").in_("source_label", batch_labels).execute()
                for r in (res.data or []):
                    key = f"{r['source_label']}|{r['target_label']}|{r['relationship']}"
                    existing_map[key] = r
            
            to_insert = []
            for edge in pending_edges_batch:
                key = f"{edge['source_label']}|{edge['target_label']}|{edge['relationship']}"
                if key in existing_map:
                    # Append source_text to preserve provenance across duplicate extractions
                    existing = existing_map[key]
                    # if existing is just from the batch itself (not from db yet), it won't have an 'id'
                    if 'id' in existing:
                        current_sources = [s.strip() for s in (existing.get('source_text') or '').split(',') if s.strip()]
                        new_source = edge['source_text']
                        if new_source not in current_sources:
                            current_sources.append(new_source)
                            updated_source_text = ", ".join(current_sources)
                            supabase.table("pending_graph_edges").update({"source_text": updated_source_text}).eq("id", existing['id']).execute()
                            existing['source_text'] = updated_source_text
                else:
                    to_insert.append(edge)
                    existing_map[key] = edge # Treat it as existing for the rest of the batch
                    
            if to_insert:
                for i in range(0, len(to_insert), 100):
                    batch = to_insert[i:i+100]
                    ins_res = supabase.table("pending_graph_edges").insert(batch).execute()
                    if ins_res and ins_res.data:
                        from core.clarifier import evaluate_edge, store_and_send_clarification
                        for new_edge in ins_res.data:
                            clar = evaluate_edge(new_edge, batch_mode=True)
                            if clar:
                                import asyncio
                                loop = asyncio.get_event_loop()
                                if loop.is_running():
                                    loop.create_task(store_and_send_clarification(clar, "pending_graph_edges", new_edge['id']))
                                else:
                                    asyncio.run(store_and_send_clarification(clar, "pending_graph_edges", new_edge['id']))
        except Exception as e:
            audit_log_sync("pulse", "ERROR", f"Pending edge insert failed: {e}")

