import asyncio
import json
from datetime import datetime, timezone
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
        active_res = supabase.table('projects').select('name').eq('is_active', True).execute()
        entities = list(set([p['name'] for p in active_res.data]))
        
        batch_payload = []
        
        # 2. COLLECT: Bundle fragments for every entity into one payload
        print(f"📡 Gathering fragments for {len(entities)} entities...")
        for entity in entities:
            try:
                mem = supabase.table('memories').select('content') \
                    .or_(f"metadata->>entity.eq.{entity.upper()},content.ilike.%{entity}%") \
                    .execute()
                
                tasks = supabase.table('tasks').select('title, status, created_at') \
                    .ilike('title', f'%{entity}%') \
                    .limit(30).execute()
                
                logs = supabase.table('logs').select('content, created_at') \
                    .ilike('content', f'%{entity}%') \
                    .limit(20).execute()
                
                resources = supabase.table('resources').select('title, summary, strategic_note') \
                    .ilike('title', f'%{entity}%') \
                    .limit(20).execute()
            except Exception as e:
                print(f"Skipping {entity} — failed to fetch fragments: {e}")
                continue
            
            all_fragments = []
            if mem.data: all_fragments += [f['content'] for f in mem.data]
            if tasks.data: all_fragments += [f"TASK ({t['status']}): {t['title']}" for t in tasks.data]
            if logs.data: all_fragments += [f['content'] for f in logs.data]
            if resources.data: all_fragments += [f"RESOURCE: {r['title']} — {r.get('summary','')}" for r in resources.data]
            
            if all_fragments:
                content = "\n".join(all_fragments)
                batch_payload.append({"entity": entity, "data": content})

        if not batch_payload:
            print("No data found to synthesize.")
            return

        # 3. CONSOLIDATE: Single "Grand Sweep" Prompt
        prompt = f"""
        ROLE: Senior Historian for Danny's OS.
        OBJECTIVE: Synthesize high-fidelity Master Pages for {len(batch_payload)} distinct entities.
        
        RULES:
        - THE REVENUE GUARD: Solvstrat = Service (Now/Leads/Sales). Qhord = Product (June GTM).
        - ATTRIBUTION: Map client wins/pipelines to Solvstrat. Map beta/GTM milestones to Qhord.
        - VERTICALITY: Use clean Markdown headers and bulleted lists.
        - OUTPUT: Return a JSON object where keys are Entity Names and values are the synthesized Markdown content.
        
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

        # 5. COMMIT: Bulk update the Canonical Record in Supabase
        for entity, markdown in results.items():
            embedding = get_embedding(markdown)
            if not embedding:
                print(f"Warning: empty embedding for {entity}, storing without vector")
            try:
                normalized_title = entity.strip()
                existing = supabase.table('canonical_pages').select('id').ilike('title', normalized_title).execute()
                if existing.data:
                    supabase.table('canonical_pages').update({
                        "content": markdown,
                        "embedding": embedding,
                        "updated_at": datetime.now(timezone.utc).isoformat()
                    }).eq('id', existing.data[0]['id']).execute()
                    print(f"✅ Master Page Updated: {entity}")
                else:
                    supabase.table('canonical_pages').insert({
                        "title": normalized_title,
                        "content": markdown,
                        "embedding": embedding,
                        "updated_at": datetime.now(timezone.utc).isoformat()
                    }).execute()
                    print(f"✅ Master Page Created: {entity}")
            except Exception as e:
                print(f"Failed to update canonical page for {entity}: {e}")
                continue
    except Exception as e:
        print(f"Brain sweep failed: {e}")

if __name__ == "__main__":
    asyncio.run(run_batch_sweep())