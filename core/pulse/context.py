from core.llm import get_embedding
import time
import re
import asyncio
import json
from datetime import datetime, timezone, timedelta

from core.services.db import tenant_aware_client, get_tenant
from core.services.google_service import get_google_calendar_events
from core.services.outlook_service import get_outlook_calendar_events
from core.lib.redis_cache import cache_get, cache_set, cache_delete
from core.lib.time_utils import age_tag, resolve_relative_dates
from core.lib.audit_logger import audit_log_sync
from core.lib.constants import BOT_SENDERS
from core.retrieval.config import config as retrieval_config

supabase = tenant_aware_client()

class SimpleCache:
    """A lightweight TTL cache to avoid redundant DB queries. Backed by Redis if configured.

    Tenant-aware: the effective Redis key AND the in-memory entry are
    namespaced by the current tenant id (get_tenant). context_provider is a
    module-level singleton shared across all tenants — without this, tenant
    A's tasks/people/calendar cached in-process would be served to tenant B
    (cross-tenant data leak, worse than the Redis-key variant since it leaks
    without any shared cache infra).
    """
    def __init__(self, ttl_seconds=60, redis_key=None):
        self.ttl = ttl_seconds
        self.redis_key = redis_key
        # tenant-scoped key -> (data, fetched_at)
        self._mem = {}

    def _key(self):
        """Effective storage key: redis_key namespaced by the current tenant."""
        if not self.redis_key:
            return None
        uid = get_tenant()
        return f"{self.redis_key}:{uid}" if uid else self.redis_key

    def get(self):
        key = self._key()
        if key is None:
            return None
        now = time.time()
        entry = self._mem.get(key)
        if entry is not None:
            if now - entry[1] < self.ttl:
                return entry[0]
            self._mem.pop(key, None)  # expired → evict, cap memory growth
        redis_data = cache_get(key)
        if redis_data is not None:
            self._mem[key] = (redis_data, now)
            return redis_data
        return None

    def set(self, data):
        key = self._key()
        if key is None:
            return
        self._mem[key] = (data, time.time())
        cache_set(key, data, ttl=self.ttl)

    def invalidate(self):
        key = self._key()
        if key is None:
            return
        self._mem.pop(key, None)
        cache_delete(key)


class ContextProvider:
    """
    Phase 2: Context Hydration Engine
    Pre-computes and caches context. Uses semantic selection + hard safeguards 
    to prioritize relevant tasks/memories without exceeding token budgets.
    """
    def __init__(self):
        self.caches = {
            'tasks': SimpleCache(ttl_seconds=300, redis_key="rhodey:cache:tasks"),
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

    async def get_organizations(self):
        cached = self.caches['organizations'].get()
        if cached is not None:
            return cached

        # Consolidation (migrations 74+75): organizations come from live graph
        # nodes; enrichment lives on the node's metadata. `id` is the graph
        # NODE id (mirror table removed) — the same id tasks.organization_id,
        # projects.organization_id and project_organizations now reference.
        res = supabase.table('graph_nodes') \
            .select('id, label, metadata, db_record_id') \
            .eq('type', 'organization') \
            .eq('is_current', True) \
            .execute()
        orgs = []
        for n in res.data or []:
            meta = n.get('metadata') or {}
            if isinstance(meta, str):
                try:
                    meta = json.loads(meta)
                except Exception:
                    meta = {}
            enrich = meta.get('enrichment') or {}
            orgs.append({
                'name': n.get('label'),
                'id': meta.get('organization_id') or n.get('db_record_id') or n.get('id'),
                'description': enrich.get('description'),
                'is_active': enrich.get('is_active', True),
                'org_type': enrich.get('org_type'),
                'parent_organization_id': enrich.get('parent_organization_id'),
                'is_personal': meta.get('is_personal', False),
            })
        self.caches['organizations'].set(orgs)
        return orgs

    async def get_active_tasks(self):
        cached = self.caches['tasks'].get()
        if cached is not None:
            return cached
            
        res = supabase.table('tasks')\
            .select('id, title, organization_id, pending_org_id, priority, created_at, reminder_at, status, direction, committed_to')\
            .eq('is_current', True)\
            .not_.in_('status', ['done', 'cancelled'])\
            .execute()
        tasks = res.data or []
        self.caches['tasks'].set(tasks)
        return tasks

    def resolve_task_org(self, task: dict) -> str | None:
        """Resolve task's org for graph traversal. Handles pending orgs.

        Path 1: Approved org (fast)
        Path 2: Pending org — find by label in graph_nodes
        Path 3: Pending org still pending — use label for text-based retrieval
        """
        # Path 1: Approved org (fast)
        if task.get('organization_id'):
            return task['organization_id']

        # Path 2: Pending org — find by label
        if task.get('pending_org_id'):
            try:
                pending = supabase.table('pending_nodes').select('label').eq(
                    'id', task['pending_org_id']
                ).single().execute()

                if pending.data:
                    label = pending.data['label']

                    # Try approved graph_node first
                    org_node = supabase.table('graph_nodes').select('id').ilike(
                        'label', label
                    ).eq('type', 'organization').eq('is_current', True).single().execute()
                    if org_node.data:
                        return org_node.data['id']

                    # Org is still pending — return label for text-based retrieval
                    return f"pending:{label}"
            except Exception:
                pass

        return None
        
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
            .in_('type', ['person', 'organization']) \
            .eq('is_current', True) \
            .execute()
        nodes = res.data or []
        self.caches['graph_nodes'].set(nodes)
        return nodes

    async def get_people(self):
        cached = self.caches['people'].get()
        if cached is not None:
            return cached
            
        # Consolidation (migration 74): people come from live person graph
        # nodes — enrichment lives on the node's metadata.
        res = supabase.table('graph_nodes') \
            .select('id, label, metadata, db_record_id') \
            .eq('type', 'person') \
            .eq('is_current', True) \
            .execute()
        people = []
        for n in res.data or []:
            meta = n.get('metadata') or {}
            if isinstance(meta, str):
                try:
                    meta = json.loads(meta)
                except Exception:
                    meta = {}
            enrich = meta.get('enrichment') or {}
            people.append({
                'id': n.get('id'),
                'name': n.get('label'),
                'strategic_weight': enrich.get('strategic_weight', 5),
                # Legacy bigint people id (mirror) — used by messages.linked_person_id
                'people_id': meta.get('people_id') or n.get('db_record_id'),
            })
        self.caches['people'].set(people)
        return people
        
    async def get_recently_completed_tasks(self, hours: int = 24):
        cached = self.caches['recent_tasks'].get()
        if cached is not None:
            return cached
            
        since_utc = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        res = supabase.table('tasks') \
            .select('title, organization_id, updated_at') \
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

        _uid = get_tenant()
        _range = f"{start_date.strftime('%Y-%m-%d')}:{end_date.strftime('%Y-%m-%d')}:{max_days}"
        cache_key = f"rhodey:cache:calendar_range:{_uid}:{_range}" if _uid else f"rhodey:cache:calendar_range:{_range}"
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
            entity_name: Optional entity to filter tasks by (e.g. "AcmeCorp").
                        When provided, only tasks related to this entity are returned.
        """
        from core.features import is_org_routing_enabled
        tasks, orgs = await asyncio.gather(
            self.get_active_tasks(),
            self.get_organizations() if is_org_routing_enabled() else asyncio.sleep(0, result=[]),
        )
        org_map = {o['id']: o['name'] for o in (orgs or [])}
        
        # Entity-aware task filtering — when a specific entity is resolved (e.g. "AcmeCorp"),
        # only show tasks that belong to that entity.
        if entity_name:
            entity_lower = entity_name.lower().strip()
            
            # Migration 76: for a PERSON entity, also match any of the person's
            # aliases (e.g. "Jane" in task titles when entity is "Jane Doe").
            person_terms = {entity_lower}
            try:
                from core.lib.graph_rules import _build_person_index
                for p in _build_person_index():
                    if p["label"].lower() == entity_lower:
                        person_terms.update(a.lower() for a in p["aliases"] if len(a) >= 3)
                        break
            except Exception:
                pass
            
            entity_org_ids = set()
            for oid, oname in org_map.items():
                if entity_lower in oname.lower():
                    entity_org_ids.add(oid)
            
            filtered = []
            for t in tasks:
                t_title = t.get('title', '').lower()
                t_org = t.get('organization_id')
                title_hit = any(term in t_title for term in person_terms if term)
                if title_hit or t_org in entity_org_ids:
                    filtered.append(t)
            
            tasks = filtered
        
        now = datetime.now(timezone.utc)
        now_iso = now.isoformat()
        tomorrow_iso = (now + timedelta(days=1)).isoformat()
        
        always_include = []
        semantic_pool = []
        
        for t in tasks:
            is_urgent = t.get('priority') == 'urgent'
            reminder = t.get('reminder_at') or t.get('deadline')
            
            # Check overdue or due today/tomorrow
            is_due_soon = False
            if reminder:
                # Pad date-only string for safe string comparison
                cmp_rem = f"{reminder}T00:00:00+00:00" if len(str(reminder)) == 10 else reminder
                if cmp_rem < now_iso:
                    is_due_soon = True # overdue
                elif cmp_rem < tomorrow_iso:
                    is_due_soon = True # due today
            
            if is_org_routing_enabled():
                o_id = t.get('organization_id')
                o_name = org_map.get(o_id, 'INBOX')
                loc = o_name
                formatted = f"[{loc}] {t.get('title')} ({t.get('priority')}) [ID:{t.get('id')}]"
            else:
                o_name = ""
                formatted = f"[INBOX] {t.get('title')} ({t.get('priority')}) [ID:{t.get('id')}]"
            
            # Horizon guard: skip non-urgent tasks with reminder_at/deadline > 2 days out
            if not is_urgent and not is_due_soon and reminder:
                try:
                    clean_reminder = str(reminder).replace(' ', 'T').replace('Z', '+00:00')
                    if len(clean_reminder) == 10:
                        clean_reminder += "T00:00:00+00:00"
                    reminder_dt = datetime.fromisoformat(clean_reminder)
                    if reminder_dt.tzinfo is None:
                        reminder_dt = reminder_dt.replace(tzinfo=timezone.utc)
                    if reminder_dt > now + timedelta(days=2):
                        continue
                except Exception:
                    pass  # Fail-open: include task if we can't parse reminder

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
            
        return compressed_tasks

    async def hydrate_memories_context(self, query_text: str, match_count: int = 5, return_raw: bool = False, recency_weight: float = 0.3, precomputed_embedding: list = None):
        """Uses pgvector to find semantically relevant memories, with recency weighting.
        
        Args:
            precomputed_embedding: Optional pre-computed embedding vector. If provided,
                saves ~500ms by avoiding a redundant Gemini API call. The internal memory
                pipeline (associative_retrieve) has its own Redis cache for the embedding,
                so this is mainly useful when the caller has already computed the embedding
                for other purposes (e.g. SharedQueryContext in interrogate_brain).
        """
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

    async def hydrate_persona_context(self) -> str:
        """L3 knowledge accessor for the tenant's persona card (M18c).

        THE single place a language generator reads the persona card.
        Generators must call this method — they must NEVER reach into
        core.services.persona directly (architectural rule: knowledge
        flows through the ContextProvider at Layer 3; presentation code
        at Layer 4 consumes the result but does not read knowledge
        itself — see session-notes/72-persona-l3-knowledge.md).

        Returns the persona block (who / voice / never / verified life
        circle) or ``""`` when there is no card — fail-closed: every
        prompt stays byte-identical pre-persona, and a tenant never
        inherits another tenant's card (the card read is tenant-scoped).
        """
        try:
            from core.services.persona import persona_voice_block, resolve_persona
            from core.services.user_settings import resolve_user_name

            # Read the card ONCE and pass it to persona_voice_block (its
            # signature accepts card=) — avoids a redundant second resolve.
            card = resolve_persona()
            block = persona_voice_block(user_name=resolve_user_name() or "", card=card)
            if not block:
                return ""
            # M18c: the verified life circle is KNOWLEDGE, not voice — add
            # it as a clause so generators can reason about the user's
            # world the way they reason about memories and people.
            life = [
                str(x) for x in ((card or {}).get("life_snapshot") or [])
                if str(x).strip()
            ]
            if life:
                block += " Life: " + ", ".join(life) + "."
            return block
        except Exception:
            return ""  # fail-closed: no card => block omitted

    def persona_signoffs_context(self) -> str:
        """L3 knowledge accessor: the tenant's persona card sign-offs (sync).

        Sync counterpart of :meth:`hydrate_persona_context` for prompt
        builders that cannot await (classify's receipt). THE single place
        a sync builder reads the card's sign-offs — never import
        resolve_persona at a prompt site (architectural rule, see
        session-notes/72-persona-l3-knowledge.md).

        Returns the card's sign-offs as a quoted list ("Rest well." /
        "Locked in for the night.") or ``""`` when there is no card —
        fail-closed: callers fall back to the fixed override row or the
        neutral default, never another tenant's card.
        """
        try:
            from core.services.persona import resolve_persona

            _persona = resolve_persona()
            _p_signoffs = (_persona or {}).get("signoffs") or []
            if _p_signoffs:
                return " / ".join(f'"{s}"' for s in _p_signoffs[:4])
            return ""
        except Exception:
            return ""  # fail-closed: no card => caller falls back

    async def get_pending_decisions_context(self):
        try:
            pending_lines = []
            rejected_lines = []
            
            seven_days_ago = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
            
            # Pending
            p_res = supabase.table('messages').select('id, channel, suggested_title, sender_name, has_memory_value').in_('channel', ['email', 'call', 'whatsapp']).is_('danny_decision', 'null').eq('direction', 'incoming').order('created_at', desc=True).limit(50).execute()
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

    async def get_cross_referenced_context(self, query_text: str, task_inputs: list, people: list, orgs: list = None, match_count: int = 5):
        """
        Runs hybrid pgvector search and graph edge search in parallel,
        and cross-references memories with graph connections.
        """
        from core.pulse.graph import fetch_hybrid_graph_context
        
        # 1. Fetch raw memories and graph context in parallel
        memories_task = self.hydrate_memories_context(query_text, match_count=match_count, return_raw=True, recency_weight=0.3)
        graph_task = fetch_hybrid_graph_context(people, orgs or [], task_inputs)
        
        memories, graph_context = await asyncio.gather(memories_task, graph_task)
        
        if not memories and not graph_context:
            return "None"
            
        # 2. Extract entity names from people
        entity_terms = set(p.get('name', '').lower() for p in people if p.get('name'))
        
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

    async def get_master_page_context(self, entity_names: list = None, match_count: int = 3) -> str:
        """
        Fetch canonical master pages for relevant entities.
        Uses ilike matching to find pages whose titles contain any entity name.
        Returns a formatted context string for the pulse briefing.
        """
        if not entity_names:
            return ""
        try:
            seen_titles = set()
            collected = []
            for name in entity_names[:10]:
                raw = name.strip()
                if not raw:
                    continue
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
