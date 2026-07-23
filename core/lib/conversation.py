from core.services.db import get_supabase, maybe_single_safe
import uuid
import re
from datetime import datetime, timezone

from core.llm.constants import CLASSIFICATION_MODEL
from core.llm.compat import call_llm_with_fallback_sync

SESSION_TIMEOUT_MINUTES = 60
MAX_HISTORY_TOKENS = 5000

def _approx_tokens(text: str) -> int:
    """Approximate token count based on character length (~4 chars/token)."""
    return max(1, len(text) // 4)

def _touch_thread(thread_id: str):
    try:
        get_supabase().table('conversation_threads').update({'last_active_at': 'now()'}).eq('id', thread_id).execute()
    except Exception as e:
        from core.lib.audit_logger import audit_log_sync
        audit_log_sync("conversation", "WARNING", f"Failed to touch thread {thread_id}: {e}")

def _check_topic_overlap(text: str, payload: dict) -> bool:
    """Deterministic topical relevance check between message and workflow payload.

    Uses n-gram entity resolver for orgs/projects + substring match for people.
    Payload text enriched with canonical names from entity ID fields.
    Logs structured metadata for routing auditability.

    Returns True if:
    - Message has no known entities (filler like 'yes'/'ok')
    - Any detected entity name overlaps with payload content

    Returns False if message references known entities absent from payload.
    """
    if not text or not payload:
        return True

    from core.lib.audit_logger import audit_log_sync
    supabase = get_supabase()
    entity_names = set()
    resolver_reason = ""

    try:
        from core.pulse.entity_resolver import resolve_entities_from_text
        org_id, proj_id, resolver_reason = resolve_entities_from_text(text)
        if org_id:
            r = supabase.table('organizations').select('name').eq('id', org_id).execute()
            if r.data:
                entity_names.add(r.data[0]['name'].lower())
        if proj_id:
            r = supabase.table('projects').select('name').eq('id', proj_id).execute()
            if r.data:
                entity_names.add(r.data[0]['name'].lower())
    except Exception:
        resolver_reason = "resolver_error"

    try:
        people = supabase.table('people').select('name').eq('is_current', True).execute()
        text_lower = text.lower()
        for p in (people.data or []):
            name = p['name'].strip().lower()
            if name and len(name) >= 3 and name in text_lower:
                entity_names.add(name)
    except Exception:
        pass

    # Build enriched payload text: raw values + canonical names from ID fields
    payload_text_parts = [str(v) for v in payload.values()]
    for id_field, table in [('project_id', 'projects'), ('organization_id', 'organizations')]:
        val = payload.get(id_field)
        if val and isinstance(val, str) and len(val) == 36:
            try:
                r = supabase.table(table).select('name').eq('id', val).execute()
                if r.data:
                    payload_text_parts.append(r.data[0]['name'])
            except Exception:
                pass
    payload_text = ' '.join(payload_text_parts).lower()

    if not entity_names:
        audit_log_sync("routing", "INFO",
            "topic_overlap: verdict=pass reason=no_entities_in_text"
            f" payload_keys={list(payload.keys())}")
        return True

    verdict = any(entity in payload_text for entity in entity_names)

    audit_log_sync("routing", "INFO",
        f"topic_overlap: verdict={'pass' if verdict else 'bypass'}"
        f" entities={sorted(entity_names)}"
        f" resolver={resolver_reason}"
        f" payload_keys={list(payload.keys())}")

    return verdict


def _entity_is_primary_topic(text: str, entity_name: str) -> bool:
    """K2: Check if entity_name is the primary subject of text, not a side mention.
    
    Heuristic: if entity name is a large proportion of the content, or appears
    at the start of the message, it's likely the primary topic.
    A side mention ('talked to X about Y') should not reroute the thread.
    """
    if not text or not entity_name:
        return False
    norm_text = text.lower().strip()
    norm_entity = entity_name.lower().strip()
    
    # Direct match: text IS the entity or starts with it
    if norm_text == norm_entity or norm_text.startswith(norm_entity + " "):
        return True
    
    # Entity name appears as a large proportion of text (>25% of words)
    text_words = set(norm_text.split())
    entity_words = norm_entity.split()
    if entity_words and len(text_words) > 1:
        overlap = sum(1 for w in entity_words if w in text_words)
        if overlap / len(text_words) >= 0.25:
            return True
    
    # Check for common side-mention patterns
    side_patterns = [
        r'\b(?:talked to|spoke with|met with|had lunch with|from|at|works at)\s+' + re.escape(norm_entity),
        r'\b' + re.escape(norm_entity) + r'\s+(?:is|was|said|mentioned|confirmed)',
    ]
    for pat in side_patterns:
        if re.search(pat, norm_text):
            return False  # It's a side mention
    
    # If entity appears multiple times, it's likely the topic
    return norm_text.count(norm_entity) >= 2


def _fetch_entity_candidates(text: str, chat_id: int) -> list:
    """K2: Fetch all entity candidate threads from text, ranked by recency + confidence.
    
    Returns list of dicts with thread_id, active_anchor, entity_name, score.
    Uses deterministic resolver first (orgs, projects, people), then LLM fallback.
    """
    candidates = []
    
    # 1. Try deterministic n-gram resolver (orgs + projects)
    try:
        from core.pulse.entity_resolver import resolve_entities_from_text
        org_id, proj_id, reason = resolve_entities_from_text(text)
        
        candidates.extend(_resolve_entity_to_candidates(chat_id, 'organization', org_id, "deterministic", text))
        candidates.extend(_resolve_entity_to_candidates(chat_id, 'project', proj_id, "deterministic", text))
    except Exception:
        pass
    
    # 2. Person entity matching (NEW — Fix B): detect people via graph_nodes
    try:
        person_candidates = _resolve_person_candidates(text, chat_id)
        candidates.extend(person_candidates)
    except Exception:
        pass
    
    # 3. LLM fallback — if no candidates, check if text references a known entity
    if not candidates:
        try:
            llm_candidates = _llm_entity_disambiguation(text, chat_id)
            if llm_candidates:
                candidates.extend(llm_candidates)
                from core.lib.audit_logger import audit_log_sync
                audit_log_sync("routing", "INFO", f"LLM entity disambiguation found {len(llm_candidates)} candidate(s)")
        except Exception:
            pass
    
    # 4. Sort by score descending
    candidates.sort(key=lambda c: c.get('score', 0), reverse=True)
    return candidates


def _resolve_person_candidates(text: str, chat_id: int) -> list:
    """Detect person entities in text and create/lookup person-scoped threads.
    
    Uses n-gram matching against graph_nodes with type='person'.
    Follows the same pattern as _resolve_entity_to_candidates for orgs/projects.
    Returns list of candidate dicts.
    """
    if not text:
        return []
    
    supabase = get_supabase()
    results = []
    
    try:
        # Fetch known people from graph_nodes
        people_res = supabase.table('graph_nodes') \
            .select('id, label') \
            .eq('type', 'person') \
            .eq('is_current', True) \
            .execute()
        people = people_res.data or []
        
        norm_text = text.lower().strip()
        
        for person in people:
            label = person.get('label', '')
            if not label:
                continue
            norm_label = label.lower().strip()
            
            # Check if person name is the primary topic of the text
            if not _entity_is_primary_topic(norm_text, norm_label):
                continue
            
            person_id = str(person['id'])
            
            # Check if a person-scoped thread already exists
            thread = supabase.table('conversation_threads') \
                .select('id, active_anchor, last_active_at') \
                .eq('chat_id', chat_id) \
                .eq('thread_type', 'entity') \
                .eq('entity_type', 'person') \
                .eq('entity_id', person_id) \
                .is_('archived_at', 'null') \
                .order('last_active_at', desc=True) \
                .limit(1) \
                .execute()
            
            base_score = 75  # Slightly below project (90) and org (80) to prefer structured entities
            
            if thread.data and thread.data[0].get('id'):
                t = thread.data[0]
                last_active = t.get('last_active_at')
                boost = 0
                if last_active:
                    try:
                        hours_ago = (datetime.now(timezone.utc) - datetime.fromisoformat(last_active.replace('Z', '+00:00'))).total_seconds() / 3600
                        if hours_ago <= 24:
                            boost = 10
                        elif hours_ago <= 72:
                            boost = 5
                    except Exception:
                        pass
                results.append({
                    'thread_id': t['id'],
                    'active_anchor': t.get('active_anchor'),
                    'entity_name': label,
                    'entity_type': 'person',
                    'entity_id': person_id,
                    'score': base_score + boost,
                    'source': 'person_match',
                    'is_new': False
                })
            else:
                results.append({
                    'thread_id': None,
                    'active_anchor': None,
                    'entity_name': label,
                    'entity_type': 'person',
                    'entity_id': person_id,
                    'score': base_score - 20,
                    'source': 'person_match',
                    'is_new': True
                })
            # Only return the best person match (no ambiguous person routing)
            break
    except Exception as e:
        from core.lib.audit_logger import audit_log_sync
        audit_log_sync("routing", "WARNING", f"Person candidate resolution failed: {e}")
    
    return results


def _resolve_entity_to_candidates(chat_id: int, entity_type: str, entity_id, source: str, text: str) -> list:
    """K2: Convert a single entity match to ranked candidates with thread info."""
    if not entity_id:
        return []
    
    supabase = get_supabase()
    results = []
    e_id = str(entity_id)
    
    # Fetch entity name for primary-topic check
    entity_name = ""
    if entity_type == 'organization':
        r = maybe_single_safe(supabase.table('organizations').select('name').eq('id', entity_id))
        if r.data:
            entity_name = r.data.get('name', '')
    elif entity_type == 'project':
        r = maybe_single_safe(supabase.table('projects').select('name').eq('id', entity_id))
        if r.data:
            entity_name = r.data.get('name', '')
    
    # Primary topic check — filter out side mentions
    if entity_name and text and not _entity_is_primary_topic(text, entity_name):
        return []
    
    # Check for existing thread
    thread = supabase.table('conversation_threads') \
        .select('id, active_anchor, last_active_at') \
        .eq('chat_id', chat_id) \
        .eq('thread_type', 'entity') \
        .eq('entity_type', entity_type) \
        .eq('entity_id', e_id) \
        .is_('archived_at', 'null') \
        .order('last_active_at', desc=True) \
        .limit(1) \
        .execute()
    
    base_score = 90 if entity_type == 'project' else 80
    
    if thread.data and thread.data[0].get('id'):
        t = thread.data[0]
        last_active = t.get('last_active_at')
        boost = 0
        if last_active:
            try:
                hours_ago = (datetime.now(timezone.utc) - datetime.fromisoformat(last_active.replace('Z', '+00:00'))).total_seconds() / 3600
                if hours_ago <= 24:
                    boost = 10
                elif hours_ago <= 72:
                    boost = 5
            except Exception:
                pass
        results.append({
            'thread_id': t['id'],
            'active_anchor': t.get('active_anchor'),
            'entity_name': entity_name,
            'entity_type': entity_type,
            'entity_id': e_id,
            'score': base_score + boost,
            'source': source,
            'is_new': False
        })
    else:
        results.append({
            'thread_id': None,
            'active_anchor': None,
            'entity_name': entity_name,
            'entity_type': entity_type,
            'entity_id': e_id,
            'score': base_score - 20,
            'source': source,
            'is_new': True
        })
    
    return results


def _llm_entity_disambiguation(text: str, chat_id: int) -> list:
    """K2: LLM fallback when n-gram resolver finds nothing.
    
    Uses Gemini to check if text references a known entity.
    Returns list of candidate dicts (same format as _resolve_entity_to_candidates).
    """
    supabase = get_supabase()
    
    # Fetch known org and project names
    orgs = supabase.table('organizations').select('id, name').execute().data or []
    projs = supabase.table('projects').select('id, name').eq('status', 'active').eq('is_current', True).execute().data or []
    
    if not orgs and not projs:
        return []
    
    known_orgs = [o['name'] for o in orgs]
    known_projs = [p['name'] for p in projs]
    
    prompt = f"""Given this message: "{text}"

Known organizations: {', '.join(known_orgs) if known_orgs else 'none'}
Known projects: {', '.join(known_projs) if known_projs else 'none'}

Does the message refer to any of these entities as its PRIMARY topic?
If yes, respond with: ORGANIZATION|project_name or PROJECT|project_name
If the entity is only a side mention (e.g., "talked to X from Y"), respond with: NONE
If no entity matches, respond with: NONE

Response (one line only):"""
    
    resp = call_llm_with_fallback_sync(prompt, model=CLASSIFICATION_MODEL, is_critical=False)
    result = resp.text.strip().upper() if resp and resp.text else ""
    
    candidates = []
    
    if result.startswith("ORGANIZATION|"):
        name = result.split("|", 1)[1].strip()
        for o in orgs:
            if o['name'].lower() == name.lower():
                thread = supabase.table('conversation_threads') \
                    .select('id, active_anchor') \
                    .eq('chat_id', chat_id) \
                    .eq('thread_type', 'entity') \
                    .eq('entity_type', 'organization') \
                    .eq('entity_id', str(o['id'])) \
                    .is_('archived_at', 'null') \
                    .order('last_active_at', desc=True) \
                    .limit(1) \
                    .execute()
                if thread.data:
                    candidates.append({
                        'thread_id': thread.data[0]['id'],
                        'active_anchor': thread.data[0].get('active_anchor'),
                        'entity_name': o['name'],
                        'entity_type': 'organization',
                        'entity_id': str(o['id']),
                        'score': 85,
                        'source': 'llm',
                        'is_new': False
                    })
                else:
                    candidates.append({
                        'thread_id': None,
                        'active_anchor': None,
                        'entity_name': o['name'],
                        'entity_type': 'organization',
                        'entity_id': str(o['id']),
                        'score': 65,
                        'source': 'llm',
                        'is_new': True
                    })
                break
                
    elif result.startswith("PROJECT|"):
        name = result.split("|", 1)[1].strip()
        for p in projs:
            if p['name'].lower() == name.lower():
                thread = supabase.table('conversation_threads') \
                    .select('id, active_anchor') \
                    .eq('chat_id', chat_id) \
                    .eq('thread_type', 'entity') \
                    .eq('entity_type', 'project') \
                    .eq('entity_id', str(p['id'])) \
                    .is_('archived_at', 'null') \
                    .order('last_active_at', desc=True) \
                    .limit(1) \
                    .execute()
                if thread.data:
                    candidates.append({
                        'thread_id': thread.data[0]['id'],
                        'active_anchor': thread.data[0].get('active_anchor'),
                        'entity_name': p['name'],
                        'entity_type': 'project',
                        'entity_id': str(p['id']),
                        'score': 90,
                        'source': 'llm',
                        'is_new': False
                    })
                else:
                    candidates.append({
                        'thread_id': None,
                        'active_anchor': None,
                        'entity_name': p['name'],
                        'entity_type': 'project',
                        'entity_id': str(p['id']),
                        'score': 70,
                        'source': 'llm',
                        'is_new': True
                    })
                break
    
    return candidates


def resolve_thread(chat_id: int, text: str = None) -> tuple:
    """Returns (thread_id, active_anchor)
    
    Routing priority (K2):
    1. Open workflow (1 active workflow → that thread)
    2. Entity match with disambiguation (best candidate by recency + confidence)
    3. Prior bot question (last thread where bot asked a question)
    4. General fallback (create or reuse general thread)

    K2 FALLBACK CONTRACT (see core/FALLBACK_CONTRACTS.md):
    - Inner catch (entity resolution failure): logged WARNING, falls through to
      priority 4 (general fallback). No user-visible artifact.
    - Outer catch (any routing failure): logged ERROR, returns brand-new UUID
      with no anchor. Creates NO conversation_history row, sends NO receipt,
      generates NO user-visible message. The caller receives empty history
      [] and proceeds normally — any receipt emitted is from classification
      (C3 fallback contract), not from thread routing.
    """
    try:
        supabase = get_supabase()
        
        # 1. Open workflow bound to chat_id
        workflows = supabase.table('conversation_workflows').select('thread_id, payload').eq('chat_id', chat_id).eq('status', 'active').execute()
        if workflows.data and len(workflows.data) == 1:
            thread_id = workflows.data[0]['thread_id']
            if text:
                payload = workflows.data[0].get('payload') or {}
                if not _check_topic_overlap(text, payload):
                    from core.lib.audit_logger import audit_log_sync
                    audit_log_sync("routing", "INFO",
                        "Message topic differs from active workflow — falling through to normal routing")
                else:
                    _touch_thread(thread_id)
                    t_res = supabase.table('conversation_threads').select('active_anchor').eq('id', thread_id).execute()
                    anchor = t_res.data[0].get('active_anchor') if t_res.data else None
                    from core.lib.audit_logger import audit_log_sync
                    audit_log_sync("routing", "INFO", f"Routed to thread {thread_id} via workflow_resume")
                    return thread_id, anchor
            else:
                _touch_thread(thread_id)
                t_res = supabase.table('conversation_threads').select('active_anchor').eq('id', thread_id).execute()
                anchor = t_res.data[0].get('active_anchor') if t_res.data else None
                from core.lib.audit_logger import audit_log_sync
                audit_log_sync("routing", "INFO", f"Routed to thread {thread_id} via workflow_resume")
                return thread_id, anchor

        # 2. Entity match with disambiguation (K2)
        if text:
            try:
                candidates = _fetch_entity_candidates(text, chat_id)
                if candidates:
                    best = candidates[0]
                    if best.get('thread_id'):
                        _touch_thread(best['thread_id'])
                        from core.lib.audit_logger import audit_log_sync
                        audit_log_sync("routing", "INFO",
                            f"Routed to thread {best['thread_id']} via entity_disambiguated "
                            f"(entity={best.get('entity_name','')}, score={best.get('score',0)}, source={best.get('source','')})")
                        return best['thread_id'], best.get('active_anchor')
                    elif best.get('is_new'):
                        # Create new entity thread
                        new_thread = supabase.table('conversation_threads').insert({
                            'chat_id': chat_id,
                            'thread_type': 'entity',
                            'entity_type': best.get('entity_type'),
                            'entity_id': best.get('entity_id'),
                            'entity_label': best.get('entity_name', ''),
                            'routing_confidence': best.get('source', '')
                        }).execute()
                        from core.lib.audit_logger import audit_log_sync
                        audit_log_sync("routing", "INFO",
                            f"Routed to new thread {new_thread.data[0]['id']} via entity_disambiguated (new, {best.get('entity_name','')})")
                        return new_thread.data[0]['id'], None
            except Exception as inner_e:
                from core.lib.audit_logger import audit_log_sync
                audit_log_sync("routing", "WARNING", f"Entity resolution failed in thread routing: {inner_e}")

        # 4. Last active non-archived thread (if previous bot turn ended with question)
        last_thread = supabase.table('conversation_threads') \
            .select('id, active_anchor') \
            .eq('chat_id', chat_id) \
            .is_('archived_at', 'null') \
            .order('last_active_at', desc=True) \
            .limit(1) \
            .execute()
            
        if last_thread.data:
            thread_id = last_thread.data[0]['id']
            last_msg = supabase.table('conversations') \
                .select('role, content') \
                .eq('thread_id', thread_id) \
                .order('created_at', desc=True) \
                .limit(1) \
                .execute()
                
            if last_msg.data and last_msg.data[0]['role'] == 'bot':
                content = last_msg.data[0]['content']
                if content.strip().endswith('?') or 'clarification' in content.lower() or 'ready to add that to your calendar' in content.lower():
                    _touch_thread(thread_id)
                    from core.lib.audit_logger import audit_log_sync
                    audit_log_sync("routing", "INFO", f"Routed to thread {thread_id} via prior_bot_question")
                    return thread_id, last_thread.data[0].get('active_anchor')

        # 5. Else general
        general = supabase.table('conversation_threads') \
            .select('id, active_anchor') \
            .eq('chat_id', chat_id) \
            .eq('thread_type', 'general') \
            .is_('archived_at', 'null') \
            .execute()
            
        if general.data:
            thread_id = general.data[0]['id']
            _touch_thread(thread_id)
            from core.lib.audit_logger import audit_log_sync
            audit_log_sync("routing", "INFO", f"Routed to thread {thread_id} via fallback_general (existing)")
            return thread_id, general.data[0].get('active_anchor')
        else:
            new_thread = supabase.table('conversation_threads').insert({
                'chat_id': chat_id,
                'thread_type': 'general'
            }).execute()
            from core.lib.audit_logger import audit_log_sync
            audit_log_sync("routing", "INFO", f"Routed to new thread {new_thread.data[0]['id']} via fallback_general (new)")
            return new_thread.data[0]['id'], None

    except Exception as e:
        from core.lib.audit_logger import audit_log_sync
        audit_log_sync("routing", "ERROR", f"Thread routing failed, falling back to new session: {e}")
        return str(uuid.uuid4()), None

def get_or_create_session(chat_id: int, message_text: str = None) -> tuple:
    """
    Legacy wrapper: Returns (session_id, history_pairs, active_anchor)
    Behind the scenes, resolves a thread and uses thread_id as session_id.
    """
    thread_id, active_anchor = resolve_thread(chat_id, message_text)
    return thread_id, get_history(thread_id), active_anchor

def _compress_to_summary(pairs: list) -> str:
    """K1: Abstractive summary of overflow conversation pairs via LLM.

    Generates 2-3 sentence summary capturing topic, decisions, key outcomes.
    Falls back to extractive concatenation if LLM unavailable (fail-open).
    """
    parts = []
    for p in pairs:
        user = p.get('user')
        bot = p.get('bot')
        user_content = (user or {}).get('content', '').strip()
        bot_content = (bot or {}).get('content', '').strip() if bot else ''
        if user_content:
            parts.append(f"User: {user_content[:200]}")
        if bot_content:
            parts.append(f"Rhodey: {bot_content[:200]}")
    if not parts:
        return ""

    raw = "\n".join(parts)
    if len(raw) > 3000:
        raw = raw[:3000] + "..."

    try:
        prompt = f"""Summarize this conversation thread in 2-3 sentences.
Capture: the topic, any decisions made, and key outcomes.
Be concise — this will be used as memory context for future replies.

Conversation:
{raw}

Summary:"""
        resp = call_llm_with_fallback_sync(prompt, model=CLASSIFICATION_MODEL, is_critical=False)
        summary = resp.text.strip()
        if summary and len(summary) < 600:
            return summary
    except Exception:
        pass

    # Fail-open: fall back to extractive concatenation
    condensed = " · ".join(p[:150] for p in parts)
    return condensed[:800]

def _store_thread_summary(session_id: str, summary: str):
    """Persist thread summary to conversation_threads row."""
    try:
        get_supabase().table('conversation_threads').update({'summary': summary}).eq('id', session_id).execute()
    except Exception:
        pass

def _store_thread_summary_if_missing(session_id: str, summary: str):
    """Persist thread summary idempotently, only if it doesn't already exist."""
    try:
        get_supabase().table('conversation_threads') \
            .update({'summary': summary}) \
            .eq('id', session_id) \
            .is_('summary', 'null') \
            .execute()
    except Exception:
        pass

def _compress_to_classify_summary(pairs: list) -> str:
    """K1: Generate a topic-level summary specifically for classification context.
    
    Captures what was discussed, explicitly avoiding specific actions or receipts
    to prevent context leakage biasing future classifications.
    """
    parts = []
    for p in pairs:
        user = p.get('user')
        bot = p.get('bot')
        user_content = (user or {}).get('content', '').strip()
        bot_content = (bot or {}).get('content', '').strip() if bot else ''
        if user_content:
            parts.append(f"User: {user_content[:200]}")
        if bot_content:
            parts.append(f"Rhodey: {bot_content[:200]}")
    if not parts:
        return ""

    raw = "\n".join(parts)
    if len(raw) > 3000:
        raw = raw[:3000] + "..."

    try:
        prompt = f"""Summarize the overarching topic of this conversation in 1-2 sentences.
Focus strictly on WHAT is being discussed (the subject matter).
Do NOT include specific actions taken, receipts, bot responses, or outcomes.

Conversation:
{raw}

Topic Summary:"""
        resp = call_llm_with_fallback_sync(prompt, model=CLASSIFICATION_MODEL, is_critical=False)
        summary = resp.text.strip()
        if summary and len(summary) < 600:
            return summary
    except Exception:
        pass
    return ""

async def _background_summary_check(session_id: str):
    """Best-effort background job to generate/update thread summary.
    
    Fires eagerly every 3 user exchanges so short threads always have
    a summary for the awareness layer to scan. Always updates the summary
    so it doesn't go stale as the conversation evolves.
    """
    try:
        conv_res = get_supabase().table('conversations').select('id').eq('thread_id', session_id).eq('role', 'user').execute()
        user_count = len(conv_res.data or [])
        if user_count < 2 or user_count % 3 != 0:
            return  # Generate every 3rd user exchange (2, 5, 8, 11...)
            
        all_pairs = get_history(session_id, max_tokens=8000)
        summary = _compress_to_classify_summary(all_pairs)
        if summary:
            # Always update the summary (not if_missing) so it stays fresh
            _store_thread_summary(session_id, summary)
    except Exception as e:
        from core.lib.audit_logger import audit_log_sync
        audit_log_sync("conversation", "WARNING", f"Background summary generation failed: {e}")

def get_history(session_id: str, max_tokens: int = MAX_HISTORY_TOKENS) -> list:
    """
    Get conversation history for a thread (session_id = thread_id), truncated by token budget.
    Builds user+bot pairs, drops oldest pairs from front until within max_tokens.
    On first overflow, compresses dropped pairs into a thread summary and stores it.
    """
    res = get_supabase().table('conversations') \
        .select('role, intent, content, token_count') \
        .eq('thread_id', session_id) \
        .order('created_at') \
        .execute()
        
    rows = res.data or []
    # Fallback to session_id if no rows found (for old conversations before migration)
    if not rows:
        res = get_supabase().table('conversations') \
            .select('role, intent, content, token_count') \
            .eq('session_id', session_id) \
            .order('created_at') \
            .execute()
        rows = res.data or []

    if not rows:
        return []

    pairs = []
    i = 0
    while i < len(rows):
        user_msg = rows[i] if rows[i]['role'] == 'user' else None
        bot_msg = None
        if i + 1 < len(rows) and rows[i + 1]['role'] == 'bot':
            bot_msg = rows[i + 1]
            i += 2
        else:
            i += 1
        pairs.append({'user': user_msg, 'bot': bot_msg})

    if not pairs:
        return []

    total = sum(
        (p.get('user') or {}).get('token_count', 0) +
        (p.get('bot') or {}).get('token_count', 0)
        for p in pairs
    )

    if total <= max_tokens:
        return pairs

    # Overflow: capture dropped pairs before popping
    overflow = []
    while total > max_tokens and len(pairs) > 1:
        removed = pairs.pop(0)
        total -= (
            (removed.get('user') or {}).get('token_count', 0) +
            (removed.get('bot') or {}).get('token_count', 0)
        )
        overflow.append(removed)

    # Lazy summarization: store overflow summary only if thread has none yet
    if overflow:
        try:
            t_res = get_supabase().table('conversation_threads') \
                .select('summary').eq('id', session_id).execute()
            if t_res.data and not t_res.data[0].get('summary'):
                summary = _compress_to_summary(overflow)
                if summary:
                    _store_thread_summary(session_id, summary)
        except Exception:
            pass

    return pairs

def get_thread_summary(thread_id: str) -> str:
    """Retrieve stored compressed summary for a thread."""
    try:
        t_res = get_supabase().table('conversation_threads') \
            .select('summary').eq('id', thread_id).execute()
        if t_res.data and t_res.data[0].get('summary'):
            return t_res.data[0]['summary']
    except Exception:
        pass
    return ""

async def _store_exchange_embedding(exchange_id: int, content: str):
    """Async fire-and-forget task to store embedding on a user exchange.
    
    This is Fix C: stores embeddings on ALL user exchanges (not just QUERY),
    so the match_conversations RPC (Phase 1) can find TASK, NOTE, COMPLETION
    exchanges too.
    """
    try:
        from core.llm import get_embedding
        emb = await get_embedding(content)
        if emb and emb.vector:
            get_supabase().table('conversations') \
                .update({'embedding': emb.vector}) \
                .eq('id', exchange_id) \
                .execute()
    except Exception as e:
        from core.lib.audit_logger import audit_log_sync
        audit_log_sync("conversation", "WARNING", f"Failed to store exchange embedding: {e}")


def log_exchange(session_id: str, role: str, intent: str, content: str, chat_id: int, metadata: dict = None):
    """Insert an exchange row into conversations. Maps session_id to thread_id."""
    try:
        record = {
            "session_id": session_id,
            "thread_id": session_id, # We use the same UUID for both to maintain legacy compatibility
            "role": role,
            "intent": intent,
            "content": content,
            "chat_id": chat_id,
            "token_count": _approx_tokens(content),
            "metadata": metadata or {}
        }
        insert_res = get_supabase().table('conversations').insert(record).execute()
        _touch_thread(session_id)
        
        # Store embedding for user exchanges (Fix C — fire-and-forget)
        if role == 'user' and insert_res.data:
            import asyncio
            try:
                exchange_id = insert_res.data[0].get('id')
                if exchange_id:
                    loop = asyncio.get_running_loop()
                    loop.create_task(_store_exchange_embedding(exchange_id, content))
            except RuntimeError:
                pass  # No running event loop
        
        if role == 'bot':
            import asyncio
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(_background_summary_check(session_id))
            except RuntimeError:
                pass  # No running event loop
    except Exception as e:
        from core.lib.audit_logger import audit_log_sync
        audit_log_sync("conversation", "ERROR", f"log_exchange error: {e}")

def format_classify_context(pairs: list, thread_summary: str = "", active_anchor: dict = None) -> str:
    """Format a bounded context block specifically for classification.
    
    Replaces raw conversation history to prevent bot receipt leakage.
    Uses abstractive thread summary + last user message only.
    """
    parts = []
    
    if thread_summary:
        parts.append(f"THREAD SUMMARY: {thread_summary[:500]}")
    
    if active_anchor and active_anchor.get('name') and active_anchor.get('type'):
        parts.append(f"ACTIVE ENTITY: {active_anchor['name']} ({active_anchor['type']})")
        
    if pairs:
        last = pairs[-1]
        user = last.get('user')
        if user and user.get('content'):
            parts.append("PRECEDING TURN:")
            parts.append(f"User: {user['content'][:500]}")
            
    if parts:
        return "CONVERSATION HISTORY:\n" + "\n".join(parts)
    return ""

def format_history_for_prompt(pairs: list) -> str:
    """Format conversation history as a CONVERSATION HISTORY block for LLM prompts."""
    if not pairs:
        return ""
    lines = ["CONVERSATION HISTORY:"]
    for pair in pairs:
        user = pair.get('user')
        bot = pair.get('bot')
        if user:
            lines.append(f'User: {user.get("content", "")}')
        if bot:
            lines.append(f'Rhodey: {bot.get("content", "")}')
    return "\n".join(lines)
