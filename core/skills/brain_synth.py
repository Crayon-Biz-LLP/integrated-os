import asyncio
import json
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv
load_dotenv()
import os
from supabase import create_client, Client
from google import genai

supabase: Client = create_client(
    os.getenv("SUPABASE_URL"),
    os.getenv("SUPABASE_SERVICE_ROLE_KEY")
)
gemini_client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))

EMBEDDING_MODEL = "gemini-embedding-2-preview"
EMBEDDING_DIMENSION = 768

def get_embedding(text: str) -> list:
    try:
        result = gemini_client.models.embed_content(
            model=EMBEDDING_MODEL,
            contents=text,
            config={"output_dimensionality": EMBEDDING_DIMENSION}
        )
        return result.embeddings[0].values
    except Exception as e:
        print(f"Embedding error: {e}")
        return []

async def call_gemini_with_retry(prompt: str, model: str = None, config: dict = None):
    import asyncio
    max_retries = 5
    base_delay = 15
    retryable_errors = ['503', '504', '500', 'disconnected', 'timeout', 'deadline exceeded']
    for attempt in range(max_retries):
        try:
            response = gemini_client.models.generate_content(
                model=model,
                contents=prompt,
                config=config or {}
            )
            return response
        except Exception as e:
            error_str = str(e).lower()
            should_retry = any(err in error_str for err in retryable_errors)
            if should_retry and attempt < max_retries - 1:
                delay = base_delay * (2 ** attempt)
                print(f"⚠️ Gemini retry ({error_str}), retrying in {delay}s...")
                await asyncio.sleep(delay)
                continue
            else:
                raise

async def run_batch_sweep():
    try:
        # 1. GATHER TARGETS: Fetch active projects and define core pillars
        active_res = supabase.table('projects') \
            .select('id, name') \
            .eq('is_active', True) \
            .eq('status', 'active') \
            .execute()
        entities = [(p['id'], p['name']) for p in active_res.data]
        
        batch_payload = []
        
        # Staleness check
        print(f"🔍 Checking canonical page freshness...")
        stale_threshold = datetime.now(timezone.utc) - timedelta(hours=24)
        stale_pages = supabase.table('canonical_pages') \
            .select('title, last_synth_at') \
            .lt('last_synth_at', stale_threshold.isoformat()) \
            .execute()
        if stale_pages.data:
            print(f"⚠️ {len(stale_pages.data)} stale pages detected: {[p['title'] for p in stale_pages.data]}")
        
        # 2. COLLECT: Bundle fragments for every entity into one payload
        print(f"📡 Gathering fragments for {len(entities)} entities...")
        for project_id, entity_name in entities:
            try:
                all_fragments = []

                # memories — name match only
                mem = supabase.table('memories').select('content') \
                    .ilike('content', f'%{entity_name}%').execute()
                if mem.data:
                    all_fragments += [f"[MEMORY] {f['content']}" for f in mem.data]

                # tasks — match by project_id first, then name
                tasks = supabase.table('tasks').select('title, status') \
                    .eq('project_id', project_id).execute()
                if tasks.data:
                    all_fragments += [f"[TASK/{t['status'].upper()}] {t['title']}" for t in tasks.data]

                # logs — name match
                logs = supabase.table('logs').select('content') \
                    .ilike('content', f'%{entity_name}%').execute()
                if logs.data:
                    all_fragments += [f"[LOG] {f['content']}" for f in logs.data]

                # resources — name match
                resources = supabase.table('resources').select('title, summary, strategic_note') \
                    .ilike('title', f'%{entity_name}%').execute()
                if resources.data:
                    all_fragments += [f"[RESOURCE] {r['title']} — {r.get('summary', '')}" for r in resources.data]

                # raw_dumps — full historical input stream
                dumps = supabase.table('raw_dumps').select('content') \
                    .ilike('content', f'%{entity_name}%').execute()
                if dumps.data:
                    all_fragments += [f"[DUMP] {d['content']}" for d in dumps.data]

                # people — linked to this project
                people = supabase.table('people').select('name, role, strategic_weight') \
                    .eq('project_id', project_id).execute()
                if not people.data:
                    people = supabase.table('people').select('name, role, strategic_weight') \
                        .ilike('name', f'%{entity_name}%').execute()
                if people.data:
                    all_fragments += [f"[PERSON] {p['name']} — {p.get('role', 'Unknown role')}" \
                        for p in people.data]
            except Exception as e:
                print(f"Skipping {entity_name} — failed to fetch fragments: {e}")
                continue
            
            if all_fragments:
                # Feed existing page back into synthesis
                existing = supabase.table('canonical_pages') \
                    .select('id, content') \
                    .eq('project_id', project_id) \
                    .maybe_single().execute()

                existing_content = existing.data['content'] if existing.data else None
                existing_id = existing.data['id'] if existing.data else None

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

        # 3. CONSOLIDATE: Single "Grand Sweep" Prompt
        prompt = f"""
ROLE: Senior Historian and Knowledge Curator for Danny's OS.
OBJECTIVE: Update {len(batch_payload)} Master Pages using an ACCUMULATION MODEL.

RULES:
- MERGE, DO NOT REPLACE. The existing page is ground truth. Never discard established facts.
- ENRICH. Add new information from NEW FRAGMENTS that isn't already in the existing page.
- CORRECT. If new fragments contradict the existing page, update with the newer information.
- REVENUE GUARD: Solvstrat = Service (Leads/Sales/Clients). Qhord = Product (GTM/Beta).
- ATTRIBUTION: Map client wins to Solvstrat. Map GTM milestones to Qhord.
- SPARSE GUARD: Output MUST be at least 300 characters. If fragments are thin, preserve the existing page as-is.
- FORMAT: Clean Markdown with headers and bullets.
- OUTPUT: Return a JSON object where keys are entity names and values are the merged Markdown content.

DATA BUNDLE:
{json.dumps(batch_payload)}
"""

        # 4. EXECUTE: Call Gemini (Using 3.1 Flash Lite for 500 RPD safety)
        print("🧠 Synthesizing Master Pages in batch mode...")
        response = await call_gemini_with_retry(
            prompt=prompt,
            model="gemini-3.1-flash-lite-preview",
            config={'response_mime_type': 'application/json'}
        )
        
        # Handle the potential JSON formatting from the LLM
        clean_json = response.text.replace('```json', '').replace('```', '').strip()
        try:
            results = json.loads(clean_json)
        except Exception as e:
            print(f"Brain synth JSON parse failed: {e}\nRaw response: {clean_json[:200]}")
            return

        # 5. COMMIT: Validated write with quality checks
        for entity_name, markdown in results.items():
            # Find matching payload entry
            payload_entry = next((p for p in batch_payload if p['entity'] == entity_name), None)
            if not payload_entry:
                continue

            project_id = payload_entry['project_id']
            existing_id = payload_entry.get('existing_id')
            existing_content = payload_entry.get('existing_page', '')

            # VALIDATION GATE — never write a page shorter than existing
            if len(markdown) < 300:
                print(f"⚠️ Skipping {entity_name} — output too sparse ({len(markdown)} chars)")
                continue

            if existing_content and len(markdown) < len(existing_content) * 0.6:
                print(f"⚠️ Skipping {entity_name} — output is significantly shorter than existing page. Possible bad run.")
                continue

            embedding = get_embedding(markdown)
            now_iso = datetime.now(timezone.utc).isoformat()

            if existing_id:
                supabase.table('canonical_pages').update({
                    "content": markdown,
                    "embedding": embedding,
                    "updated_at": now_iso,
                    "source_count": payload_entry['fragment_count'],
                    "last_synth_at": now_iso,
                    "is_sparse": len(markdown) < 500
                }).eq('id', existing_id).execute()
                print(f"✅ Master Page Merged: {entity_name} ({payload_entry['fragment_count']} fragments)")
            else:
                supabase.table('canonical_pages').insert({
                    "title": entity_name,
                    "project_id": project_id,
                    "content": markdown,
                    "embedding": embedding,
                    "updated_at": now_iso,
                    "source_count": payload_entry['fragment_count'],
                    "last_synth_at": now_iso,
                    "is_sparse": len(markdown) < 500
                }).execute()
                print(f"✅ Master Page Created: {entity_name} ({payload_entry['fragment_count']} fragments)")
    except Exception as e:
        print(f"Brain sweep failed: {e}")

if __name__ == "__main__":
    asyncio.run(run_batch_sweep())