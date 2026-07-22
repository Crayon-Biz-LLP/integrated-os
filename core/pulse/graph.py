from core.llm.constants import SYNTHESIS_MODEL
from core.services.db import get_supabase, maybe_single_safe
from core.llm import get_embedding
from core.llm.fallback import generate_content_with_fallback
import json
import asyncio
import uuid
from core.lib.audit_logger import audit_log_sync
from core.lib.telemetry import emit_observation
from core.lib.people_utils import normalize_person_name
from core.lib.graph_rules import find_similar_node, resolve_alias, canonicalize_relationship, normalize_label_display, get_canonical_id, normalize_label
from core.clarifier import evaluate_node, evaluate_edge, store_and_send_clarification
from core.decisions import record_decision
from core.lib.node_tables import resolve_merge_proposal

supabase = get_supabase()


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
    'project': 'OWNS',
    'person': 'KNOWS',
    'organization': 'MEMBER_OF',
    'place': 'RELATES_TO',
    'event': 'ATTENDED',
    'emotional_state': 'FEELS',
}


async def create_graph_node_with_db_record(
    label: str,
    node_type: str,
    source_text: str = "",
    context: str = None,
    source_tag: str = "pending_approval",
    force: bool = False
) -> dict:
    """Create a people/projects table row + graph_nodes entry + Danny edge.
    
    Three modes:
    - Person: creates people row → graph_nodes with people_id → Danny KNOWS edge
    - Project: creates projects row → graph_nodes with project_id → Danny OWNS edge
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
            existing = maybe_single_safe(supabase.table('projects').select('id, name').ilike('name', label).eq('is_current', True))
            if existing and existing.data:
                project_id = existing.data['id']
                audit_log_sync("pulse", "INFO", f"Reusing existing project '{label}' (ID {project_id})")
            else:
                result = supabase.table('projects').insert({
                    "name": label,
                    "status": "active",
                    "context": context or "from graph_approval",
                    "is_active": True,
                }).execute()
                if not result or not result.data:
                    raise Exception("Supabase insert returned no data for projects")
                project_id = result.data[0]['id']

            node_data = {
                "label": label,
                "type": "project",
                "epistemic_status": "asserted",
                "normalized_label": normalize_label(label),
                "db_record_id": str(project_id),
                "metadata": {
                    "source": source_tag,
                    "project_id": str(project_id),
                    "memory_id": source_text,
                }
            }
            supabase.table("graph_nodes").upsert(
                node_data,
                on_conflict="normalized_label, type"
            ).execute()

            # Post-creation hook: Conservative org link
            if source_text and source_text.strip() not in ("", "batch"):
                # Find all known organizations
                orgs_res = supabase.table('organizations').select('name').execute()
                known_orgs = [o['name'].strip() for o in (orgs_res.data or []) if o.get('name')]
                
                source_lower = source_text.lower()
                matched_org = None
                match_reason = None
                
                # Check exact/alias/substring matches
                # We need to respect stopword-like tokens and minimum length
                from core.lib.graph_rules import NOISE_LABELS
                
                for org in known_orgs:
                    if org.lower() in NOISE_LABELS:
                        continue
                    
                    # 1. Exact match
                    if f" {org.lower()} " in f" {source_lower} ":
                        matched_org = org
                        match_reason = "exact_match"
                        break
                        
                    # 2. Alias match
                    canonical = resolve_alias(org)
                    if canonical != org and f" {canonical.lower()} " in f" {source_lower} ":
                        matched_org = org
                        match_reason = "alias_match"
                        break
                        
                    # 3. Substring match (conservative)
                    if len(org) >= 6 and org.lower() in source_lower:
                        matched_org = org
                        match_reason = "substring_match"
                        break
                        
                if matched_org:
                    from core.lib.graph_rules import insert_pending_edge
                    res = insert_pending_edge(
                        label,
                        matched_org,
                        "BELONGS_TO",
                        {
                            "source_text": f"post_creation_hook:{source_text[:50]}",
                            "source_table": "graph_nodes",
                            "source_type": "project",
                            "target_type": "organization"
                        }
                    )
                    audit_log_sync("pulse", "INFO", f"Post-creation hook: Proposed {label} BELONGS_TO {matched_org} (reason: {match_reason}, status: {res.get('status')})")
                else:
                    audit_log_sync("pulse", "INFO", f"Post-creation hook: No confident org match found for project {label} in source text.")

            await _ensure_danny_edge(label, node_type)

            inferred = []
            if source_text and source_text.strip() not in ("", "batch"):
                inferred = await _infer_additional_edges(label, node_type, source_text)

            return {
                "success": True, "action": "approved",
                "message": f"Approved project '{label}'",
                "inferred_edges": inferred,
                "project_id": project_id
            }

        elif node_type == 'person':
            norm_name = normalize_person_name(label)
            existing_resp = supabase.table('people').select('id, name').eq('is_current', True).execute()
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

            upsert_res = supabase.table("graph_nodes").upsert(
                {
                    "label": label,
                    "type": "person",
                    "epistemic_status": "asserted",
                    "normalized_label": normalize_label(label),
                    "db_record_id": str(people_id),
                    "metadata": {
                        "source": source_tag,
                        "people_id": str(people_id),
                        "memory_id": source_text,
                    }
                },
                on_conflict="normalized_label, type"
            ).execute()

            # Back-link: store graph_node_id on the people row
            if upsert_res and upsert_res.data:
                graph_node_id = upsert_res.data[0].get('id')
                if graph_node_id:
                    supabase.table('people').update({'graph_node_id': graph_node_id}).eq('id', people_id).execute()

            # Post-creation hook: Conservative org link for person
            if source_text and source_text.strip() not in ("", "batch"):
                orgs_res = supabase.table('organizations').select('name').execute()
                known_orgs = [o['name'].strip() for o in (orgs_res.data or []) if o.get('name')]
                
                source_lower = source_text.lower()
                matched_org = None
                match_reason = None
                
                from core.lib.graph_rules import NOISE_LABELS
                
                for org in known_orgs:
                    if org.lower() in NOISE_LABELS:
                        continue
                    if f" {org.lower()} " in f" {source_lower} ":
                        matched_org = org
                        match_reason = "exact_match"
                        break
                    canonical = resolve_alias(org)
                    if canonical != org and f" {canonical.lower()} " in f" {source_lower} ":
                        matched_org = org
                        match_reason = "alias_match"
                        break
                    if len(org) >= 6 and org.lower() in source_lower:
                        matched_org = org
                        match_reason = "substring_match"
                        break
                        
                if matched_org:
                    from core.lib.graph_rules import insert_pending_edge
                    res = insert_pending_edge(
                        label,
                        matched_org,
                        "WORKS_AT",
                        {
                            "source_text": f"post_creation_hook:{source_text[:50]}",
                            "source_table": "graph_nodes",
                            "source_type": "person",
                            "target_type": "organization"
                        }
                    )
                    audit_log_sync("pulse", "INFO", f"Post-creation hook: Proposed {label} WORKS_AT {matched_org} (reason: {match_reason}, status: {res.get('status')})")

            await _ensure_danny_edge(label, node_type)

            inferred = []
            if source_text and source_text.strip() not in ("", "batch"):
                inferred = await _infer_additional_edges(label, node_type, source_text)

            msg = f"Approved person '{label}'"
            if context:
                msg += f" ({context.strip()})"
            return {"success": True, "action": "approved", "message": msg, "inferred_edges": inferred}

        else:
            # For organizations: create/upsert an organizations table row first,
            # then link graph_node_id back to it.
            org_db_id = None
            if node_type == 'organization':
                existing_org = maybe_single_safe(supabase.table('organizations').select('id').ilike('name', label))
                if existing_org and existing_org.data:
                    org_db_id = existing_org.data['id']
                    audit_log_sync("pulse", "INFO", f"Reusing existing organization '{label}' (ID {org_db_id})")
                else:
                    org_insert = supabase.table('organizations').insert({
                        "name": label,
                        "is_active": True,
                    }).execute()
                    if not org_insert or not org_insert.data:
                        raise Exception("Supabase insert returned no data for organizations")
                    org_db_id = org_insert.data[0]['id']

            node_meta = {
                "source": source_tag,
                "memory_id": source_text,
            }
            if org_db_id:
                node_meta["organization_id"] = str(org_db_id)

            upsert_res = supabase.table("graph_nodes").upsert(
                {
                    "label": label,
                    "type": node_type,
                    "epistemic_status": "asserted",
                    "normalized_label": normalize_label(label),
                    "db_record_id": str(org_db_id) if org_db_id else None,
                    "metadata": node_meta,
                },
                on_conflict="normalized_label, type"
            ).execute()

            # Back-link: store graph_node_id on the organizations row
            if org_db_id and upsert_res and upsert_res.data:
                graph_node_id = upsert_res.data[0].get('id')
                if graph_node_id:
                    supabase.table('organizations').update({'graph_node_id': graph_node_id}).eq('id', org_db_id).execute()

            await _ensure_danny_edge(label, node_type)

            inferred = []
            if source_text and source_text.strip() not in ("", "batch"):
                inferred = await _infer_additional_edges(label, node_type, source_text)

            msg = f"Approved node '{label}' ({node_type})"
            if node_type == 'organization' and org_db_id:
                msg = f"Approved organization '{label}' — organizations row created/linked (ID {org_db_id})"
            return {"success": True, "action": "approved", "message": msg, "inferred_edges": inferred}

    except Exception as e:
        audit_log_sync("pulse", "ERROR", f"Error creating graph node with DB record: {e}")
        return {"success": False, "action": "error", "message": str(e)}


async def _ensure_danny_edge(label: str, node_type: str):
    """Create OWNS/KNOWS edge from Danny to the node if one doesn't exist."""
    rel = TYPE_TO_DANNY_EDGE.get(node_type)
    if not rel:
        return
    try:
        danny_res = maybe_single_safe(supabase.table("graph_nodes").select("id").eq("type", "person").ilike("label", "Danny").eq('is_current', True))
        if not danny_res or not danny_res.data:
            return
        danny_id = danny_res.data["id"]

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
    except Exception as e:
        audit_log_sync("pulse", "WARNING", f"Failed to create Danny edge: {e}")


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
            audit_log_sync("pulse", "WARNING", f"Gap D: LLM inference returned unparseable JSON for {label} ({node_type}): {content[:200]}")
            return []
            
        inferred = []
        for e in edges_to_create:
            s_label = normalize_label_display(e.get('source_label'))
            t_label = normalize_label_display(e.get('target_label'))
            rel = e.get('relationship')
            if not s_label or not t_label or not rel:
                continue
                
            # PHASE 2 HOOK
            from core.clarifier import evaluate_edge
            evaluate_edge(e, batch_mode=True)
                
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

        if status not in ('pending', 'awaiting_details', 'awaiting_clarification', 'flagged', 'merge_proposed') and decision != 'unreject':
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

            # Auto-approve any pending Danny→KNOWS edge for this label
            danny_edge_res = maybe_single_safe(
                supabase.table("pending_graph_edges")
                .select("id")
                .eq("source_label", "Danny")
                .eq("target_label", label)
                .eq("relationship", "KNOWS")
                .eq("status", "pending")
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

            return {"success": True, "action": "approved", "message": f"Approved edge: {s_label} → {rel} → {t_label}"}
            
    except Exception as e:
        audit_log_sync("pulse", "ERROR", f"Error processing edge decision: {e}")
        return {"success": False, "action": "error", "message": str(e)}

async def write_graph_edges_for_task(task_id: int, task_title: str, project_id: int = None, task_description: str = None, people_cache=None, organization_id: str = None):
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
                "project_id": project_id
            }
        }, on_conflict="normalized_label, type").execute()

        if project_id:
            proj_node = supabase.table('graph_nodes') \
                .select('id, label') \
                .eq('type', 'project') \
                .filter('metadata->>project_id', 'eq', str(project_id)) \
                .maybe_single() \
                .execute()

            if proj_node and proj_node.data:
                from core.lib.graph_rules import insert_pending_edge
                insert_pending_edge(
                    task_title,
                    proj_node.data.get('label', str(project_id)),
                    "WORKS_ON",
                    {
                        "source_text": f"tasks:{task_id}",
                        "source_table": "task_engine",
                        "source_type": "task",
                        "target_type": "project"
                    }
                )

        # NEW: Task→Organization BELONGS_TO edge
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

        # Use cache if provided, otherwise fetch
        if people_cache is not None:
            all_people = people_cache
        else:
            all_people = supabase.table('people').select('id, name').eq('is_current', True).execute().data or []

        for person in (all_people or []):
            if person['name'].lower() in search_text:
                person_node = supabase.table('graph_nodes') \
                    .select('id') \
                    .eq('type', 'person') \
                    .filter('metadata->>people_id', 'eq', str(person['id'])) \
                    .maybe_single() \
                    .execute()

                if person_node and person_node.data:
                    from core.lib.graph_rules import insert_pending_edge
                    insert_pending_edge(
                        task_title,
                        person['name'],
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

            # Get person node
            person_node_res = supabase.table('graph_nodes') \
                .select('id') \
                .eq('type', 'person') \
                .filter('metadata->>people_id', 'eq', str(person_id)) \
                .eq('is_current', True) \
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
                .eq('is_current', True) \
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
                from core.lib.graph_rules import make_memory_preview
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
            res["node_type"] = extracted_nodes.get(raw_lbl, "concept")
            
        # 3. Route
        route = route_label(res, val)
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
        
        # 4b. Clarifier: evaluate new nodes for disambiguation
        if route == "pending" and node_id:
            try:
                clar = evaluate_node(res, batch_mode=True)
                if clar:
                    # Fire-and-forget: send clarification via Telegram
                    asyncio.ensure_future(store_and_send_clarification(clar, "pending_nodes", str(node_id)))
            except Exception as clar_err:
                audit_log_sync("graph_pipeline", "WARNING", f"Clarifier evaluate_node failed: {clar_err}")
        
        if node_id:
            c_lbl = res["label"]
            node_id_map[c_lbl] = node_id
            
            # If person and pending, add KNOWS edge
            if route == "pending" and res["node_type"] == "person":
                insert_pending_edge(
                    "Danny", 
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
        
        edge_result = insert_pending_edge(
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
        
        # 6b. Clarifier: evaluate new pending edges for contradictions
        if edge_result.get("status") == "inserted":
            try:
                edge_clar = evaluate_edge({
                    "source_label": s_c,
                    "target_label": t_c,
                    "relationship": rel,
                    "source_type": s_res.get("node_type", "concept"),
                    "target_type": t_res.get("node_type", "concept"),
                    "confidence": 0.5,  # Default confidence for LLM-extracted edges
                }, batch_mode=True)
                if edge_clar:
                    asyncio.ensure_future(store_and_send_clarification(edge_clar, "pending_graph_edges", str(edge_result.get("id", ""))))
            except Exception as edge_clar_err:
                audit_log_sync("graph_pipeline", "WARNING", f"Clarifier evaluate_edge failed: {edge_clar_err}")

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

