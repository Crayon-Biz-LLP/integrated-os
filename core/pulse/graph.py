import os
import json
import asyncio
from datetime import datetime, timezone, timedelta
from supabase import create_client, Client
from core.lib.audit_logger import audit_log_sync
from core.pulse.llm import get_embedding, cosine_similarity

supabase: Client = create_client(
    os.getenv("SUPABASE_URL"),
    os.getenv("SUPABASE_SERVICE_ROLE_KEY")
)


async def write_graph_edges_for_task(task_id: int, task_title: str, project_id: int = None, task_description: str = None, people_cache=None):
    """
    Add-on: Writes graph edges after a task is created.
    Non-blocking. If this fails, the task is already saved — no rollback needed.
    """
    try:
        task_node = supabase.table('graph_nodes') \
            .select('id') \
            .eq('type', 'task') \
            .filter('metadata->>task_id', 'eq', str(task_id)) \
            .maybe_single() \
            .execute()

        if task_node and task_node.data:
            task_node_id = task_node.data['id']
        else:
            new_node = supabase.table('graph_nodes').insert({
                "label": task_title,
                "type": "task",
                "metadata": {
                    "source": "tasks_table",
                    "task_id": task_id,
                    "project_id": project_id
                }
            }).execute()
            task_node_id = new_node.data[0]['id']

        if project_id:
            proj_node = supabase.table('graph_nodes') \
                .select('id') \
                .eq('type', 'project') \
                .filter('metadata->>project_id', 'eq', str(project_id)) \
                .maybe_single() \
                .execute()

            if proj_node and proj_node.data:
                existing = supabase.table('graph_edges') \
                    .select('id') \
                    .eq('source_node_id', task_node_id) \
                    .eq('target_node_id', proj_node.data['id']) \
                    .eq('relationship', 'BELONGS_TO') \
                    .maybe_single() \
                    .execute()

                if not existing or not existing.data:
                    supabase.table('graph_edges').insert({
                        "source_node_id": task_node_id,
                        "target_node_id": proj_node.data['id'],
                        "relationship": "BELONGS_TO",
                        "weight": 1.0,
                        "metadata": {"source": "task_engine", "task_id": task_id}
                    }).execute()

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
                    existing_edge = supabase.table('graph_edges') \
                        .select('id') \
                        .eq('source_node_id', task_node_id) \
                        .eq('target_node_id', person_node.data['id']) \
                        .eq('relationship', 'INVOLVES') \
                        .maybe_single() \
                        .execute()

                    if not existing_edge or not existing_edge.data:
                        supabase.table('graph_edges').insert({
                            "source_node_id": task_node_id,
                            "target_node_id": person_node.data['id'],
                            "relationship": "INVOLVES",
                            "weight": 1.0,
                            "metadata": {
                                "source": "task_engine",
                                "task_id": task_id,
                                "matched_name": person['name']
                            }
                        }).execute()

        print(f"🕸️ Graph edges written for task {task_id}: '{task_title}'")

    except Exception as e:
        audit_log_sync("pulse", "WARNING", f"⚠️ Graph edge write failed (non-critical): {e}")

async def hybrid_search_graph(query: str) -> str:
    """Graph-first search: Find primary entity and its connections."""
    try:
        nodes_res = supabase.table('graph_nodes').select('id, label').ilike('label', f'%{query}%').limit(1).execute()

        # TODO: If match_graph_nodes RPC does not exist yet in Supabase,
        # create it mirroring the match_memories pattern for graph_nodes table.
        if not nodes_res.data:
            try:
                query_embedding = await asyncio.to_thread(get_embedding, query)
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
                        meta = target_node_res.data.get('metadata', {})
                        if isinstance(meta, str):
                            try:
                                meta = json.loads(meta)
                            except:
                                meta = {}
                        dep_task_id = meta.get('task_id')

                        if dep_task_id and int(dep_task_id) in task_map:
                            dep_task = task_map[int(dep_task_id)]
                            dep_status = dep_task.get('status', '')

                            if dep_status not in ['done', 'cancelled']:
                                blocked_tasks.append({
                                    'task': task_title,
                                    'depends_on': dep_task.get('title', ''),
                                    'dep_status': dep_status
                                })

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
                email_res = supabase.table('emails') \
                    .select('id', count='exact') \
                    .or_(f'sender.ilike.%{person_name}%,linked_person_id.eq.{person_id}') \
                    .execute()
                email_count = email_res.count or 0
            except:
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

        lines = []
        task_map = {t['id']: t for t in active_tasks}

        # Get all person nodes
        people_ids = {p['id']: p['name'] for p in people}
        person_nodes = supabase.table('graph_nodes') \
            .select('id, label, metadata') \
            .eq('type', 'person') \
            .execute()

        # Build node_id → person_name map
        node_to_person = {}
        for node in (person_nodes.data or []):
            meta = node.get('metadata', {})
            if isinstance(meta, str):
                try:
                    meta = json.loads(meta)
                except:
                    continue
            people_id = meta.get('people_id')
            if people_id and int(people_id) in people_ids:
                node_to_person[node['id']] = people_ids[int(people_id)]

        # Find INVOLVES edges linking person nodes to task nodes
        task_nodes = supabase.table('graph_nodes') \
            .select('id, metadata') \
            .eq('type', 'task') \
            .execute()

        task_node_ids = []
        task_node_map = {}  # node_id → task_id
        for node in (task_nodes.data or []):
            meta = node.get('metadata', {})
            if isinstance(meta, str):
                try:
                    meta = json.loads(meta)
                except:
                    continue
            task_id = meta.get('task_id')
            if task_id and int(task_id) in task_map:
                task_node_ids.append(node['id'])
                task_node_map[node['id']] = int(task_id)

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
