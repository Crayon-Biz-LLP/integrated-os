import asyncio
import json
from datetime import datetime, timezone
from dotenv import load_dotenv
load_dotenv()
from core.pulse import supabase, get_embedding, call_gemini_with_retry

async def run_batch_sweep():
    try:
        # 1. GATHER TARGETS: Fetch active projects and define core pillars
        active_res = supabase.table('projects').select('name').eq('is_active', True).execute()
        core_missions = ["Solvstrat", "Qhord", "Church", "Canadian Project"]
        entities = list(set([p['name'] for p in active_res.data] + core_missions))
        
        batch_payload = []
        
        # 2. COLLECT: Bundle fragments for every entity into one payload
        print(f"📡 Gathering fragments for {len(entities)} entities...")
        for entity in entities:
            try:
                fragments = supabase.table('memories').select('content') \
                    .or_(f"metadata->>entity.eq.{entity.upper()},content.ilike.%{entity}%") \
                    .execute()
            except Exception as e:
                print(f"Skipping {entity} — failed to fetch fragments: {e}")
                continue
            
            if fragments.data:
                content = "\n".join([f["content"] for f in fragments.data])
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
                supabase.table('canonical_pages').upsert({
                    "title": entity,
                    "content": markdown,
                    "embedding": embedding,
                    "updated_at": datetime.now(timezone.utc).isoformat()
                }, on_conflict='title').execute()
                print(f"✅ Master Page Updated: {entity}")
            except Exception as e:
                print(f"Failed to update canonical page for {entity}: {e}")
                continue
    except Exception as e:
        print(f"Brain sweep failed: {e}")

if __name__ == "__main__":
    asyncio.run(run_batch_sweep())