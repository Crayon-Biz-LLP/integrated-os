from core.llm import get_embedding
import time
import re
import asyncio
from datetime import datetime, timezone, timedelta

from core.services.db import get_supabase
from core.services.google_service import get_google_calendar_events
from core.services.outlook_service import get_outlook_calendar_events
from core.lib.redis_cache import cache_get, cache_set, cache_delete
from core.lib.time_utils import age_tag, resolve_relative_dates
from core.lib.audit_logger import audit_log_sync
from core.lib.constants import BOT_SENDERS
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
            'tasks': SimpleCache(ttl_seconds=300, redis_key="rhodey:cache:tasks"),
            'projects': SimpleCache(ttl_seconds=300, redis_key="rhodey:cache:projects"),
            'people': SimpleCache(ttl_seconds=300, redis_key="rhodey:cache:people"),
            'calendar': SimpleCache(ttl_seconds=300, redis_key="rhodey:cache:calendar"),
            'recent_tasks': SimpleCache(ttl_seconds=300, redis_key="rhodey:cache:recent_tasks"),
            'organizations': SimpleCache(ttl_seconds=300, redis_key="rhodey:cache:organizations"),
            'graph_nodes': SimpleCache(ttl_seconds=300, redis_key="rhodey:cache:graph_nodes")
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
            
        res = supabase.table('projects').select('*').eq('status', 'active').eq('is_current', True).execute()
        projects = res.data or []
        self.caches['projects'].set(projects)
        
        return projects

    async def get_organizations(self):
        cached = self.caches['organizations'].get()
        if cached is not None:
            return cached
            
        res = supabase.table('organizations').select('*').eq('is_active', True).execute()
        orgs = res.data or []
        self.caches['organizations'].set(orgs)
        return orgs

    async def get_active_tasks(self):
        cached = self.caches['tasks'].get()
        if cached is not None:
            return cached
            
        res = supabase.table('tasks')\
            .select('id, title, project_id, organization_id, priority, created_at, reminder_at, status, direction, committed_to')\
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
        except Exception as e:
            audit_log_sync('context', 'WARNING', f'Google calendar fetch failed: {e}')
            
        try:
            outlook_ev = await asyncio.to_thread(get_outlook_calendar_events, target_date)
            events.extend(outlook_ev)
        except Exception as e:
            audit_log_sync('context', 'WARNING', f'Outlook calendar fetch failed: {e}')
            
        events.sort(key=lambda x: x.get("time", ""))
        self.caches['calendar'].set(events)
        return events

    async def get_graph_nodes(self):
        """Fetch all active person/organization/project graph nodes with TTL caching.
        
        Single source of truth for the graph_nodes ALL-type query.
        Multiple callers across interrogate_brain() and sub-fetchers
        reuse the cache instead of issuing 5+ redundant HTTP requests.
        Returns list of dicts with id, label, type, normalized_label.
        """
        cached = self.caches['graph_nodes'].get()
        if cached is not None:
            return cached
        
        res = supabase.table('graph_nodes') \
            .select('id, label, type, normalized_label') \
            .in_('type', ['person', 'organization', 'project']) \
            .eq('is_current', True) \
            .execute()
        nodes = res.data or []
        self.caches['graph_nodes'].set(nodes)
        return nodes

    async def get_people(self):
        cached = self.caches['people'].get()
        if cached is not None:
            return cached
            
        res = supabase.table('people').select('id, name, strategic_weight').eq('is_current', True).execute()
        people = res.data or []
        self.caches['people'].set(people)
        return people
        
    async def get_recently_completed_tasks(self, hours: int = 24):
        cached = self.caches['recent_tasks'].get()
        if cached is not None:
            return cached
            
        since_utc = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        res = supabase.table('tasks') \
            .select('title, project_id, organization_id, updated_at') \
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

        cache_key = f"rhodey:cache:calendar_range:{start_date.strftime('%Y-%m-%d')}:{end_date.strftime('%Y-%m-%d')}:{max_days}"
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
        except Exception as e:
            audit_log_sync('context', 'WARNING', f'Google calendar range fetch failed: {e}')

        try:
            from core.services.outlook_service import get_outlook_calendar_events_range
            outlook_ev = await asyncio.to_thread(get_outlook_calendar_events_range, start_date, end_date)
            events.extend(outlook_ev)
        except Exception as e:
            audit_log_sync('context', 'WARNING', f'Outlook calendar range fetch failed: {e}')

        events.sort(key=lambda x: x.get("time", ""))
        
        if delta_days > max_days and len(events) > 3:
            events = events[:3]
            events.append({"time": "", "title": f"...and {delta_days - max_days} more days. Output truncated to 14 days.", "source": "system"})

        cache_set(cache_key, events, ttl=120)
        return events

    async def get_resources_context(self, query_text: str, match_count: int = 5, precomputed_embedding: list = None):
        if not query_text:
            return "None"
        try:
            if precomputed_embedding:
                embedding = precomputed_embedding
            else:
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
            audit_log_sync('context', 'WARNING', f'Resource hydration failed: {e}')
            return "None"

    async def get_email_context(self, query_text: str, match_count: int = 3, precomputed_embedding: list = None):
        if not query_text:
            return "None"
        try:
            if precomputed_embedding:
                embedding = precomputed_embedding
            else:
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
                # Add age_tag and resolve relative dates
                ts = e.get('received_at')
                tag = age_tag(ts)
                body = e.get('body_summary', '')
                if ts:
                    try:
                        dt = datetime.fromisoformat(ts.replace('Z', '+00:00'))
                        body = resolve_relative_dates(body, dt)
                    except Exception:
                        pass
                lines.append(f"{tag} From {e.get('sender', '')}: {e.get('subject', '')} ({body})")
            return "\n".join(lines)
        except Exception as e:
            audit_log_sync('context', 'WARNING', f'Email hydration failed: {e}')
            return "None"

    async def get_whatsapp_context(self, query_text: str, match_count: int = 5, precomputed_embedding: list = None):
        if not query_text:
            return "None"
        try:
            if precomputed_embedding:
                embedding = precomputed_embedding
            else:
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
            # Filter out bot's own responses — old briefings containing task lists
            # shouldn't be fed back as current context (causes hallucination loops)
            lines = []
            for m in msgs:
                sender_name = (m.get('sender_name') or '').lower().strip()
                if sender_name in BOT_SENDERS:
                    continue
                # Add age_tag and resolve relative dates
                text = m.get('message_text', '')
                ts = m.get('received_at')
                tag = age_tag(ts)
                if ts:
                    try:
                        dt = datetime.fromisoformat(ts.replace('Z', '+00:00'))
                        text = resolve_relative_dates(text, dt)
                    except Exception:
                        pass
                lines.append(f"{tag} {m.get('sender_name', '')}: {text}")
            if not lines:
                return "None"
            return "\n".join(lines)
        except Exception as e:
            audit_log_sync('context', 'WARNING', f'WhatsApp hydration failed: {e}')
            return "None"

    async def get_practices_context(self):
        try:
            res = supabase.table('graph_nodes').select('label, metadata').eq('type', 'practice').eq('is_current', True).execute()
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
            audit_log_sync('context', 'WARNING', f'Practices hydration failed: {e}')
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

    async def hydrate_tasks_context(self, query_text: str = None, max_chars: int = 4000, entity_name: str = None):
        """
        Implements semantic selection with hard safeguards.
        1. Always-include: urgent, overdue, due today.
        2. Semantic Tail: remaining tasks ranked by similarity to query_text.
        
        Args:
            query_text: The user's query for semantic matching.
            max_chars: Maximum formatted output length.
            entity_name: Optional entity to filter tasks by (e.g. "Ashraya").
                        When provided, only tasks related to this entity are returned.
        """
        from core.features import is_org_routing_enabled
        tasks, projects, orgs = await asyncio.gather(
            self.get_active_tasks(),
            self.get_projects(),
            self.get_organizations() if is_org_routing_enabled() else asyncio.sleep(0, result=[]),
        )
        proj_map = {p['id']: p for p in projects}
        org_map = {o['id']: o['name'] for o in (orgs or [])}
        
        # Entity-aware task filtering — when a specific entity is resolved (e.g. "Ashraya"),
        # only show tasks that belong to that entity. Prevents hallucinated task lists
        # where the LLM sees all system-wide tasks and assumes they're all related.
        if entity_name:
            entity_lower = entity_name.lower().strip()
            
            # Build set of project IDs that belong to this entity
            entity_project_ids = set()
            entity_org_ids = set()
            for p in projects:
                p_name = p.get('name', '').lower()
                if entity_lower in p_name:
                    entity_project_ids.add(p['id'])
                    if p.get('organization_id'):
                        entity_org_ids.add(p['organization_id'])
            
            # Find orgs whose names match the entity
            for oid, oname in org_map.items():
                if entity_lower in oname.lower():
                    entity_org_ids.add(oid)
                    # Add all projects under this org
                    for p in projects:
                        if p.get('organization_id') == oid:
                            entity_project_ids.add(p['id'])
            
            filtered = []
            for t in tasks:
                t_title = t.get('title', '').lower()
                t_proj = t.get('project_id')
                t_org = t.get('organization_id')
                
                # Include if: title mentions entity, OR project belongs to entity, OR org matches entity
                if (entity_lower in t_title or
                    t_proj in entity_project_ids or
                    t_org in entity_org_ids):
                    filtered.append(t)
                    continue
                # Also check if the task's project's org matches (when task has no direct org)
                if t_proj and t_proj in proj_map:
                    p_org = proj_map[t_proj].get('organization_id')
                    if p_org and p_org in entity_org_ids:
                        filtered.append(t)
            
            tasks = filtered
        
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
            
            if is_org_routing_enabled():
                o_id = t.get('organization_id') or (p_data.get('organization_id') if p_data else None)
                o_name = org_map.get(o_id, 'INBOX')
                if p_name != "General":
                    loc = f"{o_name} · {p_name}"
                else:
                    loc = o_name
                formatted = f"[{loc}] {t.get('title')} ({t.get('priority')}) [ID:{t.get('id')}]"
            else:
                formatted = f"[INBOX >> {p_name}] {t.get('title')} ({t.get('priority')}) [ID:{t.get('id')}]"
            
            if is_urgent or is_due_soon:
                always_include.append(formatted)
            else:
                semantic_pool.append({"task": t, "formatted": formatted, "score": 0.0})
                
        # Embedding-aware similarity boost — use query to find semantically related
        # memories, then boost tasks that those memories reference.
        boosted_task_ids = set()
        if query_text:
            try:
                from core.context import execute_context_strategy, HYDRATE_TASKS_CONFIG
                res = await execute_context_strategy(
                    query=query_text,
                    strategy=HYDRATE_TASKS_CONFIG
                )
                related = [m.metadata for m in res.matched_items]
                if related:
                    for mem in related:
                        content_str = mem.get('content', '')
                        # Extract task ID references from memory content
                        for tid in re.findall(r'\[ID:(\d+)\]', content_str):
                            boosted_task_ids.add(int(tid))
                        # Also check title mentions (case-insensitive)
                        content_lower = content_str.lower()
                        for item in semantic_pool:
                            title_lower = item['task'].get('title', '').lower()
                            if title_lower and len(title_lower) > 3 and title_lower in content_lower:
                                boosted_task_ids.add(item['task']['id'])
            except Exception:
                pass  # Fail-open: embedding boost degrades gracefully
        
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
            # X3: Embedding-aware boost
            if t['id'] in boosted_task_ids:
                score += 100
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
            from core.context import execute_context_strategy, HYDRATE_MEMORIES_CONFIG
            res = await execute_context_strategy(
                query=query_text,
                strategy=HYDRATE_MEMORIES_CONFIG
            )
            memories = [m.metadata for m in res.matched_items]
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
            audit_log_sync('context', 'WARNING', f'Memory hydration failed: {e}')
            return [] if return_raw else "None"

    async def get_pending_decisions_context(self):
        try:
            pending_lines = []
            rejected_lines = []
            
            seven_days_ago = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
            
            # Pending
            p_res = supabase.table('messages').select('id, channel, suggested_title, sender_name, has_memory_value').in_('channel', ['email', 'call', 'whatsapp']).is_('danny_decision', 'null').order('created_at', desc=True).limit(50).execute()
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
            audit_log_sync('context', 'WARNING', f'Pending decisions hydration failed: {e}')
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
            
            # Check if this memory mentions any known entities (word-boundary match)
            found_entities = [term for term in entity_terms if len(term) > 3 and re.search(r'\b' + re.escape(term) + r'\b', content_lower)]
            
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

    async def get_master_page_context(self, project_names: list = None, match_count: int = 3) -> str:
        """
        Fetch canonical master pages for relevant projects.
        Uses ilike matching to find pages whose titles contain any project name.
        Returns a formatted context string for the pulse briefing.
        """
        if not project_names:
            return ""
        try:
            seen_titles = set()
            collected = []
            for name in project_names[:10]:
                raw = name.strip()
                if not raw:
                    continue
                # Escape LIKE wildcards so project names like "100% Review" match literally
                sanitized = raw.replace('%', r'\%').replace('_', r'\_')
                res = supabase.table('canonical_pages') \
                    .select('title, content, last_synth_at, source_count') \
                    .eq('is_current', True) \
                    .ilike('title', f'%{sanitized}%') \
                    .order('last_synth_at', desc=True) \
                    .limit(3) \
                    .execute()
                for p in (res.data or []):
                    tid = p.get('title', '')
                    if tid and tid not in seen_titles:
                        seen_titles.add(tid)
                        collected.append(p)
                if len(collected) >= match_count:
                    break

            if not collected:
                return ""

            lines = ["🗂️ MASTER PAGES:"]
            for p in collected[:match_count]:
                title = p.get('title', 'Unknown')
                content = (p.get('content') or '')[:300]
                last_synth = p.get('last_synth_at', '')
                source_count = p.get('source_count', 0)
                if last_synth:
                    last_synth = str(last_synth)[:10]
                lines.append(f"\n--- {title} (sources: {source_count}, last synced: {last_synth}) ---")
                lines.append(content)
            return "\n".join(lines)
        except Exception as e:
            audit_log_sync('context', 'WARNING', f'Master page context fetch failed: {e}')
            return ""


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
        # Shadow mode failures must never affect the production path
        audit_log_sync('retrieval', 'WARNING', 'Shadow mode comparison failed (non-critical)')
