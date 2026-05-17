import asyncio
import json
import os
import httpx
from datetime import datetime, timezone, timedelta

from core.services.db import get_supabase, get_embedding
from core.services.llm import call_gemini_with_retry

supabase = get_supabase()


async def run_batch_sweep():
    try:
        active_res = supabase.table('projects') \
            .select('id, name') \
            .eq('is_active', True) \
            .eq('status', 'active') \
            .execute()
        entities = [(p['id'], p.get('name') or p.get('title', '')) for p in active_res.data]

        batch_payload = []

        print("Checking canonical page freshness...")
        stale_threshold = datetime.now(timezone.utc) - timedelta(hours=24)
        stale_pages = supabase.table('canonical_pages') \
            .select('title, last_synth_at, project_id') \
            .eq('is_current', True) \
            .lt('last_synth_at', stale_threshold.isoformat()) \
            .execute()
        if stale_pages.data:
            print(f"{len(stale_pages.data)} stale pages detected: {[p['title'] for p in stale_pages.data]}")
            for p in stale_pages.data:
                if not any(e[0] == p.get('project_id') for e in entities):
                    entities.append((p.get('project_id'), p['title']))

        print(f"Gathering fragments for {len(entities)} entities...")
        for project_id, entity_name in entities:
            try:
                all_fragments = []
                seen_hashes = set()

                def add_fragment(prefix: str, text: str):
                    normalized = text.strip().lower()
                    h = hash(normalized)
                    if h not in seen_hashes and normalized:
                        seen_hashes.add(h)
                        all_fragments.append(f"[{prefix}] {text}")

                entity_embedding = get_embedding(entity_name)

                if entity_embedding:
                    mem = supabase.rpc('match_memories', {
                        'query_embedding': entity_embedding,
                        'match_threshold': 0.5,
                        'match_count': 20
                    }).execute()
                    if mem.data:
                        for f in mem.data:
                            add_fragment("MEMORY", f['content'])

                tasks = supabase.table('tasks').select('title, status') \
                    .eq('project_id', project_id).execute()
                if tasks.data:
                    for t in tasks.data:
                        add_fragment(f"TASK/{t['status'].upper()}", t['title'])

                if entity_embedding:
                    logs = supabase.rpc('match_logs', {
                        'query_embedding': entity_embedding,
                        'match_threshold': 0.5,
                        'match_count': 20
                    }).execute()
                    if logs.data:
                        for f in logs.data:
                            add_fragment("LOG", f['content'])

                if entity_embedding:
                    resources = supabase.rpc('match_resources', {
                        'query_embedding': entity_embedding,
                        'match_threshold': 0.5,
                        'match_count': 10
                    }).execute()
                    if resources.data:
                        for r in resources.data:
                            add_fragment("RESOURCE", f"{r['title']} — {r.get('summary', '')}")

                if entity_embedding:
                    dumps = supabase.rpc('match_raw_dumps', {
                        'query_embedding': entity_embedding,
                        'match_threshold': 0.5,
                        'match_count': 30
                    }).execute()
                    if dumps.data:
                        for d in dumps.data:
                            add_fragment("DUMP", d['content'])

                people = supabase.table('people').select('name, role, strategic_weight') \
                    .ilike('name', f'%{entity_name}%').execute()
                if people.data:
                    for p in people.data:
                        add_fragment("PERSON", f"{p.get('name', 'Unknown')} — {p.get('role', 'Unknown role')}")

            except Exception as e:
                print(f"Skipping {entity_name} — failed to fetch fragments: {e}")
                continue

            if all_fragments:
                existing = supabase.table('canonical_pages') \
                    .select('id, content') \
                    .eq('project_id', project_id) \
                    .eq('is_current', True) \
                    .limit(1).execute()

                existing_content = existing.data[0]["content"] if existing.data else None
                existing_id = existing.data[0]["id"] if existing.data else None

                batch_payload.append({
                    "entity": entity_name,
                    "project_id": project_id,
                    "existing_page": existing_content or "No existing page — create from scratch.",
                    "new_fragments": all_fragments,
                    "fragment_count": len(all_fragments),
                    "existing_id": existing_id
                })

        if not batch_payload:
            print("No data found to synthesize.")
            return

        telegram_chat_id = os.getenv("TELEGRAM_CHAT_ID")
        telegram_bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
        if telegram_chat_id and telegram_bot_token:
            try:
                url = f"https://api.telegram.org/bot{telegram_bot_token}/sendMessage"
                payload = {"chat_id": int(telegram_chat_id), "text": f"Synthesizing {len(batch_payload)} Master Pages...", "parse_mode": "Markdown"}
                httpx.post(url, json=payload, timeout=10)
            except:
                pass

        print("Synthesizing Master Pages per entity...")
        results = {}
        for entry in batch_payload:
            entity_name = entry['entity']
            print(f"  Processing {entity_name} ({entry['fragment_count']} fragments)...")

            per_prompt = f"""
ROLE: Senior Historian and Knowledge Curator for Danny's OS.
OBJECTIVE: Update the Master Page for {entity_name} using an ACCUMULATION MODEL.

RULES:
- MERGE, DO NOT REPLACE. The existing page is ground truth. Never discard established facts.
- ENRICH. Add new information from NEW FRAGMENTS that isn't already in the existing page.
- CORRECT. If new fragments contradict the existing page, update with the newer information.
- REVENUE GUARD: Solvstrat = Service (Leads/Sales/Clients). Qhord = Product (GTM/Beta).
- ATTRIBUTION: Map client wins to Solvstrat. Map GTM milestones to Qhord.
- SPARSE GUARD: Output MUST be at least 300 characters. If fragments are thin, preserve the existing page as-is.
- FORMAT: Clean Markdown with headers and bullets.
- OUTPUT: Return ONLY the raw Markdown string. No JSON wrapper.

EXISTING PAGE:
{entry['existing_page']}

NEW FRAGMENTS:
{json.dumps(entry['new_fragments'], indent=2)}
"""
            try:
                response = await call_gemini_with_retry(
                    prompt=per_prompt,
                    model="gemini-3.1-flash-lite-preview",
                    config={'response_mime_type': 'text/plain'}
                )
                if response and response.text:
                    results[entity_name] = response.text.strip()
                else:
                    print(f"No response for {entity_name}, skipping.")
            except Exception as e:
                print(f"Gemini failed for {entity_name}: {e}")
                continue

        for entity_name, markdown in results.items():
            payload_entry = next((p for p in batch_payload if p['entity'] == entity_name), None)
            if not payload_entry:
                continue

            project_id = payload_entry['project_id']
            existing_id = payload_entry.get('existing_id')
            existing_content = payload_entry.get('existing_page', '')

            if len(markdown) < 300:
                print(f"Skipping {entity_name} — output too sparse ({len(markdown)} chars)")
                continue

            if existing_content and len(markdown) < len(existing_content) * 0.6:
                print(f"Skipping {entity_name} — output significantly shorter than existing page.")
                continue

            embedding = get_embedding(markdown)
            now_iso = datetime.now(timezone.utc).isoformat()

            try:
                if existing_id:
                    version_res = supabase.table('canonical_pages') \
                        .select('version') \
                        .eq('id', existing_id) \
                        .single() \
                        .execute()
                    old_version = (version_res.data.get('version') or 0) if version_res.data else 0

                    supabase.table('canonical_pages').insert({
                        "title": entity_name,
                        "project_id": project_id,
                        "content": markdown,
                        "embedding": embedding,
                        "version": old_version + 1,
                        "is_current": True,
                        "supersedes_id": existing_id,
                        "updated_at": now_iso,
                        "source_count": payload_entry['fragment_count'],
                        "last_synth_at": now_iso,
                        "is_sparse": len(markdown) < 500
                    }).execute()

                    supabase.table('canonical_pages') \
                        .update({"is_current": False}) \
                        .eq('id', existing_id) \
                        .execute()

                    print(f"Master Page Versioned: {entity_name} (v{old_version + 1}, {payload_entry['fragment_count']} fragments)")
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
        print(f"Brain sweep failed: {e}")
        telegram_chat_id = os.getenv("TELEGRAM_CHAT_ID")
        telegram_bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
        if telegram_chat_id and telegram_bot_token:
            try:
                url = f"https://api.telegram.org/bot{telegram_bot_token}/sendMessage"
                payload = {"chat_id": int(telegram_chat_id), "text": f"Brain Synthesizer failed: {str(e)[:100]}", "parse_mode": "Markdown"}
                httpx.post(url, json=payload, timeout=10)
            except:
                pass


if __name__ == "__main__":
    asyncio.run(run_batch_sweep())
