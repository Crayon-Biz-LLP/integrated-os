import os
import re
import httpx
from datetime import datetime, timedelta, timezone
from typing import Optional
from fastapi import HTTPException
from pydantic import BaseModel
from supabase import create_client, Client
from google import genai


supabase: Client = create_client(
    os.getenv("SUPABASE_URL"),
    os.getenv("SUPABASE_SERVICE_ROLE_KEY")
)
gemini_client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))

VALID_CATEGORIES = ["COMPETITOR", "TECH_TOOL", "LEAD_POTENTIAL", "MARKET_TREND", "CHURCH", "PERSONAL"]


class SupabasePayload(BaseModel):
    record: dict


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
        print(f"Scraper error for {url}: {e}")
    return {"title": "Unknown", "description": ""}


async def generate_strategic_note(url: str, title: str, summary: str) -> tuple[str, str]:
    prompt = f"""You are Danny's Chief of Staff. Mission 10 is about accelerating Qhord's June launch and achieving ₹30L cashflow recovery.

For this resource:
- URL: {url}
- Title: {title}
- Summary: {summary}

Analyze this resource and provide:
1. A ONE sentence strategic value for Mission 10 (how it could help Qhord's GTM, product validation, pilot acquisition, or revenue acceleration)
2. A category from this list: {', '.join(VALID_CATEGORIES)}

Rules:
- If the resource is about family, home, faith, or personal life → CHURCH or PERSONAL
- If it mentions competitors to Qhord → COMPETITOR
- If it's a SaaS tool, developer tool, or productivity app → TECH_TOOL
- If it's about potential clients or partners → LEAD_POTENTIAL
- If it reveals market patterns, trends, or industry shifts → MARKET_TREND
- Default to MARKET_TREND if uncertain

Response format (exactly this, no other text):
STRATEGIC_NOTE: [your one sentence]
CATEGORY: [one category from the list]"""

    try:
        response = gemini_client.models.generate_content(
            model="gemini-3-flash-preview",
            contents=prompt
        )
        text = response.text.strip()
        
        note_match = re.search(r'STRATEGIC_NOTE:\s*(.+?)(?:CATEGORY:|$)', text, re.DOTALL)
        cat_match = re.search(r'CATEGORY:\s*(\w+)', text)
        
        strategic_note = note_match.group(1).strip() if note_match else ""
        category = cat_match.group(1).strip() if cat_match else "MARKET_TREND"
        
        if category not in VALID_CATEGORIES:
            category = "MARKET_TREND"
        
        return strategic_note, category
    except Exception as e:
        print(f"Gemini error: {e}")
        return "", "MARKET_TREND"


def get_ist_timestamp() -> str:
    ist_offset = timezone(timedelta(hours=5, minutes=30))
    return datetime.now(ist_offset).isoformat()


async def enrich_resource_from_webhook(payload: SupabasePayload) -> dict:
    record = payload.record
    resource_id = record.get('id')
    
    if not resource_id:
        raise HTTPException(status_code=400, detail="No id in webhook record")
    
    if record.get('summary'):
        return {"message": "Already enriched, skipping.", "id": resource_id}
    
    url = record.get('url')
    if not url:
        raise HTTPException(status_code=400, detail="No URL in record")
    
    meta = await fetch_url_metadata(url)
    strategic_note, category = await generate_strategic_note(url, meta['title'], meta['description'])
    
    supabase.table('resources').update({
        "title": meta['title'],
        "summary": meta['description'],
        "strategic_note": strategic_note,
        "category": category,
        "enriched_at": get_ist_timestamp()
    }).eq('id', resource_id).execute()
    
    return {
        "success": True,
        "id": resource_id,
        "title": meta['title'],
        "summary": meta['description'],
        "strategic_note": strategic_note,
        "category": category
    }


async def enrich_resource_by_id(resource_id: int) -> dict:
    resource = supabase.table('resources').select('id, url, summary, mission_id').eq('id', resource_id).single().execute()

    if not resource.data:
        raise HTTPException(status_code=404, detail="Resource not found")

    if resource.data.get('summary'):
        return {"message": "Already enriched, skipping.", "id": resource_id}

    url = resource.data.get('url')
    if not url:
        raise HTTPException(status_code=400, detail="No URL in resource")

    meta = await fetch_url_metadata(url)
    strategic_note, category = await generate_strategic_note(url, meta['title'], meta['description'])

    supabase.table('resources').update({
        "title": meta['title'],
        "summary": meta['description'],
        "strategic_note": strategic_note,
        "category": category,
        "enriched_at": get_ist_timestamp()
    }).eq('id', resource_id).execute()

    return {
        "success": True,
        "id": resource_id,
        "title": meta['title'],
        "summary": meta['description'],
        "strategic_note": strategic_note,
        "category": category
    }


if __name__ == "__main__":
    import uvicorn
    from fastapi import FastAPI
    
    app = FastAPI(title="Shadow Scouter")
    
    @app.post("/api/enrich")
    async def enrich_webhook(payload: SupabasePayload):
        return await enrich_resource_from_webhook(payload)
    
    @app.post("/api/enrich/{resource_id}")
    async def enrich_by_id(resource_id: int):
        return await enrich_resource_by_id(resource_id)
    
    uvicorn.run(app, host="0.0.0.0", port=8000)
