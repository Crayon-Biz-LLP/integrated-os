import os
import asyncio
from datetime import datetime, timezone, timedelta
from supabase import create_client, Client
from core.lib.audit_logger import audit_log_sync
from core.services.db import versioned_update
from core.pulse.llm import call_llm_with_fallback, get_embedding

supabase: Client = create_client(
    os.getenv("SUPABASE_URL"),
    os.getenv("SUPABASE_SERVICE_ROLE_KEY")
)


async def write_outcome_memory(task_title: str, project_name: str = None):
    """
    Writes a type:outcome memory when a task is completed.
    Non-blocking. Mirrors the same pattern as reflection writes in AAR.
    """
    try:
        label = f"Completed: {task_title}"
        if project_name:
            label += f" on {project_name}"

        embedding = await asyncio.to_thread(get_embedding, label)
        status = 'success' if embedding and any(embedding) else 'failed'
        supabase.table('memories').insert({
            "content": label,
            "memory_type": "outcome",
            "embedding": embedding,
            "embedding_status": status,
            "source": "pulse_outcome"
        }).execute()
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
        # Generate embedding for the query
        query_embedding = await asyncio.to_thread(get_embedding, query_text)

        if not query_embedding or all(v == 0 for v in query_embedding):
            return ""

        # Semantic search for relevant memories (last 30 days)
        from datetime import timedelta
        thirty_days_ago = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()

        memories_res = supabase.rpc('match_memories', {
            'query_embedding': query_embedding,
            'match_threshold': 0.7,
            'match_count': max_memories,
        }).execute()
        if memories_res.data:
            cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
            memories_res.data = [m for m in memories_res.data
                                 if m.get('created_at')
                                 and m['created_at'] >= cutoff]

        if not memories_res.data:
            return ""

        # Format memories for briefing context
        memory_entries = []
        for m in memories_res.data:
            memory_type = m.get('memory_type', 'note')
            content = m.get('content', '')[:200]  # Truncate to 200 chars
            memory_entries.append(f"[{memory_type.upper()}] {content}")

        result = "\n".join(memory_entries)
        print(f"🧠 Retrieved {len(memories_res.data)} relevant memories for briefing")
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
                embedding = await asyncio.to_thread(get_embedding, query_text)
                if not any(embedding): return []
                res = supabase.rpc(
                    'match_memories',
                    {
                        'query_embedding': embedding,
                        'match_count': top_k,
                        'match_threshold': 0.6
                    }
                ).execute()
                return res.data if res.data else []
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
                f"[MEMORY CONTEXT ONLY — DO NOT LIST IN BRIEFING] {m.get('memory_type', '').upper()}: {m.get('content', '')}"
                for m in top_memories
            ]
            return (formatted, latest_timestamp)
    except Exception as e:
        audit_log_sync("pulse", "ERROR", f"High-Res Hindsight error: {e}")
    return ([], None)

async def generate_after_action_report() -> str:
    """Generate an After-Action Report on the day's activities and save to memories."""
    try:
        now = datetime.now(timezone(timedelta(hours=5, minutes=30)))
        today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()

        completed_tasks_res = supabase.table('tasks').select('title').eq('status', 'done').gte('completed_at', today_start).execute()
        completed_count = len(completed_tasks_res.data) if completed_tasks_res.data else 0

        open_tasks_res = supabase.table('tasks').select('id').eq('status', 'todo').eq('is_current', True).execute()
        open_count = len(open_tasks_res.data) if open_tasks_res.data else 0

        prompt = f"""You are Danny's Rhodey. Provide a dry After-Action Report (AAR). 1-2 sentences max. Focus on loops closed vs. open.
        - Loops closed today: {completed_count}
        - Loops still open: {open_count}"""

        response = await call_llm_with_fallback(
            prompt=prompt,
            is_critical=False,
            require_json=False
        )

        lesson = response.text.strip()

        if lesson and len(lesson) > 10:
            embedding = await asyncio.to_thread(get_embedding, lesson)
            status = 'success' if embedding and any(embedding) else 'failed'
            if status == 'failed':
                audit_log_sync("pulse", "WARNING", f"Warning: zero-vector embedding for daily reflection — storing with failed status")
            supabase.table('memories').insert({
                "content": lesson,
                "memory_type": "reflection",
                "embedding": embedding,
                "embedding_status": status,
                "source": "pulse_reflection"
            }).execute()
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

        # Search memories from same month/day in previous years
        memories_res = supabase.table('memories') \
            .select('content, memory_type, created_at') \
            .or_(f"created_at::text.ilike.*{today.month:02}-{today.day:02}*") \
            .order('created_at', desc=True) \
            .limit(10) \
            .execute()

        if not memories_res.data:
            return ""

        lines = [f"📅 TEMPORAL PATTERNS (On this day {today_str}):"]
        seen = set()

        for m in memories_res.data:
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

async def serendipity_engine(active_tasks: list, people: list, resources: list) -> str:
    """
    SERENDIPITY ENGINE: Surfaces unexpected connections and cross-domain insights.
    Finds non-obvious links between tasks, people, resources, and past memories
    that could spark new ideas or reveal hidden opportunities.
    """
    try:
        insights = []

        # 1. Cross-domain task connections
        # Find tasks from different org_tags that share keywords
        if len(active_tasks) >= 2:
            from collections import defaultdict
            keyword_tasks = defaultdict(list)

            for t in active_tasks[:20]:  # Limit to avoid token bloat
                title_words = set(t.get('title', '').lower().split())
                for word in title_words:
                    if len(word) > 4:  # Only meaningful keywords
                        keyword_tasks[word].append(t.get('title', ''))

            # Find keywords that appear in tasks from different domains
            for keyword, task_titles in keyword_tasks.items():
                if len(task_titles) >= 2:
                    insights.append(f"🔗 Keyword '{keyword}' connects: {' | '.join(task_titles[:3])}")

        # 2. People + Resources serendipity
        # Find resources that mention people but aren't directly linked
        if people and resources:
            for person in people[:5]:
                person_name = person.get('name', '')
                if not person_name:
                    continue
                related_resources = [
                    (r.get('title', '') or '') for r in resources[:30]
                    if person_name.lower() in ((r.get('title', '') or '') + (r.get('strategic_note', '') or '')).lower()
                ]
                if len(related_resources) >= 2:
                    insights.append(f"👤 {person_name} appears in: {' | '.join(related_resources[:3])}")

        # 3. Temporal serendipity - resources created on same day as memories
        try:
            from datetime import date
            today = date.today()
            thirty_days_ago = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()

            recent_resources = [r for r in resources if r.get('created_at', '') > thirty_days_ago]
            if recent_resources and len(recent_resources) >= 2:
                insights.append(f"📚 Recent additions ({len(recent_resources)} resources in 30d) may have hidden connections to current tasks")
        except:
            pass

        if insights:
            lines = ["✨ SERENDIPITY FINDS:"]
            lines.extend(insights[:5])  # Cap at 5 insights
            return "\n".join(lines)

        return ""

    except Exception as e:
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
                morning_count = sum(1 for m in recent_memories.data
                                   if m.get('created_at', '').startswith('0') or
                                   m.get('created_at', '').startswith('1') or
                                   m.get('created_at', '').startswith('2'))
                evening_count = sum(1 for m in recent_memories.data
                                  if m.get('created_at', '').startswith('1') or
                                  m.get('created_at', '').startswith('2'))

                if morning_count > evening_count * 2:
                    insights.append("🌅 Morning briefings seem more reflective — consider adding deeper synthesis")
                elif evening_count > morning_count * 2:
                    insights.append("🌙 Evening briefings generate more insights — consider longer night briefings")
        except:
            pass

        # 2. Section density learning
        # Track if certain sections are consistently empty and suggest hiding them
        try:
            recent_tasks = supabase.table('tasks') \
                .select('org_tag, priority, status') \
                .eq('status', 'active') \
                .execute()

            if recent_tasks.data:
                tag_counts = {}
                for t in recent_tasks.data:
                    tag = t.get('org_tag', 'INBOX')
                    tag_counts[tag] = tag_counts.get(tag, 0) + 1

                # Suggest hiding sections with < 2 tasks
                sparse_tags = [tag for tag, count in tag_counts.items() if count < 2]
                if sparse_tags:
                    insights.append(f"📊 Sparse sections detected: {', '.join(sparse_tags)} — consider condensing")
        except:
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
