"""Continuous cluster discovery — groups unmapped resources into coherent themes.

Extracted from core/pulse/engine.py as a focused module.
Uses a single LLM call to identify natural groupings of 3+ related
resources, creating new clusters when a coherent strategic theme emerges.
"""
import json
from datetime import datetime, timedelta, timezone

from core.llm.constants import CLASSIFICATION_MODEL
from core.llm.fallback import generate_content_with_fallback
from core.llm.config import WorkloadProfile
from core.lib.audit_logger import audit_log_sync
from core.pulse.llm import supabase
from core.pulse.utils import normalize_cluster_title


async def discover_new_clusters():
    """Analyze unmapped resources for natural groupings and create new clusters
    when 3+ related resources form a coherent theme.

    Uses a single Gemini Flash Lite call to classify resources into clusters.
    Idempotent — skips resources already assigned to a cluster.
    """
    try:
        unclustered_res = supabase.table('resources').select(
            'id, url, title, summary, strategic_note, category'
        ).is_('cluster_id', None).eq('is_current', True).limit(100).execute()
        unclustered = unclustered_res.data or []
        if len(unclustered) < 3:
            audit_log_sync("pulse", "INFO", f"📍 Cluster discovery: only {len(unclustered)} unmapped resources, need 3+.")
            return []

        existing_res = supabase.table('clusters').select('id, title').eq('status', 'active').execute()
        existing_titles = set(m['title'].lower() for m in (existing_res.data or []))
        existing_list = ", ".join(sorted(existing_titles)) or "None"

        resources_json = json.dumps([{
            "id": r['id'],
            "url": r.get('url', ''),
            "title": r.get('title', ''),
            "summary": r.get('summary', ''),
            "strategic_note": r.get('strategic_note', ''),
            "category": r.get('category', '')
        } for r in unclustered], indent=2)

        prompt = f"""You are a cluster discoverer. Review these unclustered resources.

Existing active clusters: {existing_list}

Rules:
- Identify any natural groupings of 3+ resources that form a coherent strategic theme NOT covered by existing active clusters.
- Only suggest a new cluster if at least 3 resources clearly belong together under a single strategic theme.
- If no such grouping exists, return an empty array.
- Do not suggest clusters that overlap with existing cluster titles.

Return ONLY valid JSON array:
[
  {{"cluster_title": "New Cluster Name", "resource_ids": [1, 2, 3], "description": "Strategic intent for this cluster"}}
]

Resources:
{resources_json}"""

        response = await generate_content_with_fallback(
            prompt=prompt,
            workload=WorkloadProfile.SYNTHESIS,
            primary_model=CLASSIFICATION_MODEL,
            config={'response_mime_type': 'application/json'},
            require_json=True
        )

        discovered = response.parse_json()
        if not isinstance(discovered, list):
            return []

        created = []
        ist_ts = datetime.now(timezone(timedelta(hours=5, minutes=30)))
        for item in discovered:
            title = item.get('cluster_title', '').strip()
            resource_ids = item.get('resource_ids', [])
            if not title or len(resource_ids) < 3:
                continue
            norm = normalize_cluster_title(title)
            if not norm or norm in existing_titles:
                continue
            description = item.get('description', f'Auto-discovered from {len(resource_ids)} related resources on {ist_ts.strftime("%Y-%m-%d")}.')
            insert_res = supabase.table('clusters').insert({
                "title": title,
                "status": "active",
                "description": description
            }).execute()
            if not insert_res.data:
                continue
            new_cluster_id = insert_res.data[0]['id']
            existing_titles.add(norm)
            supabase.table('resources').update({
                "cluster_id": new_cluster_id
            }).in_('id', resource_ids).execute()
            created.append(title)
            audit_log_sync("pulse", "INFO", f"🔗 Cluster discovery: created '{title}' with {len(resource_ids)} resources")

        if created:
            audit_log_sync("pulse", "INFO", f"✅ Cluster discovery created {len(created)} new clusters: {', '.join(created)}")
        else:
            audit_log_sync("pulse", "INFO", "📍 Cluster discovery: no new clusters found.")
        return created

    except Exception as e:
        audit_log_sync("pulse", "WARNING", f"⚠️ Cluster discovery error: {e}")
        return []
