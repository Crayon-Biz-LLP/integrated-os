from core.services.db import get_supabase
from core.llm import get_embedding
import asyncio
from datetime import datetime, timezone, timedelta
from core.lib.audit_logger import audit_log_sync
from core.lib.time_utils import age_tag
from core.llm.fallback import generate_content_with_fallback
from core.llm.config import WorkloadProfile
from core.retrieval.config import config as retrieval_config

supabase = get_supabase()


async def write_outcome_memory(task_title: str, project_name: str = None):
    """
    Writes a type:outcome memory when a task is completed.
    Non-blocking. Mirrors the same pattern as reflection writes in AAR.
    """
    try:
        label = f"Completed: {task_title}"
        if project_name:
            label += f" on {project_name}"

        embedding = (await get_embedding(label)).vector
        status = 'success' if embedding and any(embedding) else 'failed'
        result = supabase.table('memories').insert({
            "content": label,
            "memory_type": "outcome",
            "embedding": embedding,
            "embedding_status": status,
            "source": "pulse_outcome"
        }).execute()
        memory_id = result.data[0]['id']
        from core.retrieval.pipeline import schedule_index_memory
        asyncio.create_task(schedule_index_memory(memory_id, label, "outcome", "pulse_outcome"))
        print(f"🧠 Outcome memory written: {label}")
    except Exception as e:
        audit_log_sync("pulse", "WARNING", f"⚠️ Outcome memory write failed (non-critical): {e}")

async def get_recent_memories_for_briefing(tasks: list, max_memories: int = 5) -> str:
    """
    Retrieve recent memories semantically related to today's tasks.
    Uses task titles to query match_memories RPC for relevant past insights.
    """
    if not tasks:
        return ""

    # Collect unique project contexts
    project_ids = list(set([
        t.get('project_id') for t in tasks
        if t.get('project_id') and t.get('status') not in ['done', 'cancelled']
    ]))

    if not project_ids:
        return ""

    # Build query from task titles
    query_text = " ".join([
        t.get('title', '') for t in tasks[:5]  # Top 5 tasks
        if t.get('title')
    ])

    if not query_text.strip():
        return ""

    try:
        from core.retrieval.search import search_memories_compat
        memories = await search_memories_compat(
            query_text=query_text,
            top_k=max_memories,
            threshold=0.7,
            recency_weight=0.4,
            importance_weight=0.2,
            use_associative=retrieval_config.associative_enabled_recent_memories,
        )

        if memories:
            cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
            memories = [m for m in memories
                        if m.get('created_at')
                        and m['created_at'] >= cutoff]

        if not memories:
            return ""

        # Shadow mode: run associative retrieval alongside for comparison
        if retrieval_config.shadow_mode and query_text:
            from core.pulse.context import _shadow_comparison
            asyncio.create_task(_shadow_comparison(query_text, memories, max_memories))

        # Format memories for briefing context
        memory_entries = []
        for m in memories:
            memory_type = m.get('memory_type', 'note')
            content = m.get('content', '')[:200]  # Truncate to 200 chars
            memory_entries.append(f"{age_tag(m.get('created_at'))} [{memory_type.upper()}] {content}")

        result = "\n".join(memory_entries)
        print(f"🧠 Retrieved {len(memories)} relevant memories for briefing")
        return result

    except Exception as e:
        audit_log_sync("pulse", "WARNING", f"Recent memories retrieval failed: {e}")
        return ""

async def retrieve_hindsight_memories(task_inputs: list, active_tasks: list, top_k: int = 5, entity_terms: list = None) -> tuple:
    """High-Res Hindsight: Multi-signal vector search across tasks and inputs.
    Returns tuple of (formatted_memories, latest_timestamp).
    """
    latest_timestamp = None
    try:
        search_queries = []

        if task_inputs:
            combined_tasks = " ".join(task_inputs)
            search_queries.append(("combined_tasks", combined_tasks))

        top_active = sorted(active_tasks, key=lambda t: t.get('priority', 'chores') == 'urgent', reverse=True)[:3]
        for t in top_active:
            title = t.get('title', '')
            if title:
                search_queries.append((f"task:{title}", title))

        # Entity-seeded queries from graph traversal (if provided)
        if entity_terms:
            for term in entity_terms[:5]:  # cap at 5 to avoid token bloat
                search_queries.append((f"entity:{term}", term))

        if not search_queries:
            return ([], None)

        async def fetch_memories_for_query(query_name: str, query_text: str):
            try:
                from core.retrieval.search import search_memories_compat
                return await search_memories_compat(
                    query_text=query_text,
                    top_k=top_k,
                    threshold=0.6,
                    recency_weight=0.4,
                    importance_weight=0.2,
                    use_associative=retrieval_config.associative_enabled_hindsight,
                )
            except Exception as e:
                audit_log_sync("pulse", "ERROR", f"Hindsight query error ({query_name}): {e}")
                return []

        all_results = await asyncio.gather(*[fetch_memories_for_query(name, text) for name, text in search_queries])

        seen_ids = set()
        unique_memories = []
        for results in all_results:
            for m in results:
                m_id = m.get('id')
                if m_id and m_id not in seen_ids:
                    seen_ids.add(m_id)
                    unique_memories.append(m)

        unique_memories.sort(key=lambda x: x.get('similarity', 0), reverse=True)
        top_memories = unique_memories[:top_k]

        if top_memories:
            latest_timestamp = top_memories[0].get('created_at')
            formatted = [
                f"{age_tag(m.get('created_at'))} [MEMORY CONTEXT ONLY — DO NOT LIST IN BRIEFING] {m.get('memory_type', '').upper()}: {m.get('content', '')}"
                for m in top_memories
            ]
            return (formatted, latest_timestamp)
    except Exception as e:
        audit_log_sync("pulse", "ERROR", f"High-Res Hindsight error: {e}")
    return ([], None)

async def generate_after_action_report() -> str:
    """Generate an After-Action Report on the day's activities and save to memories."""
    try:
        today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()

        completed_tasks_res = supabase.table('tasks').select('title').eq('is_current', True).eq('status', 'done').gte('completed_at', today_start).execute()
        completed_count = len(completed_tasks_res.data) if completed_tasks_res.data else 0

        open_tasks_res = supabase.table('tasks').select('id').eq('status', 'todo').eq('is_current', True).execute()
        open_count = len(open_tasks_res.data) if open_tasks_res.data else 0

        prompt = f"""You are Danny's Rhodey. Provide a dry After-Action Report (AAR). 1-2 sentences max. Focus on loops closed vs. open.
        - Loops closed today: {completed_count}
        - Loops still open: {open_count}"""

        response = await generate_content_with_fallback(
            prompt=prompt,
            workload=WorkloadProfile.SYNTHESIS,
            require_json=False
        )

        lesson = response.text.strip()

        if lesson and len(lesson) > 10:
            embedding = (await get_embedding(lesson)).vector
            status = 'success' if embedding and any(embedding) else 'failed'
            if status == 'failed':
                audit_log_sync("pulse", "WARNING", "Warning: zero-vector embedding for daily reflection — storing with failed status")
            result = supabase.table('memories').insert({
                "content": lesson,
                "memory_type": "reflection",
                "embedding": embedding,
                "embedding_status": status,
                "source": "pulse_reflection"
            }).execute()
            memory_id = result.data[0]['id']
            from core.retrieval.pipeline import schedule_index_memory
            asyncio.create_task(schedule_index_memory(memory_id, lesson, "reflection", "pulse_reflection"))
            print(f"📝 Daily Reflection saved: {lesson[:50]}...")
            return lesson
    except Exception as e:
        audit_log_sync("pulse", "ERROR", f"Daily reflection error: {e}")
    return ""

async def detect_temporal_patterns() -> str:
    """
    TEMPORAL PATTERN DETECTOR: Surfaces 'On this day' insights from memories
    and detects seasonal patterns in productivity/mood.
    """
    try:
        from datetime import date

        today = date.today()
        today_str = today.strftime("%B %d")
        month_day = f"-{today.month:02}-{today.day:02}"

        # Fetch all memories and filter by month-day in Python
        # (PostgREST's LIKE operator cannot be applied to timestamptz columns)
        memories_res = supabase.table('memories') \
            .select('content, memory_type, created_at') \
            .order('created_at', desc=True) \
            .limit(100) \
            .execute()

        if not memories_res.data:
            return ""

        on_this_day_memories = [
            m for m in memories_res.data
            if month_day in m.get('created_at', '')
        ][:10]

        if not on_this_day_memories:
            return ""

        lines = [f"📅 TEMPORAL PATTERNS (On this day {today_str}):"]
        seen = set()

        for m in on_this_day_memories:
            content = m.get('content', '')[:100]
            mem_type = m.get('memory_type', '')
            created = m.get('created_at', '')[:4]  # Just the year

            if content in seen:
                continue
            seen.add(content)

            lines.append(f"  - {created}: [{mem_type.upper()}] {content}...")

        if len(lines) > 1:
            return "\n".join(lines[:6])  # Cap at 5 memories + header

        return ""

    except Exception as e:
        audit_log_sync("pulse", "WARNING", f"⚠️ Temporal Pattern Detector failed (non-critical): {e}")
        return ""

async def serendipity_engine(active_tasks: list, people: list, resources: list, max_paths: int = 30, pattern_context: str = None) -> str:
    """
    SERENDIPITY ENGINE: Surfaces unexpected multi-hop connections in the knowledge graph.
    Uses PostgreSQL Recursive CTEs to find hidden 2nd and 3rd degree links between today's tasks
    and historical projects, people, or resources.
    """
    try:
        import random
        
        # 1. Gather all active task IDs
        task_ids = []
        for t in active_tasks:
            tid = t.get('id')
            if tid is not None:
                tid_str = str(tid)
                if tid_str.isdigit():
                    task_ids.append(tid_str)
                    
        if not task_ids:
            return "No active tasks to base serendipity queries on."
            
        # 2. Find the graph_node IDs for these tasks
        # Assuming metadata->>task_id is how task nodes are linked
        try:
            nodes_res = supabase.table('graph_nodes').select('id').in_('metadata->>task_id', task_ids).execute()
            start_node_ids = [n['id'] for n in nodes_res.data] if nodes_res and nodes_res.data else []
        except Exception:
            return "Graph nodes unavailable for serendipity query."
        
        if not start_node_ids:
            return "No graph nodes found for active tasks."

        # S6: Add pattern-detected active projects as seed nodes for cross-domain insight
        if pattern_context:
            try:
                pattern_terms = [t.split(':', 1)[1].strip() for t in pattern_context.split('|') if ':' in t]
                if pattern_terms:
                    pattern_nodes = supabase.table('graph_nodes').select('id').in_('label', pattern_terms).execute()
                    if pattern_nodes and pattern_nodes.data:
                        start_node_ids.extend([n['id'] for n in pattern_nodes.data])
            except Exception:
                pass

        # Add people and resources as seed nodes
        entity_labels = []
        for p in people:
            if p.get('name'):
                entity_labels.append(p['name'])
        for r in resources:
            if r.get('title'):
                entity_labels.append(r['title'])
                
        if entity_labels:
            try:
                entity_nodes = supabase.table('graph_nodes').select('id').in_('label', entity_labels).execute()
                if entity_nodes.data:
                    start_node_ids.extend([n['id'] for n in entity_nodes.data])
            except Exception:
                pass
            
        # 3. Call the Supabase RPC
        rpc_res = supabase.rpc('find_serendipity_paths', {'start_node_ids': start_node_ids, 'max_depth': 3}).execute()
        paths = rpc_res.data
        
        if not paths:
            return "No multi-hop connections found in the graph."
            
        # 4. Sample up to max_paths to prevent token bloat and guarantee novelty
        if len(paths) > max_paths:
            paths = random.sample(paths, max_paths)
            
        # 5. Format the paths beautifully for the LLM
        formatted_paths = []
        for path in paths:
            labels = path.get('path_labels', [])
            types = path.get('path_types', [])
            relations = path.get('path_relations', [])
            weight = path.get('total_weight', 0.0)
            
            # Reconstruct the string: Task [X] --RELATES_TO--> Person [Y]
            path_str_parts = []
            for i in range(len(labels)):
                if i == 0:
                    path_str_parts.append(f"{types[i].capitalize()} [{labels[i]}]")
                else:
                    path_str_parts.append(f"--{relations[i]}--> {types[i].capitalize()} [{labels[i]}]")
                    
            path_str = " ".join(path_str_parts)
            formatted_paths.append(f"- Path (Weight {weight}): {path_str}")
            
        final_output = "✨ HIDDEN GRAPH CONNECTIONS (MULTI-HOP):\n" + "\n".join(formatted_paths)
        return final_output

    except Exception as e:
        from core.lib.audit_logger import audit_log_sync
        audit_log_sync("pulse", "WARNING", f"⚠️ Serendipity Engine failed (non-critical): {e}")
        return ""

async def adaptive_briefing_learner(briefing_history: list = None) -> str:
    """
    ADAPTIVE BRIEFING LEARNER: Learns from past briefings to improve future ones.
    Tracks which insights were useful, adjusts briefing style, and personalizes
    the briefing based on Danny's interaction patterns.
    """
    try:
        # For now, implement basic pattern tracking
        # In future, this could read from a 'briefing_feedback' table

        insights = []

        # 1. Check briefing mode effectiveness
        # Track which briefing modes (morning/afternoon/night) produce more actionable insights
        try:
            # Look at recent memories to see which time of day produced more reflections
            recent_memories = supabase.table('memories') \
                .select('content, memory_type, created_at') \
                .gte('created_at', (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()) \
                .execute()

            if recent_memories.data:
                morning_count = 0
                evening_count = 0
                for m in recent_memories.data:
                    dt_str = m.get('created_at', '')
                    if len(dt_str) >= 13:
                        try:
                            hour = int(dt_str[11:13])
                            if hour < 12:
                                morning_count += 1
                            else:
                                evening_count += 1
                        except ValueError:
                            pass

                if morning_count > evening_count * 2:
                    insights.append("🌅 Morning briefings seem more reflective — consider adding deeper synthesis")
                elif evening_count > morning_count * 2:
                    insights.append("🌙 Evening briefings generate more insights — consider longer night briefings")
        except Exception:
            pass

        # 2. Section density learning
        # Track if certain sections are consistently empty and suggest hiding them
        try:
            recent_tasks = supabase.table('tasks') \
                .select('organization_id, priority, status') \
                .eq('status', 'active') \
                .execute()

            if recent_tasks.data:
                tag_counts = {}
                for t in recent_tasks.data:
                    tag = str(t.get('organization_id', 'INBOX'))
                    tag_counts[tag] = tag_counts.get(tag, 0) + 1

                # Suggest hiding sections with < 2 tasks
                sparse_tags = [tag for tag, count in tag_counts.items() if count < 2]
                if sparse_tags:
                    insights.append(f"📊 Sparse sections detected: {', '.join(sparse_tags)} — consider condensing")
        except Exception:
            pass

        # 3. Prompt token optimization suggestion
        insights.append("🎯 Tip: Keep briefings under 3 bullets per section for maximum clarity")

        if insights:
            lines = ["🧠 ADAPTIVE LEARNING:"]
            lines.extend(insights[:4])  # Cap at 4 insights
            return "\n".join(lines)

        return ""

    except Exception as e:
        audit_log_sync("pulse", "WARNING", f"⚠️ Adaptive Briefing Learner failed (non-critical): {e}")
        return ""
