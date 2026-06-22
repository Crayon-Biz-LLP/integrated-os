import asyncio
import json
import os
import httpx
from datetime import datetime, timezone

from core.services.db import get_supabase
from core.llm.fallback import generate_content_with_fallback
from core.llm.config import WorkloadProfile
from core.llm.constants import CLASSIFICATION_MODEL
from core.llm.embedding import get_embedding

supabase = get_supabase()

PARENT_ORG_TAGS = {'SOLVSTRAT', 'QHORD', 'ASHRAYA', 'PERSONAL', 'CRAYON'}
SKIP_ORG_TAGS = {None, 'INBOX'}
MIN_FRAGMENT_THRESHOLD = 5

ORG_TAG_CONTEXT = {
    'SOLVSTRAT': 'Client services and delivery. Software development, consulting, client projects.',
    'QHORD': "Product GTM and launch. Qhord is Danny's standalone product launching June 2026.",
    'ASHRAYA': 'Ashraya church administration, operations, finances, events.',
    'PERSONAL': 'Family, home, health, personal admin, spiritual practices.',
    'CRAYON': 'Company governance, legal, tax, compliance, admin structure.',
}

entity_sem = asyncio.Semaphore(6)
gemini_sem = asyncio.Semaphore(4)

def parse_iso(ts_str):
    if not ts_str:
        return datetime.min.replace(tzinfo=timezone.utc)
    try:
        return datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
    except ValueError:
        return datetime.min.replace(tzinfo=timezone.utc)

def get_max_timestamp(records, date_fields):
    max_ts = datetime.min.replace(tzinfo=timezone.utc)
    for r in records:
        for field in date_fields:
            val = r.get(field)
            if val:
                ts = parse_iso(val)
                if ts > max_ts:
                    max_ts = ts
    return max_ts

def filter_fragments_by_project_strict(results, project_name):
    """Filter RPC results using AND logic (all words >2 chars must match)."""
    if not results:
        return []
    project_words = [w for w in project_name.lower().split() if len(w) > 2]
    if not project_words:
        return results
    filtered = []
    for r in results:
        meta = r.get('metadata') or {}
        entity = (meta.get('entity') or '').lower() if isinstance(meta, dict) else ''
        content = (r.get('content') or r.get('title') or r.get('message_text') or r.get('body_summary') or '').lower()
        if all(w in entity or w in content for w in project_words):
            filtered.append(r)
    return filtered

async def fetch_entity_graph_edges(entity_name: str, max_edges=20):
    """Fetches depth-1 relationships from graph_edges for the given entity."""
    try:
        entity_embedding_res = await get_embedding(entity_name)
        if not entity_embedding_res or not entity_embedding_res.vector:
            return []
            
        nodes = supabase.rpc('match_graph_nodes', {
            'query_embedding': entity_embedding_res.vector,
            'match_threshold': 0.7,
            'match_count': 1
        }).execute()
        
        if not nodes.data:
            return []
            
        node_id = nodes.data[0]['id']
        
        edges = supabase.table('graph_edges') \
            .select('source_node_id, target_node_id, relationship_type, metadata, created_at') \
            .or_(f"source_node_id.eq.{node_id},target_node_id.eq.{node_id}") \
            .order('created_at', desc=True) \
            .limit(max_edges) \
            .execute()
            
        return edges.data or []
    except Exception as e:
        print(f"Graph context error for {entity_name}: {e}")
        return []

async def synth_entity(project_id, entity_name, org_tag):
    async with entity_sem:
        print(f"  Gathering fragments for {entity_name}...")
        all_fragments = []
        seen_hashes = set()
        newest_timestamp = datetime.min.replace(tzinfo=timezone.utc)

        def add_fragment(prefix: str, text: str, ts: datetime):
            nonlocal newest_timestamp
            if ts and ts > newest_timestamp:
                newest_timestamp = ts
                
            normalized = text.strip().lower()
            h = hash(normalized)
            if h not in seen_hashes and normalized and not normalized.startswith("http"):
                seen_hashes.add(h)
                all_fragments.append(f"[{prefix}] {text}")

        entity_embedding_res = await get_embedding(entity_name)
        entity_embedding = entity_embedding_res.vector if entity_embedding_res else None

        # 1. Memories (via associative retrieve compat)
        try:
            from core.retrieval.search import search_memories_compat
            # Use associative=True to get 7-signal ranking including project boost
            mem = await search_memories_compat(
                query_text=entity_name,
                top_k=30,
                threshold=0.5,
                recency_weight=0.3,
                importance_weight=0.2,
                use_associative=True
            )
            if mem:
                # Still apply strict word filter as a safety net
                filtered_mem = filter_fragments_by_project_strict(mem, entity_name)
                for f in filtered_mem:
                    ts = parse_iso(f.get('created_at'))
                    add_fragment("MEMORY", f.get('content', ''), ts)
        except Exception as e:
            print(f"  [Error] Memories failed for {entity_name}: {e}")

        # 2. Tasks
        try:
            tasks_res = supabase.table('tasks').select('title, status, created_at, updated_at') \
                .eq('project_id', project_id).execute()
            if tasks_res.data:
                for t in tasks_res.data:
                    ts = parse_iso(t.get('updated_at') or t.get('created_at'))
                    add_fragment(f"TASK/{t['status'].upper()}", t['title'], ts)
        except Exception as e:
            print(f"  [Error] Tasks failed for {entity_name}: {e}")

        # 3. Resources (with project_id filter fallback)
        if entity_embedding:
            try:
                resources_res = supabase.rpc('match_resources', {
                    'query_embedding': entity_embedding,
                    'match_threshold': 0.5,
                    'match_count': 20
                }).execute()
                if resources_res.data:
                    # Filter by project_id if present, else fallback to strict word filter
                    for r in resources_res.data:
                        # Assuming the RPC doesn't return project_id, we just use strict word filter
                        # If we wanted to check project_id, we'd need another query or an updated RPC
                        filtered = filter_fragments_by_project_strict([r], entity_name)
                        if filtered:
                            ts = parse_iso(r.get('enriched_at') or r.get('created_at'))
                            add_fragment("RESOURCE", f"{r['title']} — {r.get('summary', '')}", ts)
            except Exception as e:
                print(f"  [Error] Resources failed for {entity_name}: {e}")

        # 4. Raw Dumps
        if entity_embedding:
            try:
                dumps_res = supabase.rpc('match_raw_dumps', {
                    'query_embedding': entity_embedding,
                    'match_threshold': 0.5,
                    'match_count': 30
                }).execute()
                if dumps_res.data:
                    filtered_dumps = filter_fragments_by_project_strict(dumps_res.data, entity_name)
                    for d in filtered_dumps:
                        ts = parse_iso(d.get('created_at'))
                        add_fragment("DUMP", d.get('content', ''), ts)
            except Exception as e:
                print(f"  [Error] Raw Dumps failed for {entity_name}: {e}")

        # 5. Emails
        if entity_embedding:
            try:
                emails_res = supabase.rpc('match_emails_hybrid', {
                    'query_embedding': entity_embedding,
                    'match_threshold': 0.5,
                    'match_count': 10
                }).execute()
                if emails_res.data:
                    filtered_emails = filter_fragments_by_project_strict(emails_res.data, entity_name)
                    for m in filtered_emails:
                        ts = parse_iso(m.get('received_at'))
                        add_fragment("EMAIL", f"{m.get('subject', '')} — {m.get('body_summary', '')}", ts)
            except Exception as e:
                print(f"  [Error] Emails failed for {entity_name}: {e}")

        # 6. WhatsApp
        if entity_embedding:
            try:
                wa_res = supabase.rpc('match_whatsapp_hybrid', {
                    'query_embedding': entity_embedding,
                    'match_threshold': 0.5,
                    'match_count': 10
                }).execute()
                if wa_res.data:
                    filtered_wa = filter_fragments_by_project_strict(wa_res.data, entity_name)
                    for m in filtered_wa:
                        ts = parse_iso(m.get('received_at'))
                        add_fragment("WHATSAPP", m.get('message_text', ''), ts)
            except Exception as e:
                print(f"  [Error] WhatsApp failed for {entity_name}: {e}")

        # 7. Graph Context (Edges)
        graph_context = []
        try:
            edges = await fetch_entity_graph_edges(entity_name, max_edges=20)
            for e in edges:
                ts = parse_iso(e.get('created_at'))
                if ts > newest_timestamp:
                    newest_timestamp = ts
                
                meta = e.get('metadata') or {}
                predicate = meta.get('predicate_text', '')
                if predicate:
                    rel_desc = f"{e.get('relationship_type')} ({predicate})"
                else:
                    rel_desc = e.get('relationship_type')
                graph_context.append(f"Edge: {rel_desc}")
        except Exception as e:
            print(f"  [Error] Graph edges failed for {entity_name}: {e}")

        # Parent/Child Tasks
        if org_tag in PARENT_ORG_TAGS:
            try:
                child_res = supabase.table('projects') \
                    .select('id, name') \
                    .eq('parent_project_id', project_id) \
                    .eq('status', 'active') \
                    .execute()
                for child in child_res.data or []:
                    child_tasks = supabase.table('tasks').select('title, status, created_at, updated_at') \
                        .eq('project_id', child['id']).execute()
                    for t in child_tasks.data or []:
                        ts = parse_iso(t.get('updated_at') or t.get('created_at'))
                        add_fragment(f"CHILD_TASK/{t['status'].upper()}", f"[{child['name']}] {t['title']}", ts)
            except Exception as e:
                print(f"  [Error] Child tasks failed for {entity_name}: {e}")

        is_parent = org_tag in PARENT_ORG_TAGS and entity_name.lower() == org_tag.lower()

        # Existing page
        try:
            existing = supabase.table('canonical_pages') \
                .select('id, content, last_synth_at') \
                .eq('project_id', project_id) \
                .eq('is_current', True) \
                .limit(1).execute()
            existing_content = existing.data[0]["content"] if existing.data else None
            existing_id = existing.data[0]["id"] if existing.data else None
            last_synth_at = parse_iso(existing.data[0]["last_synth_at"]) if existing.data and existing.data[0].get("last_synth_at") else datetime.min.replace(tzinfo=timezone.utc)
        except Exception as e:
            print(f"  [Error] Failed to fetch canonical page for {entity_name}: {e}")
            existing_content, existing_id, last_synth_at = None, None, datetime.min.replace(tzinfo=timezone.utc)

        # Incremental skip logic
        if existing_id and newest_timestamp <= last_synth_at and not is_parent:
            print(f"  Skipping {entity_name} — no new changes since last synthesis ({last_synth_at.isoformat()}).")
            return None

        if len(all_fragments) < MIN_FRAGMENT_THRESHOLD and not is_parent:
            print(f"  Skipping {entity_name} — below fragment threshold ({len(all_fragments)}).")
            # If it has an existing page but fell below threshold, it gets archived in the main loop
            return {"entity": entity_name, "existing_id": existing_id, "archive": True}

        print(f"  Ready to synthesize {entity_name} ({len(all_fragments)} fragments).")
        return {
            "entity": entity_name,
            "project_id": project_id,
            "org_tag": org_tag,
            "is_parent": is_parent,
            "existing_page": existing_content or "No existing page — create from scratch.",
            "new_fragments": all_fragments,
            "fragment_count": len(all_fragments),
            "existing_id": existing_id,
            "graph_context": graph_context
        }

async def run_batch_sweep_v2():
    try:
        active_res = supabase.table('projects') \
            .select('id, name, org_tag') \
            .eq('is_active', True) \
            .eq('status', 'active') \
            .execute()
            
        entities = []
        for p in active_res.data:
            org_tag = p.get('org_tag')
            if org_tag not in SKIP_ORG_TAGS:
                entities.append((p['id'], p.get('name') or p.get('title', ''), org_tag))

        print(f"Gathering fragments for {len(entities)} entities (Phase 2 Parallel)...")
        
        # Stage 1-3: Gather, filter, incremental check (Parallel)
        tasks = [synth_entity(pid, name, org) for pid, name, org in entities]
        gathered_payloads = await asyncio.gather(*tasks)
        
        # Filter out skips
        batch_payload = []
        for payload in gathered_payloads:
            if payload:
                if payload.get("archive"):
                    if payload.get("existing_id"):
                        # Archive pages that fell below threshold
                        supabase.table('canonical_pages') \
                            .update({"is_current": False}) \
                            .eq('id', payload['existing_id']) \
                            .execute()
                        print(f"Master Page Archived: {payload['entity']} — below threshold.")
                    else:
                        print(f"Skipping {payload['entity']} — below threshold, no existing page to archive.")
                else:
                    batch_payload.append(payload)

        if not batch_payload:
            print("No data found to synthesize (all skipped or up-to-date).")
            return

        # Notification
        telegram_chat_id = os.getenv("TELEGRAM_CHAT_ID")
        telegram_bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
        if telegram_chat_id and telegram_bot_token:
            try:
                url = f"https://api.telegram.org/bot{telegram_bot_token}/sendMessage"
                payload = {"chat_id": int(telegram_chat_id), "text": f"Synthesizing {len(batch_payload)} Master Pages (Phase 2)...", "parse_mode": "Markdown"}
                httpx.post(url, json=payload, timeout=10)
            except Exception:
                pass

        print(f"Synthesizing {len(batch_payload)} Master Pages via Gemini...")
        
        # Stage 4-5: Build prompt and call Gemini (Parallel with Semaphore 4)
        async def call_gemini_for_entity(entry):
            async with gemini_sem:
                entity_name = entry['entity']
                org_tag = entry.get('org_tag', '')
                is_parent = entry.get('is_parent', False)
                org_context = ORG_TAG_CONTEXT.get(org_tag, org_tag)
                
                if is_parent:
                    prompt_role = "Executive Summary Writer for Danny's OS"
                    prompt_objective = f"Write a high-level overview of the {org_tag} domain ({entity_name}). Synthesize all sub-projects and activity under this domain."
                    scope_rules = f"DOMAIN SCOPE: This page covers the {org_tag} domain and its sub-projects only.\nEXCLUDE: Any content related to other domains.\nDOMAIN DESCRIPTION: {org_context}"
                else:
                    prompt_role = "Knowledge Curator for Danny's OS"
                    prompt_objective = f"Update the Master Page for {entity_name} (under {org_tag})."
                    scope_rules = f"PROJECT SCOPE: This page is ONLY for {entity_name} under {org_tag}.\nEXCLUDE: Any content about other projects.\nDOMAIN CONTEXT: {entity_name} belongs to {org_tag} ({org_context})."
                
                graph_ctx_str = "\n".join(entry['graph_context']) if entry['graph_context'] else "No known relationships."
                
                per_prompt = f"""
ROLE: {prompt_role}
OBJECTIVE: {prompt_objective}

RULES:
- {scope_rules}
- CORRECT: If new fragments contradict the existing page, update with newer information.
- SPARSE GUARD: Output MUST be at least 300 characters. If fragments are thin, preserve the existing page as-is.
- FORMAT: Clean Markdown with headers and bullets.
- OUTPUT: Return ONLY the raw Markdown string. No JSON wrapper.

EXISTING PAGE:
{entry['existing_page']}

GRAPH CONTEXT (Related Entities):
{graph_ctx_str}

FRAGMENTS (Old & New):
{json.dumps(entry['new_fragments'], indent=2)}
"""
                try:
                    response = await generate_content_with_fallback(
                        prompt=per_prompt,
                        workload=WorkloadProfile.SYNTHESIS,
                        primary_model=CLASSIFICATION_MODEL,
                        config={'response_mime_type': 'text/plain'}
                    )
                    if response and response.text:
                        return entity_name, response.text.strip()
                    else:
                        print(f"No response for {entity_name}, skipping.")
                        return entity_name, None
                except Exception as e:
                    print(f"Gemini failed for {entity_name}: {e}")
                    return entity_name, None

        gemini_tasks = [call_gemini_for_entity(entry) for entry in batch_payload]
        gemini_results = await asyncio.gather(*gemini_tasks)
        
        results = {name: text for name, text in gemini_results if text}

        # Stage 6: Write results (Sequential)
        # Sequential writes ONLY — concurrent upserts to canonical_pages
        # with version increment would create race conditions.
        now_iso = datetime.now(timezone.utc).isoformat()
        
        for entity_name, markdown in results.items():
            payload_entry = next((p for p in batch_payload if p['entity'] == entity_name), None)
            if not payload_entry:
                continue

            project_id = payload_entry['project_id']
            existing_id = payload_entry.get('existing_id')
            
            if len(markdown) < 300:
                print(f"Skipping {entity_name} — output too sparse ({len(markdown)} chars)")
                continue

            embedding_res = await get_embedding(markdown)
            embedding = embedding_res.vector if embedding_res else None

            try:
                if existing_id:
                    version_res = supabase.table('canonical_pages') \
                        .select('version') \
                        .eq('id', existing_id) \
                        .single() \
                        .execute()
                    old_version = (version_res.data.get('version') or 0) if version_res.data else 0

                    supabase.table('canonical_pages') \
                        .update({
                            "content": markdown,
                            "embedding": embedding,
                            "version": old_version + 1,
                            "updated_at": now_iso,
                            "source_count": payload_entry['fragment_count'],
                            "last_synth_at": now_iso,
                            "is_sparse": len(markdown) < 500
                        }) \
                        .eq('id', existing_id) \
                        .execute()

                    print(f"Master Page Updated: {entity_name} (v{old_version + 1}, {payload_entry['fragment_count']} fragments)")
                else:
                    supabase.table('canonical_pages').insert({
                        "title": entity_name,
                        "project_id": project_id,
                        "content": markdown,
                        "embedding": embedding,
                        "version": 1,
                        "is_current": True,
                        "updated_at": now_iso,
                        "source_count": payload_entry['fragment_count'],
                        "last_synth_at": now_iso,
                        "is_sparse": len(markdown) < 500
                    }).execute()
                    print(f"Master Page Created: {entity_name} ({payload_entry['fragment_count']} fragments)")
            except Exception as e:
                print(f"DB commit failed for {entity_name}: {e}")
                continue

    except Exception as e:
        import traceback
        traceback.print_exc()
        print(f"Brain sweep v2 failed: {e}")
        telegram_chat_id = os.getenv("TELEGRAM_CHAT_ID")
        telegram_bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
        if telegram_chat_id and telegram_bot_token:
            try:
                url = f"https://api.telegram.org/bot{telegram_bot_token}/sendMessage"
                payload = {"chat_id": int(telegram_chat_id), "text": f"Brain Synthesizer v2 failed: {str(e)[:100]}", "parse_mode": "Markdown"}
                httpx.post(url, json=payload, timeout=10)
            except Exception:
                pass

if __name__ == "__main__":
    asyncio.run(run_batch_sweep_v2())
