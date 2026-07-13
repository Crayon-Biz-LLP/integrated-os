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

MIN_FRAGMENT_THRESHOLD = 5

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
            
        nodes = await asyncio.to_thread(
            lambda: supabase.rpc('match_graph_nodes', {
                'query_embedding': entity_embedding_res.vector,
                'match_threshold': 0.7,
                'match_count': 1
            }).execute()
        )
        
        if not nodes.data:
            return []
            
        node_id = nodes.data[0]['id']
        
        edges = await asyncio.to_thread(
            lambda: supabase.table('graph_edges') \
            .select('source_node_id, target_node_id, relationship_type, metadata, created_at') \
            .or_(f"source_node_id.eq.{node_id},target_node_id.eq.{node_id}") \
            .order('created_at', desc=True) \
            .limit(max_edges) \
            .execute()
        )
            
        if not edges.data:
            return []
            
        node_ids = set()
        for e in edges.data:
            node_ids.add(e['source_node_id'])
            node_ids.add(e['target_node_id'])
            
        nodes_res = await asyncio.to_thread(
            lambda: supabase.table('graph_nodes') \
            .select('id, label') \
            .in_('id', list(node_ids)) \
            .execute()
        )
            
        id_to_label = {n['id']: n.get('label', 'Unknown') for n in (nodes_res.data or [])}
        
        resolved_edges = []
        for e in edges.data:
            src_label = id_to_label.get(e['source_node_id'], 'Unknown')
            tgt_label = id_to_label.get(e['target_node_id'], 'Unknown')
            predicate = (e.get('metadata') or {}).get('predicate_text', '')
            rel = predicate if predicate else e.get('relationship_type', '')
            resolved_edges.append({
                "description": f"{src_label} → {rel} → {tgt_label}",
                "created_at": e.get('created_at')
            })
            
        return resolved_edges
    except Exception as e:
        print(f"Graph context error for {entity_name}: {e}")
        return []

async def synth_entity(entity_id, entity_name, org_name, org_context, is_org=False, project_ids=None):
    async with entity_sem:
        print(f"  Gathering fragments for {entity_name}...")
        
        # Fetch existing page first to get last_synth_at
        try:
            if is_org:
                existing = await asyncio.to_thread(
                    lambda: supabase.table('canonical_pages') \
                    .select('id, content, last_synth_at') \
                    .eq('organization_id', entity_id) \
                    .eq('is_current', True) \
                    .limit(1).execute()
                )
            else:
                existing = await asyncio.to_thread(
                    lambda: supabase.table('canonical_pages') \
                    .select('id, content, last_synth_at') \
                    .eq('title', entity_name) \
                    .eq('is_current', True) \
                    .limit(1).execute()
                )
            existing_content = existing.data[0]["content"] if existing.data else None
            existing_id = existing.data[0]["id"] if existing.data else None
            last_synth_at = parse_iso(existing.data[0]["last_synth_at"]) if existing.data and existing.data[0].get("last_synth_at") else datetime.min.replace(tzinfo=timezone.utc)
        except Exception as e:
            print(f"  [Error] Failed to fetch canonical page for {entity_name}: {e}")
            existing_content, existing_id, last_synth_at = None, None, datetime.min.replace(tzinfo=timezone.utc)

        all_fragments = []
        seen_hashes = set()
        newest_timestamp = datetime.min.replace(tzinfo=timezone.utc)
        new_fragment_count = 0

        def add_fragment(prefix: str, text: str, ts: datetime):
            nonlocal newest_timestamp, new_fragment_count
            if ts and ts > newest_timestamp:
                newest_timestamp = ts
                
            normalized = text.strip().lower()
            h = hash(normalized)
            if h not in seen_hashes and normalized and not normalized.startswith("http"):
                seen_hashes.add(h)
                all_fragments.append(f"[{prefix}] {text}")
                if ts and ts > last_synth_at:
                    new_fragment_count += 1

        entity_embedding_res = await get_embedding(entity_name)
        entity_embedding = entity_embedding_res.vector if entity_embedding_res else None

        # 1. Memories (via context registry)
        try:
            from core.context import execute_context_strategy, BRAIN_SYNTH_CONFIG
            res = await execute_context_strategy(
                query=entity_name,
                strategy=BRAIN_SYNTH_CONFIG
            )
            mem = [m.metadata for m in res.matched_items]
            
            org_memories = []
            if is_org:
                org_mem_res = await asyncio.to_thread(
                    lambda: supabase.table('memories') \
                    .select('content, created_at') \
                    .eq('organization_id', entity_id) \
                    .order('created_at', desc=True) \
                    .limit(20).execute()
                )
                if org_mem_res and org_mem_res.data:
                    org_memories = org_mem_res.data
            
            all_mem = (mem or []) + org_memories
            if all_mem:
                if not is_org:
                    all_mem = filter_fragments_by_project_strict(all_mem, entity_name)
                for f in all_mem:
                    ts = parse_iso(f.get('created_at'))
                    add_fragment("memory", f.get('content', ''), ts)
        except Exception as e:
            print(f"  [Error] Memories failed for {entity_name}: {e}")

        # 2. Tasks
        try:
            if is_org:
                if project_ids:
                    tasks_res = await asyncio.to_thread(
                        lambda: supabase.table('tasks').select('title, status, created_at, updated_at') \
                        .eq('is_current', True).in_('project_id', project_ids).execute()
                    )
                else:
                    tasks_res = type('obj', (object,), {'data': []})()
            else:
                tasks_res = await asyncio.to_thread(
                    lambda: supabase.table('tasks').select('title, status, created_at, updated_at') \
                    .eq('is_current', True).eq('project_id', entity_id).execute()
                )
            if tasks_res.data:
                for t in tasks_res.data:
                    ts = parse_iso(t.get('updated_at') or t.get('created_at'))
                    add_fragment("task", f"({t['status'].upper()}) {t['title']}", ts)
        except Exception as e:
            print(f"  [Error] Tasks failed for {entity_name}: {e}")

        # 3. Resources (with project_id filter fallback)
        if entity_embedding:
            try:
                resources_res = await asyncio.to_thread(
                    lambda: supabase.rpc('match_resources', {
                        'query_embedding': entity_embedding,
                        'match_threshold': 0.5,
                        'match_count': 20
                    }).execute()
                )
                if resources_res.data:
                    for r in resources_res.data:
                        if not is_org:
                            filtered = filter_fragments_by_project_strict([r], entity_name)
                        else:
                            filtered = [r]
                        if filtered:
                            ts = parse_iso(r.get('enriched_at') or r.get('created_at'))
                            add_fragment("resource", f"{r['title']} — {r.get('summary', '')}", ts)
            except Exception as e:
                print(f"  [Error] Resources failed for {entity_name}: {e}")

        # 4. Raw Dumps
        if entity_embedding:
            try:
                dumps_res = await asyncio.to_thread(
                    lambda: supabase.rpc('match_raw_dumps', {
                        'query_embedding': entity_embedding,
                        'match_threshold': 0.5,
                        'match_count': 30
                    }).execute()
                )
                if dumps_res.data:
                    filtered_dumps = filter_fragments_by_project_strict(dumps_res.data, entity_name)
                    for d in filtered_dumps:
                        ts = parse_iso(d.get('created_at'))
                        add_fragment("dump", d.get('content', ''), ts)
            except Exception as e:
                print(f"  [Error] Raw Dumps failed for {entity_name}: {e}")

        # 5. Emails
        if entity_embedding:
            try:
                emails_res = await asyncio.to_thread(
                    lambda: supabase.rpc('match_emails_hybrid', {
                        'query_embedding': entity_embedding,
                        'match_threshold': 0.5,
                        'match_count': 10
                    }).execute()
                )
                if emails_res.data:
                    filtered_emails = filter_fragments_by_project_strict(emails_res.data, entity_name)
                    for m in filtered_emails:
                        ts = parse_iso(m.get('received_at'))
                        add_fragment("email", f"{m.get('subject', '')} — {m.get('body_summary', '')}", ts)
            except Exception as e:
                print(f"  [Error] Emails failed for {entity_name}: {e}")

        # 6. WhatsApp
        if entity_embedding:
            try:
                wa_res = await asyncio.to_thread(
                    lambda: supabase.rpc('match_whatsapp_hybrid', {
                        'query_embedding': entity_embedding,
                        'match_threshold': 0.5,
                        'match_count': 10
                    }).execute()
                )
                if wa_res.data:
                    filtered_wa = filter_fragments_by_project_strict(wa_res.data, entity_name)
                    for m in filtered_wa:
                        ts = parse_iso(m.get('received_at'))
                        add_fragment("whatsapp", m.get('message_text', ''), ts)
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
                graph_context.append(f"- {e['description']} [graph]")
        except Exception as e:
            print(f"  [Error] Graph edges failed for {entity_name}: {e}")

        # Parent/Child Tasks (Removed legacy logic)
        is_parent = is_org

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
            "project_id": entity_id if not is_org else None,
            "org_id": entity_id if is_org else None,
            "org_name": org_name,
            "org_context": org_context,
            "is_parent": is_parent,
            "existing_page": existing_content or "No existing page — create from scratch.",
            "new_fragments": all_fragments,
            "fragment_count": len(all_fragments),
            "new_fragment_count": new_fragment_count,
            "existing_id": existing_id,
            "graph_context": graph_context
        }

async def run_batch_sweep_v2():
    try:
        active_res = supabase.table('projects') \
            .select('id, name, organization_id') \
            .eq('is_active', True) \
            .eq('status', 'active') \
            .eq('is_current', True) \
            .execute()
            
        orgs_res = supabase.table('organizations').select('id, name, description').eq('is_active', True).execute()
        org_map = {str(o['id']): o for o in orgs_res.data} if orgs_res.data else {}
            
        project_entities = []
        for p in active_res.data:
            org_id = str(p.get('organization_id'))
            if org_id in org_map:
                project_entities.append((p['id'], p.get('name') or p.get('title', ''), org_map[org_id]['name'], org_map[org_id].get('description', '')))

        org_entities = []
        for org in orgs_res.data:
            org_project_ids = [p['id'] for p in active_res.data if str(p.get('organization_id')) == str(org['id'])]
            if org_project_ids:
                org_entities.append((org['id'], org['name'], org.get('description', ''), org_project_ids))

        print(f"Gathering fragments for {len(project_entities)} projects and {len(org_entities)} orgs (Phase 2 Parallel)...")
        
        # Stage 1-3: Gather, filter, incremental check (Parallel)
        tasks = []
        for pid, name, org_n, org_c in project_entities:
            tasks.append(synth_entity(pid, name, org_n, org_c, is_org=False))
        for oid, name, desc, pids in org_entities:
            tasks.append(synth_entity(oid, name, name, desc, is_org=True, project_ids=pids))
        gathered_payloads = await asyncio.gather(*tasks)
        
        # Filter out skips
        batch_payload = []
        for payload in gathered_payloads:
            if payload:
                if payload.get("archive"):
                    if payload.get("existing_id"):
                        # Archive pages that fell below threshold
                        await asyncio.to_thread(
                            lambda e_id=payload['existing_id']: supabase.table('canonical_pages') \
                                .update({"is_current": False}) \
                                .eq('id', e_id) \
                                .execute()
                        )
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
                org_name = entry.get('org_name', '')
                org_context = entry.get('org_context', '')
                is_parent = entry.get('is_parent', False)
                
                if is_parent:
                    prompt_role = "Executive Summary Writer for Danny's OS"
                    prompt_objective = f"Write a high-level overview of the {org_name} domain ({entity_name}). Synthesize all sub-projects and activity under this domain."
                    scope_rules = f"DOMAIN SCOPE: This page covers the {org_name} domain and its sub-projects only.\nEXCLUDE: Any content related to other domains.\nDOMAIN DESCRIPTION: {org_context}"
                else:
                    prompt_role = "Knowledge Curator for Danny's OS"
                    prompt_objective = f"Update the Master Page for {entity_name} (under {org_name})."
                    scope_rules = f"PROJECT SCOPE: This page is ONLY for {entity_name} under {org_name}.\nEXCLUDE: Any content about other projects.\nDOMAIN CONTEXT: {entity_name} belongs to {org_name} ({org_context})."
                
                graph_ctx_str = "\n".join(entry['graph_context']) if entry['graph_context'] else "No known relationships."
                
                per_prompt = f"""
ROLE: {prompt_role}
OBJECTIVE: {prompt_objective}

RULES:
- {scope_rules}
- IMPORTANT: Preserve existing section content unless a new fragment directly contradicts or updates it. Add to sections, don't replace them.
- SPARSE GUARD: Output MUST be at least 300 characters. If fragments are thin, preserve the existing page as-is.
- FORMAT: You MUST follow this exact Markdown structure:
  _Synthesized {datetime.now(timezone.utc).strftime("%b %d, %Y")} · {entry['fragment_count']} sources · {entry['new_fragment_count']} new since last run_
  
  ## Status
  (Active / Winding down / On hold — one sentence summary)
  
  ## Recent Activity
  (New developments since last synthesis)
  
  ## Key People
  (People connected via graph edges, with roles)
  
  ## Active Tasks
  (Open tasks with status)
  
  ## Decisions & Notes
  (Key decisions, context, blockers)
- CITATIONS: Tag claims with their fragment source type at the end of the bullet point, like: [memory], [email], [whatsapp], [task], [resource], [graph].
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

            project_id = payload_entry.get('project_id')
            organization_id = payload_entry.get('org_id')
            existing_id = payload_entry.get('existing_id')
            
            if len(markdown) < 300:
                print(f"Skipping {entity_name} — output too sparse ({len(markdown)} chars)")
                continue

            embedding_res = await get_embedding(markdown)
            embedding = embedding_res.vector if embedding_res else None

            # Find matching graph_node by label to link it
            graph_node_res = await asyncio.to_thread(
                lambda en=entity_name: supabase.table('graph_nodes') \
                .select('id') \
                .ilike('label', en) \
                .limit(1) \
                .execute()
            )
            graph_node_uuid = graph_node_res.data[0]['id'] if graph_node_res and graph_node_res.data else None

            try:
                if existing_id:
                    version_res = await asyncio.to_thread(
                        lambda e_id=existing_id: supabase.table('canonical_pages') \
                        .select('version') \
                        .eq('id', e_id) \
                        .single() \
                        .execute()
                    )
                    old_version = (version_res.data.get('version') or 0) if version_res.data else 0

                    await asyncio.to_thread(
                        lambda e_id=existing_id, ov=old_version, m=markdown, e=embedding, ts=now_iso, sc=payload_entry['fragment_count'], gn_uuid=graph_node_uuid: supabase.table('canonical_pages') \
                        .update({
                            "content": m,
                            "embedding": e,
                            "version": ov + 1,
                            "updated_at": ts,
                            "source_count": sc,
                            "last_synth_at": ts,
                            "is_sparse": len(m) < 500,
                            "entity_id": gn_uuid
                        }) \
                        .eq('id', e_id) \
                        .execute()
                    )

                    if graph_node_uuid:
                        try:
                            await asyncio.to_thread(
                                lambda eid=graph_node_uuid, cid=existing_id: supabase.table('graph_nodes') \
                                .update({"canonical_page_id": cid}) \
                                .eq('id', eid) \
                                .execute()
                            )
                        except Exception as e:
                            print(f"Failed to update graph_nodes link for {entity_name}: {e}")

                    print(f"Master Page Updated: {entity_name} (v{old_version + 1}, {payload_entry['fragment_count']} fragments)")
                else:
                    insert_res = await asyncio.to_thread(
                        lambda en=entity_name, pid=project_id, oid=organization_id, m=markdown, e=embedding, ts=now_iso, sc=payload_entry['fragment_count'], eid=graph_node_uuid: supabase.table('canonical_pages').insert({
                            "title": en,
                            "project_id": pid,
                            "organization_id": oid,
                            "content": m,
                            "embedding": e,
                            "version": 1,
                            "is_current": True,
                            "updated_at": ts,
                            "source_count": sc,
                            "last_synth_at": ts,
                            "is_sparse": len(m) < 500,
                            "entity_id": eid
                        }).execute()
                    )
                    
                    if insert_res and insert_res.data and graph_node_uuid:
                        new_page_id = insert_res.data[0]['id']
                        try:
                            await asyncio.to_thread(
                                lambda eid=graph_node_uuid, cid=new_page_id: supabase.table('graph_nodes') \
                                .update({"canonical_page_id": cid}) \
                                .eq('id', eid) \
                                .execute()
                            )
                        except Exception as e:
                            print(f"Failed to update graph_nodes link for {entity_name}: {e}")
                            
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
