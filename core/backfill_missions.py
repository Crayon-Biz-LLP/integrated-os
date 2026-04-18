import os
import json
import asyncio
import httpx
import re
import time
from datetime import datetime, timezone, timedelta
from supabase import create_client, Client
from google import genai
from dotenv import load_dotenv

load_dotenv()

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
            config={'output_dimensionality': EMBEDDING_DIMENSION}
        )
        return result.embeddings[0].values
    except Exception as e:
        print(f"Embedding error: {e}")
        return [0] * EMBEDDING_DIMENSION


async def fetch_url_metadata(url: str):
    try:
        async with httpx.AsyncClient(timeout=8.0, follow_redirects=True) as client:
            headers = {"User-Agent": "Mozilla/5.0 (compatible; Twitterbot/1.0)"}
            response = await client.get(url, headers=headers)
            if response.status_code == 200:
                html = response.text
                title_match = re.search(r'property=["\']og:title["\'] content=["\'](.*?)["\']', html, re.I)
                title = title_match.group(1).strip() if title_match else "Unknown"
                desc_match = re.search(r'property=["\']og:description["\'] content=["\'](.*?)["\']', html, re.I)
                description = desc_match.group(1).strip() if desc_match else ""
                return {"title": title, "description": description}
    except Exception as e:
        print(f"Scraper error for {url}: {e}")
    return {"title": "Unknown", "description": ""}


async def enrich_pending_resources():
    """Step 1: Enrich all resources that are missing enriched_at."""
    unenriched = supabase.table('resources').select('id, url').is_('enriched_at', None).execute()

    if not unenriched.data:
        print("✅ No unenriched resources found. Skipping enrichment.")
        return

    print(f"🔍 Found {len(unenriched.data)} unenriched resources. Scraping...")
    scraped = await asyncio.gather(*[fetch_url_metadata(r['url']) for r in unenriched.data])

    enrichment_data = []
    for i, r in enumerate(unenriched.data):
        enrichment_data.append({
            "id": r['id'],
            "url": r['url'],
            "title": scraped[i].get('title', 'Unknown'),
            "description": scraped[i].get('description', '')
        })

    BATCH_SIZE = 10
    all_parsed = []

    for i in range(0, len(enrichment_data), BATCH_SIZE):
        batch = enrichment_data[i:i + BATCH_SIZE]
        print(f"📦 Processing batch {i//BATCH_SIZE + 1} of {-(-len(enrichment_data)//BATCH_SIZE)}...")
        
        batch_prompt = f"""You are Danny's Trusted Partner. For each resource below, provide a strategic_note (one sentence on strategic value) and category.

Categories: COMPETITOR, TECH_TOOL, LEAD_POTENTIAL, MARKET_TREND, CHURCH, PERSONAL
Rules:
- CHURCH or PERSONAL for family/home/faith topics
- COMPETITOR for competitors to Qhord
- TECH_TOOL for SaaS/dev/productivity tools
- LEAD_POTENTIAL for potential clients/partners
- MARKET_TREND for market patterns/industry shifts
- Default: MARKET_TREND

Return ONLY valid JSON array:
[
  {{"id": 1, "strategic_note": "...", "category": "..."}},
  ...
]

Resources:
{json.dumps(batch, indent=2)}"""

        try:
            response = gemini_client.models.generate_content(
                model="gemini-3.1-flash-lite-preview",
                contents=batch_prompt,
                config={'response_mime_type': 'application/json'}
            )
            batch_parsed = json.loads(response.text)
            all_parsed.extend(batch_parsed)
            print(f"✅ Batch done: {len(batch_parsed)} items parsed")
        except Exception as e:
            print(f"❌ Batch {i//BATCH_SIZE + 1} failed: {e}")
        
        if i + BATCH_SIZE < len(enrichment_data):
            print("⏳ Sleeping 3s before next batch...")
            time.sleep(3)

    parsed = all_parsed

    try:
        ist_offset = timezone(timedelta(hours=5, minutes=30))
        enriched_at = datetime.now(ist_offset).isoformat()

        for item in parsed:
            for ed in enrichment_data:
                if ed['id'] == item.get('id'):
                    item['title'] = ed['title']
                    item['description'] = ed['description']
                    break

        for item in parsed:
            title = item.get('title', '')
            strategic_note = item.get('strategic_note', '')
            embedding_text = f"{title}. {strategic_note}"
            embedding = get_embedding(embedding_text)

            supabase.table('resources').update({
                "title": title,
                "summary": item.get('description'),
                "strategic_note": strategic_note,
                "category": item.get('category', 'MARKET_TREND'),
                "enriched_at": enriched_at,
                "embedding": embedding
            }).eq('id', item['id']).execute()
            print(f"✅ Enriched: [{item.get('category')}] {title}")

        print(f"\n🎉 Enrichment complete: {len(parsed)} resources updated.")

    except Exception as e:
        print(f"❌ Enrichment error: {e}")


async def backfill_mission_links():
    """Step 2: Link all unlinked resources to active missions by keyword scoring."""
    missions_res = supabase.table('missions').select('id, title').eq('status', 'active').execute()
    active_missions = missions_res.data or []

    if not active_missions:
        print("⚠️ No active missions found. Create missions in Supabase first.")
        return

    print(f"\n🎯 Active missions: {[m['title'] for m in active_missions]}")

    unlinked = supabase.table('resources') \
        .select('id, title, strategic_note, category') \
        .is_('mission_id', None) \
        .execute()

    if not unlinked.data:
        print("✅ All resources already linked to missions.")
        return

    print(f"🔍 Found {len(unlinked.data)} unlinked resources. Matching...")

    linked_count = 0
    unmatched = []

    for resource in unlinked.data:
        resource_text = f"{resource.get('title', '')} {resource.get('strategic_note', '')}".lower()
        best_match = None
        best_score = 0

        for mission in active_missions:
            # Filter out short stop words before scoring
            mission_keywords = [kw for kw in mission['title'].lower().split() if len(kw) > 3]
            match_score = sum(1 for kw in mission_keywords if kw in resource_text)
            if match_score > best_score:
                best_score = match_score
                best_match = mission

        if best_match and best_score >= 2:
            supabase.table('resources').update({
                "mission_id": best_match['id']
            }).eq('id', resource['id']).execute()
            print(f"🔗 '{resource.get('title')}' → '{best_match['title']}' (score: {best_score})")
            linked_count += 1
        else:
            unmatched.append(resource.get('title', 'Unknown'))

    print(f"\n✅ Backfill complete: {linked_count}/{len(unlinked.data)} resources linked")

    if unmatched:
        print(f"\n⚪ No mission match for {len(unmatched)} resources:")
        for title in unmatched:
            print(f"   - {title}")
        print("\nThese likely need a new mission created, or manual assignment in Supabase.")


async def main():
    print("=" * 60)
    print("🚀 BACKFILL START")
    print("=" * 60)

    # Step 1: Enrich resources missing enriched_at
    await enrich_pending_resources()

    # Step 2: Link unlinked resources to missions
    await backfill_mission_links()

    print("\n" + "=" * 60)
    print("✅ BACKFILL COMPLETE")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())