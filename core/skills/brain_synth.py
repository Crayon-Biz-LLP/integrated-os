import asyncio
import os
from dotenv import load_dotenv
load_dotenv()
from core.pulse import supabase, get_embedding, call_gemini_with_retry

async def synthesize_master_page(entity_label: str):
    # 1. RETRIEVE: Get all fragments related to this entity
    # We search memories, logs, and resources for this specific label
    fragments = supabase.table('memories').select('content').ilike('content', f'%{entity_label}%').execute()
    raw_data = "\n---\n".join([f['content'] for f in fragments.data])

    if not raw_data:
        return

    # 2. THINK: The Historian Prompt
    prompt = f"""
    ROLE: You are the Senior Historian for Danny's OS. 
    OBJECTIVE: Synthesize the following raw fragments into a 'Master Page' for the entity: {entity_label}.
    
    RULES:
    - CONFLICT RESOLUTION: If fragments contradict, prioritize the one with the most recent timestamp or specific numbers (e.g., CAD 15k).
    - NARRATIVE FOCUS: Don't just list bullets. Write a concise 'Current Strategic Standing'.
    - PERSONA: Professional, direct, architecture-first.
    - STRICT ATTRIBUTION: If a fragment mentions multiple entities, only include it in the Master Page if this entity ({entity_label}) is clearly the primary subject of the action (e.g., a sale, a specific technical task).
- REVENUE MAPPING: Ensure client wins and pipelines are mapped to the correct business entity. Solvstrat is the service firm; Qhord is the GTM product.
    
    RAW FRAGMENTS:
    {raw_data}
    
    OUTPUT FORMAT: Markdown. Use headers for 'Context', 'Latest Decisions', and 'Open Threads'.
    """

    response = await call_gemini_with_retry(prompt=prompt, model="gemini-3-flash-preview")
    synthesized_text = response.text

    # 3. WRITE: Update the Canonical Page
    embedding = get_embedding(synthesized_text)
    supabase.table('canonical_pages').upsert({
        "title": entity_label,
        "content": synthesized_text,
        "embedding": embedding,
        "updated_at": "now()"
    }, on_conflict='title').execute()

    print(f"🧠 Master Page Synthesized: {entity_label}")

async def sweep_all_active_entities():
    # 🎯 TARGET THE SOURCE OF TRUTH
    # Pull all projects that are currently marked as 'active' in your DB
    active_projects = supabase.table('projects').select('name').eq('is_active', True).execute()
    
    # Also include your core high-level missions from config
    core_missions = ["Solvstrat", "Qhord", "Crayon Biz", "Church"]
    
    # Combine them, ensuring no duplicates
    entities_to_sync = list(set([p['name'] for p in active_projects.data] + core_missions))
    
    print(f"📡 Starting Dynamic Sweep for {len(entities_to_sync)} entities...")
    
    for entity in entities_to_sync:
        await synthesize_master_page(entity)

if __name__ == "__main__":
    asyncio.run(sweep_all_active_entities())