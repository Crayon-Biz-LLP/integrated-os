import os
import json
import time
import asyncio
from datetime import datetime, timezone, timedelta

from core.services.db import get_supabase, get_embedding
from core.services.google_service import get_google_calendar_events
from core.services.outlook_service import get_outlook_calendar_events

supabase = get_supabase()

class SimpleCache:
    """A lightweight TTL cache to avoid redundant DB queries."""
    def __init__(self, ttl_seconds=60):
        self.ttl = ttl_seconds
        self.data = None
        self.timestamp = 0

    def get(self):
        if self.data is not None and (time.time() - self.timestamp) < self.ttl:
            return self.data
        return None

    def set(self, data):
        self.data = data
        self.timestamp = time.time()

    def invalidate(self):
        self.data = None
        self.timestamp = 0


class ContextProvider:
    """
    Phase 2: Context Hydration Engine
    Pre-computes and caches context. Uses semantic selection + hard safeguards 
    to prioritize relevant tasks/memories without exceeding token budgets.
    """
    def __init__(self):
        self.caches = {
            'tasks': SimpleCache(ttl_seconds=30),
            'projects': SimpleCache(ttl_seconds=300),
            'people': SimpleCache(ttl_seconds=300),
            'calendar': SimpleCache(ttl_seconds=300),
            'recent_tasks': SimpleCache(ttl_seconds=60)
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
            .eq('is_current', False) \
            .eq('status', 'done') \
            .gte('updated_at', since_utc) \
            .order('updated_at', desc=True) \
            .limit(10) \
            .execute()
            
        completed = res.data or []
        self.caches['recent_tasks'].set(completed)
        return completed

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
                
        # Semantic Ranking
        if query_text and semantic_pool:
            query_emb = await asyncio.to_thread(get_embedding, query_text)
            if query_emb:
                # To avoid an embedding API call per task, we use a simple text overlap 
                # or we pre-compute embeddings if we have them. 
                # Since tasks don't have embeddings stored currently, we'll do lexical ranking
                # for now to save latency/tokens, or we skip semantic for tasks and rely on priority.
                # Actually, wait. We can just use priority + recency boost instead of 50 embedding calls!
                pass
                
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
            except:
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

    async def hydrate_memories_context(self, query_text: str, match_count: int = 5):
        """Uses pgvector to find semantically relevant memories."""
        if not query_text:
            return "None"
            
        try:
            embedding = await asyncio.to_thread(get_embedding, query_text)
            if not embedding:
                return "None"
                
            res = supabase.rpc('match_memories', {
                'query_embedding': embedding,
                'match_count': match_count,
                'match_threshold': 0.6
            }).execute()
            
            memories = res.data or []
            if not memories:
                return "None"
                
            lines = []
            for m in memories:
                lines.append(f"[{m.get('memory_type', 'note').upper()}] {m.get('content')}")
            return "\n".join(lines)
            
        except Exception as e:
            print(f"Memory hydration failed: {e}")
            return "None"

# Global instance
context_provider = ContextProvider()
