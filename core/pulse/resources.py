import os
import re
import json
import asyncio
import httpx
from datetime import datetime, timezone, timedelta
from supabase import create_client, Client
from core.lib.audit_logger import audit_log_sync
from core.services.db import versioned_update
from core.pulse.llm import call_llm_with_fallback, parse_json_response, get_embedding

supabase: Client = create_client(
    os.getenv("SUPABASE_URL"),
    os.getenv("SUPABASE_SERVICE_ROLE_KEY")
)


async def fetch_url_metadata(url: str):
    try:
        async with httpx.AsyncClient(timeout=8.0, follow_redirects=True) as http_client:
            headers = {
                "User-Agent": "Mozilla/5.0 (compatible; Twitterbot/1.0)",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8"
            }
            response = await http_client.get(url, headers=headers)
            if response.status_code == 200:
                html = response.text
                title_match = re.search(r'property=["\']og:title["\'] content=["\'](.*?)["\']', html, re.I)
                title = title_match.group(1).strip() if title_match else "Unknown"
                desc_match = re.search(r'property=["\']og:description["\'] content=["\'](.*?)["\']', html, re.I)
                description = desc_match.group(1).strip() if desc_match else ""
                return {"title": title, "description": description}
    except Exception as e:
        audit_log_sync("pulse", "ERROR", f"Scraper error for {url}: {e}")
    return {"title": "Unknown", "description": ""}

async def batch_enrich_resources():
    unenriched = supabase.table('resources').select('id, url').is_('enriched_at', None).execute()
    if not unenriched.data:
        print("📚 No unenriched resources found.")
        return []

    print(f"🔍 Found {len(unenriched.data)} unenriched resources. Scraping in parallel...")
    scraped = await asyncio.gather(*[fetch_url_metadata(r['url']) for r in unenriched.data])

    enrichment_data = []
    for i, r in enumerate(unenriched.data):
        enrichment_data.append({
            "id": r['id'],
            "url": r['url'],
            "title": scraped[i].get('title', 'Unknown'),
            "description": scraped[i].get('description', '')
        })

    if not enrichment_data:
        return []

    prompt = f"""You are Danny's Trusted Partner. For each resource below, provide a strategic_note (one sentence on strategic value) and category.

    Categories: COMPETITOR, TECH_TOOL, LEAD_POTENTIAL, MARKET_TREND, ASHRAYA, PERSONAL
    Rules:
    - ASHRAYA for Ashraya church admin/operations content
    - PERSONAL for family/home/personal spiritual topics
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
    {json.dumps(enrichment_data, indent=2)}"""

    try:
        response = await call_llm_with_fallback(
            prompt=prompt,
            model="gemini-3.1-flash-lite-preview",
            config={'response_mime_type': 'application/json'},
            is_critical=False,
            require_json=True
        )
        parsed = parse_json_response(response.text)

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
            embedding = await asyncio.to_thread(get_embedding, embedding_text)
            if all(v == 0 for v in embedding):
                audit_log_sync("pulse", "WARNING", f"Warning: zero-vector embedding for daily reflection — storing anyway")

            # Versioned update for resources
            versioned_update('resources', item['id'], {
                "title": title,
                "summary": item.get('description'),
                "strategic_note": strategic_note,
                "category": item.get('category', 'MARKET_TREND'),
                "enriched_at": enriched_at,
                "embedding": embedding
            })

        print(f"✅ Batch enriched {len(parsed)} resources with embeddings.")

        # MISSION RESOLVER: Link enriched resources to active missions by name
        try:
            missions_res = supabase.table('missions').select('id, title').eq('status', 'active').execute()
            active_missions = missions_res.data or []

            unlinked = supabase.table('resources').select('id, title, strategic_note').is_('mission_id', None).not_.is_('enriched_at', None).execute()

            for resource in (unlinked.data or []):
                resource_text = f"{resource.get('title', '')} {resource.get('strategic_note', '')}".lower()
                for mission in active_missions:
                    mission_keywords = mission['title'].lower().split()
                    match_score = sum(1 for kw in mission_keywords if kw in resource_text)
                    if match_score >= 2:
                        # Use versioned_update for mission linking (creates history)
                        versioned_update(
                            table_name='resources',
                            record_id=resource['id'],
                            update_data={"mission_id": mission['id']},
                            user_id=None,
                            change_source='pulse_mission_resolver',
                            change_reason=f"Linked to mission: {mission['title']}"
                        )
                        audit_log_sync("pulse", "INFO",
                            f"🔗 Linked resource '{resource.get('title')}' → mission '{mission['title']}'")
                        break
        except Exception as e:
            audit_log_sync("pulse", "WARNING", f"⚠️ Mission resolver error: {e}")

        return parsed
    except Exception as e:
        audit_log_sync("pulse", "ERROR", f"Batch enrichment error: {e}")
        return []
