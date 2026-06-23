from core.llm import get_embedding
import time
import asyncio
from datetime import datetime, timezone, timedelta

from core.services.db import get_supabase
from core.services.google_service import get_google_calendar_events
from core.services.outlook_service import get_outlook_calendar_events
from core.lib.redis_cache import cache_get, cache_set, cache_delete
from core.lib.time_utils import age_tag
from core.lib.audit_logger import audit_log_sync
from core.retrieval.config import config as retrieval_config

supabase = get_supabase()

class SimpleCache:
    """A lightweight TTL cache to avoid redundant DB queries. Backed by Redis if configured."""
    def __init__(self, ttl_seconds=60, redis_key=None):
        self.ttl = ttl_seconds
        self.redis_key = redis_key
        self.data = None
        self.timestamp = 0

    def get(self):
        if self.data is not None and (time.time() - self.timestamp) < self.ttl:
            return self.data
            
        if self.redis_key:
            redis_data = cache_get(self.redis_key)
            if redis_data is not None:
                self.data = redis_data
                self.timestamp = time.time()
                return redis_data
                
        return None

    def set(self, data):
        self.data = data
        self.timestamp = time.time()
        
        if self.redis_key:
            cache_set(self.redis_key, data, ttl=self.ttl)

    def invalidate(self):
        self.data = None
        self.timestamp = 0
        if self.redis_key:
            cache_delete(self.redis_key)


class ContextProvider:
    """
    Phase 2: Context Hydration Engine
    Pre-computes and caches context. Uses semantic selection + hard safeguards 
    to prioritize relevant tasks/memories without exceeding token budgets.
    """
    def __init__(self):
        self.caches = {
            'tasks': SimpleCache(ttl_seconds=30, redis_key="rhodey:cache:tasks"),
            'projects': SimpleCache(ttl_seconds=300, redis_key="rhodey:cache:projects"),
            'people': SimpleCache(ttl_seconds=300, redis_key="rhodey:cache:people"),
            'calendar': SimpleCache(ttl_seconds=300, redis_key="rhodey:cache:calendar"),
            'recent_tasks': SimpleCache(ttl_seconds=60, redis_key="rhodey:cache:recent_tasks")
        }
        
    def cosine_similarity(self, vec_a, vec_b):
        if not vec_a or not vec_b:
            return 0.0
        dot = sum(a * b for a, b in zip(vec_a, vec_b))
        norm_a = sum(a * a for a in vec_a) ** 0.5
        norm_b = sum(b * b for b in vec_b) ** 0.5
        if norm_a == 0 or norm_b == 0:
            return 0.0
        return dot / (norm_a * norm_b)

    async def get_projects(self):
        cached = self.caches['projects'].get()
        if cached is not None:
            return cached
            
        res = supabase.table('projects').select('*').eq('status', 'active').execute()
        projects = res.data or []
        self.caches['projects'].set(projects)
        return projects

    async def get_active_tasks(self):
        cached = self.caches['tasks'].get()
        if cached is not None:
            return cached
            
        res = supabase.table('tasks')\
            .select('id, title, project_id, priority, created_at, reminder_at, status')\
            .eq('is_current', True)\
            .not_.in_('status', ['done', 'cancelled'])\
            .execute()
        tasks = res.data or []
        self.caches['tasks'].set(tasks)
        return tasks
        
    async def get_calendar_events(self, target_date):
        cached = self.caches['calendar'].get()
        if cached is not None:
            return cached
            
        events = []
        try:
            google_ev = await asyncio.to_thread(get_google_calendar_events, target_date)
            events.extend(google_ev)
        except Exception:
            pass
            
        try:
            outlook_ev = await asyncio.to_thread(get_outlook_calendar_events, target_date)
            events.extend(outlook_ev)
        except Exception:
            pass
            
        events.sort(key=lambda x: x.get("time", ""))
        self.caches['calendar'].set(events)
        return events

    async def get_people(self):
        cached = self.caches['people'].get()
        if cached is not None:
            return cached
            
        res = supabase.table('people').select('id, name, strategic_weight').execute()
        people = res.data or []
        self.caches['people'].set(people)
        return people
        
    async def get_recently_completed_tasks(self, hours: int = 24):
        cached = self.caches['recent_tasks'].get()
        if cached is not None:
            return cached
            
        since_utc = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        res = supabase.table('tasks') \
            .select('title, project_id, updated_at') \
            .eq('is_current', True) \
            .eq('status', 'done') \
            .gte('updated_at', since_utc) \
            .order('updated_at', desc=True) \
            .limit(10) \
            .execute()
            
        completed = res.data or []
        self.caches['recent_tasks'].set(completed)
        return completed

    async def get_range_calendar_events(self, start_date, end_date, max_days=14):
        delta_days = (end_date - start_date).days
        if delta_days > max_days:
            end_date = start_date + timedelta(days=max_days)

        cache_key = f"rhodey:cache:calendar_range:{start_date.isoformat()}:{end_date.isoformat()}:{max_days}"
        cached = cache_get(cache_key)
        if cached is not None:
            return cached

        events = []
        try:
            from core.services.google_service import get_cached_service, format_rfc3339
            service = await asyncio.to_thread(get_cached_service, 'calendar', 'v3')
            rfc_start = format_rfc3339(start_date.isoformat())
            rfc_end = format_rfc3339(end_date.isoformat())
            events_res = await asyncio.to_thread(
                lambda: service.events().list(
                    calendarId="primary",
                    timeMin=rfc_start,
                    timeMax=rfc_end,
                    singleEvents=True,
                    orderBy="startTime",
                    maxResults=50,
                ).execute()
            )
            for e in events_res.get("items", []):
                start = e.get("start", {})
                dt = start.get("dateTime") or start.get("date", "")
                events.append({
                    "time": dt,
                    "title": e.get("summary", "Untitled"),
                    "source": "google",
                })
        except Exception:
            pass

        try:
            from core.services.outlook_service import get_outlook_calendar_events_range
            outlook_ev = await asyncio.to_thread(get_outlook_calendar_events_range, start_date, end_date)
            events.extend(outlook_ev)
        except Exception:
            pass

        events.sort(key=lambda x: x.get("time", ""))
        
        if delta_days > max_days and len(events) > 3:
            events = events[:3]
            events.append({"time": "", "title": f"...and {delta_days - max_days} more days. Output truncated to 14 days.", "source": "system"})

        cache_set(cache_key, events, ttl=120)
        return events

    async def get_resources_context(self, query_text: str, match_count: int = 5):
        if not query_text:
            return "None"
        try:
            embedding = (await get_embedding(query_text)).vector
            if not embedding:
                return "None"
            res = supabase.rpc('match_resources', {
                'query_embedding': embedding,
                'match_threshold': 0.5,
                'match_count': match_count
            }).execute()
            resources = res.data or []
            if not resources:
                return "None"
            lines = []
            for r in resources:
                lines.append(f"- {r.get('url', '')}")
            return "\n".join(lines)
        except Exception as e:
            print(f"Resource hydration failed: {e}")
            return "None"

    async def get_practices_context(self):
        try:
            res = supabase.table('graph_nodes').select('label, metadata').eq('type', 'practice').execute()
            practices = [p for p in (res.data or []) if p.get('metadata', {}).get('status') in ['active', 'dormant']]
            if not practices:
                return "None"
            lines = []
            for p in practices:
                meta = p.get('metadata', {})
                freq = meta.get('frequency_observed', '0/14days')
                status = meta.get('status', 'active')
                lines.append(f"- {p.get('label', '')} ({status}, {freq})")
            return "\n".join(lines)
        except Exception as e:
            print(f"Practices hydration failed: {e}")
            return "None"

    async def get_calendar_context_formatted(self, target_date):
        events = await self.get_calendar_events(target_date)
        if not events:
            return "None"
            
        lines = []
        for e in events:
            try:
                t = e["time"][:16].replace("T", " ")
                src = "Google" if e.get("source") == "google" else "Outlook"
                lines.append(f"- {t} - {e['title']} ({src})")
            except Exception:
                lines.append(f"- {e.get('title', 'Untitled')}")
        return "\n".join(lines)

    async def hydrate_tasks_context(self, query_text: str = None, max_chars: int = 4000):
        """
        Implements semantic selection with hard safeguards.
        1. Always-include: urgent, overdue, due today.
        2. Semantic Tail: remaining tasks ranked by similarity to query_text.
        """
        tasks = await self.get_active_tasks()
        projects = await self.get_projects()
        proj_map = {p['id']: p for p in projects}
        
        now = datetime.now(timezone.utc)
        now_iso = now.isoformat()
        tomorrow_iso = (now + timedelta(days=1)).isoformat()
        
        always_include = []
        semantic_pool = []
        
        for t in tasks:
            is_urgent = t.get('priority') == 'urgent'
            reminder = t.get('reminder_at')
            
            # Check overdue or due today/tomorrow
            is_due_soon = False
            if reminder:
                if reminder < now_iso:
                    is_due_soon = True # overdue
                elif reminder < tomorrow_iso:
                    is_due_soon = True # due today
            
            p_data = proj_map.get(t.get('project_id'))
            p_name = p_data['name'] if p_data else "General"
            org_tag = p_data.get('org_tag', 'INBOX') if p_data else "INBOX"
            
            formatted = f"[{org_tag} >> {p_name}] {t.get('title')} ({t.get('priority')}) [ID:{t.get('id')}]"
            
            if is_urgent or is_due_soon:
                always_include.append(formatted)
            else:
                semantic_pool.append({"task": t, "formatted": formatted, "score": 0.0})
                
        # For tasks, lexical/recency ranking is faster and safer
        for item in semantic_pool:
            t = item["task"]
            score = 0.0
            if t.get('priority') == 'important':
                score += 50
            # Recency boost
            try:
                created = datetime.fromisoformat(t['created_at'].replace('Z', '+00:00'))
                days_old = (now - created).days
                if days_old <= 2:
                    score += 30
                elif days_old > 14:
                    score -= 20
            except Exception:
                pass
            item["score"] = score
            
        semantic_pool.sort(key=lambda x: x["score"], reverse=True)
        
        final_list = list(always_include)
        
        current_len = sum(len(x) + 3 for x in final_list)
        
        for item in semantic_pool:
            added_len = len(item["formatted"]) + 3
            if current_len + added_len > max_chars:
                break
            final_list.append(item["formatted"])
            current_len += added_len
            
        remaining = len(semantic_pool) - (len(final_list) - len(always_include))
        
        compressed_tasks = " | ".join(final_list)
        if remaining > 0:
            compressed_tasks += f" | ...and {remaining} more tasks in /library"
            
        # Also return a universal map for ID matching
        universal = " | ".join([f"[ID:{t['id']}] {t['title']}" for t in tasks])
        
        return compressed_tasks, universal[:4000]

    async def hydrate_memories_context(self, query_text: str, match_count: int = 5, return_raw: bool = False, recency_weight: float = 0.3):
        """Uses pgvector to find semantically relevant memories, with recency weighting."""
        if not query_text:
            return [] if return_raw else "None"
            
        try:
            from core.retrieval.search import search_memories_compat
            memories = await search_memories_compat(
                query_text=query_text,
                top_k=match_count,
                threshold=0.6,
                recency_weight=recency_weight,
                importance_weight=0.2,
                use_associative=retrieval_config.associative_enabled_hydrate,
            )
            if return_raw:
                return memories

            # Shadow mode: run associative retrieval alongside for comparison
            if retrieval_config.shadow_mode and query_text:
                asyncio.create_task(_shadow_comparison(query_text, memories, match_count))

            if not memories:
                return "None"
                
            lines = []
            for m in memories:
                lines.append(f"{age_tag(m.get('created_at'))} [{m.get('memory_type', 'note').upper()}] {m.get('content')}")
            return "\n".join(lines)
            
        except Exception as e:
            print(f"Memory hydration failed: {e}")
            return [] if return_raw else "None"

    async def get_email_context(self, query_text: str, match_count: int = 3):
        if not query_text:
            return "None"
        try:
            embedding = (await get_embedding(query_text)).vector
            if not embedding:
                return "None"
            res = supabase.rpc('match_emails_hybrid', {
                'query_embedding': embedding,
                'match_count': match_count,
                'match_threshold': 0.5
            }).execute()
            emails = res.data or []
            if not emails:
                return "None"
            lines = []
            for e in emails:
                lines.append(f"- From {e.get('sender', '')}: {e.get('subject', '')} ({e.get('body_summary', '')})")
            return "\n".join(lines)
        except Exception as e:
            print(f"Email hydration failed: {e}")
            return "None"

    async def get_whatsapp_context(self, query_text: str, match_count: int = 5):
        if not query_text:
            return "None"
        try:
            embedding = (await get_embedding(query_text)).vector
            if not embedding:
                return "None"
            res = supabase.rpc('match_whatsapp_hybrid', {
                'query_embedding': embedding,
                'match_count': match_count,
                'match_threshold': 0.5
            }).execute()
            msgs = res.data or []
            if not msgs:
                return "None"
            lines = []
            for m in msgs:
                lines.append(f"- {m.get('sender_name', '')}: {m.get('message_text', '')}")
            return "\n".join(lines)
        except Exception as e:
            print(f"WhatsApp hydration failed: {e}")
            return "None"

    async def get_pending_decisions_context(self):
        try:
            pending_lines = []
            rejected_lines = []
            
            seven_days_ago = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
            
            # Pending
            p_res = supabase.table('messages').select('id, channel, suggested_title, sender_name, has_memory_value').in_('channel', ['email', 'call', 'whatsapp']).is_('danny_decision', 'null').execute()
            if p_res.data:
                for t in p_res.data:
                    if t['channel'] == 'whatsapp' and t.get('has_memory_value'):
                        continue
                    prefix = "e" if t['channel'] == 'email' else "c" if t['channel'] == 'call' else "w"
                    suffix = f" (from {t.get('sender_name', '')})" if t['channel'] == 'whatsapp' and t.get('sender_name') else ""
                    pending_lines.append(f"- [{t['channel'].upper()}] {prefix}{t['id']} - {t.get('suggested_title', '')}{suffix}")
                    
            # Rejected
            r_res = supabase.table('messages').select('id, channel, suggested_title, sender_name').in_('channel', ['email', 'call', 'whatsapp']).eq('danny_decision', 'rejected').gte('created_at', seven_days_ago).order('created_at', desc=True).limit(15).execute()
            if r_res.data:
                for t in r_res.data:
                    prefix = "e" if t['channel'] == 'email' else "c" if t['channel'] == 'call' else "w"
                    suffix = f" (from {t.get('sender_name', '')})" if t['channel'] == 'whatsapp' and t.get('sender_name') else ""
                    rejected_lines.append(f"- [{t['channel'].upper()}] {prefix}{t['id']} - {t.get('suggested_title', '')}{suffix}")
            
            result_blocks = []
            if pending_lines:
                result_blocks.append("PENDING APPROVALS:\n" + "\n".join(pending_lines))
            if rejected_lines:
                result_blocks.append("PREVIOUSLY REJECTED SUGGESTIONS (last 7d):\n" + "\n".join(rejected_lines))
                
            if not result_blocks:
                return "None"
            return "\n\n".join(result_blocks)
        except Exception as e:
            print(f"Pending decisions hydration failed: {e}")
            return "None"

    async def get_cross_referenced_context(self, query_text: str, task_inputs: list, people: list, projects: list, match_count: int = 5):
        """
        Runs hybrid pgvector search and graph edge search in parallel,
        and cross-references memories with graph connections.
        """
        from core.pulse.graph import fetch_hybrid_graph_context
        
        # 1. Fetch raw memories and graph context in parallel
        memories_task = self.hydrate_memories_context(query_text, match_count=match_count, return_raw=True, recency_weight=0.3)
        graph_task = fetch_hybrid_graph_context(people, projects, task_inputs)
        
        memories, graph_context = await asyncio.gather(memories_task, graph_task)
        
        if not memories and not graph_context:
            return "None"
            
        # 2. Extract entity names from people and projects
        entity_terms = set(p.get('name', '').lower() for p in people if p.get('name'))
        entity_terms.update(p.get('name', '').lower() for p in projects if p.get('name'))
        
        # 3. Format and cross-reference
        lines = []
        for m in (memories or []):
            content = m.get('content', '')
            content_lower = content.lower()
            
            # Check if this memory mentions any known entities
            found_entities = [term for term in entity_terms if term in content_lower and len(term) > 3]
            
            prefix = f"[{m.get('memory_type', 'note').upper()}]"
            if found_entities:
                # Highlight the entities it connects to
                prefix += f" (Links to: {', '.join(found_entities).title()})"
                
            lines.append(f"{age_tag(m.get('created_at'))} {prefix} {content}")
            
        # 4. Merge results
        result_blocks = []
        if lines:
            result_blocks.append("MEMORY CONTEXT:")
            result_blocks.append("\n".join(lines))
            
        if graph_context:
            result_blocks.append(graph_context)
            
        return "\n\n".join(result_blocks)
        
# Global instance
context_provider = ContextProvider()


async def _shadow_comparison(query: str, current_memories: list, top_k: int):
    """Fire-and-forget: run associative retrieval alongside current RPC for comparison."""
    try:
        from core.retrieval.search import associative_retrieve

        current_ids = set(str(m.get("id", "")) for m in (current_memories or []))

        bundle = await associative_retrieve(query=query, top_k=top_k)
        new_ids = set(str(item.memory_id) for item in bundle.items)

        overlap = current_ids & new_ids
        audit_log_sync(
            "retrieval", "INFO",
            f"shadow_mode query={query[:40]}... "
            f"current={len(current_ids)} new={len(new_ids)} "
            f"overlap={len(overlap)} {bundle.latency_ms}ms"
        )
    except Exception:
        pass  # Shadow mode failures must never affect the production path
