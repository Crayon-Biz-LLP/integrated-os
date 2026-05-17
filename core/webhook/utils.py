import os
import httpx
from datetime import datetime, timezone, timedelta
from supabase import create_client, Client
from google.oauth2.credentials import Credentials
from googleapiclient.discovery_cache import base
from core.lib.duplicate_guard import check_duplicate
from core.lib.audit_logger import audit_log_sync

supabase: Client = create_client(
    os.getenv("SUPABASE_URL"),
    os.getenv("SUPABASE_SERVICE_ROLE_KEY")
)


class MemoryCache(base.Cache):
    _cache = {}
    def get(self, url):
        return self._cache.get(url)
    def set(self, url, content):
        self._cache[url] = content

def get_google_creds():
    """Unified credential handshake for Google services."""
    return Credentials(
        None,
        refresh_token=os.getenv("GOOGLE_REFRESH_TOKEN"),
        client_id=os.getenv("GOOGLE_CLIENT_ID"),
        client_secret=os.getenv("GOOGLE_CLIENT_SECRET"),
        token_uri="https://oauth2.googleapis.com/token"
    )

def is_already_in_tasks_table(title: str) -> dict:
    """Check if a similar task already exists in the tasks table.
    Uses normalized exact match + anchor entity overlap (Jaccard-like).
    Fails open — always returns 'clear' on errors.

    Returns dict with keys: result ('block'|'flag'|'clear'), matched_id, matched_title, is_superset, ratio.
    """
    try:
        result = supabase.table('tasks')\
            .select('id, title')\
            .not_.in_('status', ['done', 'cancelled'])\
            .execute()
        tasks = result.data or []
        return check_duplicate(title, tasks)
    except Exception as e:
        audit_log_sync("webhook", "WARNING", f"Duplicate guard check failed (failing open): {e}")
        return {"result": "clear", "matched_id": None, "matched_title": None, "is_superset": False, "ratio": 0.0}


def is_recent_raw_dump(content: str, source: str) -> bool:
    """Check if identical content+source was inserted in the last 60 seconds.
    Used as idempotency guard against Telegram double-fires and user double-taps."""
    try:
        cutoff = (datetime.now(timezone.utc) - timedelta(seconds=60)).isoformat()
        dup = supabase.table('raw_dumps') \
            .select('id') \
            .eq('content', content) \
            .eq('source', source) \
            .gte('created_at', cutoff) \
            .limit(1) \
            .execute()
        if dup.data:
            print(f"Duplicate guard: Skipping '{content[:50]}...' — inserted within 60s")
            return True
        return False
    except Exception as e:
        audit_log_sync("webhook", "WARNING", f"Duplicate guard check failed (failing open): {e}")
        return False

async def get_recent_context(limit: int = 2) -> list:
    try:
        res = supabase.table('raw_dumps')\
            .select('content')\
            .eq('is_processed', False)\
            .order('created_at', desc=True)\
            .limit(limit)\
            .execute()
        return res.data if res.data else []
    except:
        return []

async def trigger_github_pulse() -> bool:
    """Trigger GitHub Actions workflow dispatch for pulse briefing."""
    try:
        github_token = os.getenv("GITHUB_TOKEN")
        if not github_token:
            print("ERROR: GITHUB_TOKEN not set")
            return False

        owner = os.getenv("GITHUB_OWNER", "Crayon-Biz-LLP")
        repo = os.getenv("GITHUB_REPO", "integrated-os")

        url = f"https://api.github.com/repos/{owner}/{repo}/dispatches"

        headers = {
            "Authorization": f"token {github_token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28"
        }

        payload = {
            "event_type": "trigger_pulse"
        }

        async with httpx.AsyncClient() as client:
            response = await client.post(url, json=payload, headers=headers, timeout=10)

            if response.status_code == 204:
                print("✓ GitHub Actions workflow triggered successfully")
                return True
            else:
                audit_log_sync("webhook", "ERROR", f"GitHub dispatch failed: {response.status_code}")
                return False

    except Exception as e:
        audit_log_sync("webhook", "ERROR", f"ERROR triggering GitHub pulse: {e}")
        return False

async def hybrid_search_graph(query: str) -> str:
    """Graph-first search: Find primary entity and its connections."""
    try:
        nodes_res = supabase.table('graph_nodes').select('id, label').ilike('label', f'%{query}%').limit(1).execute()

        if not nodes_res.data:
            return ""

        primary_node = nodes_res.data[0]
        primary_id = primary_node['id']

        edges_res = supabase.table('graph_edges').select('source_node_id, target_node_id, relationship').or_(f'source_node_id.eq.{primary_id},target_node_id.eq.{primary_id}').execute()

        if not edges_res.data:
            return ""

        connected_ids = set()

        for edge in edges_res.data:
            if edge['source_node_id'] == primary_id:
                connected_ids.add(edge['target_node_id'])
            elif edge['target_node_id'] == primary_id:
                connected_ids.add(edge['source_node_id'])

        if connected_ids:
            labels_res = supabase.table('graph_nodes').select('id, label').in_('id', list(connected_ids)).execute()
            label_map = {str(n['id']): n['label'] for n in labels_res.data}

            labeled_map = []
            for edge in edges_res.data:
                src_label = label_map.get(str(edge['source_node_id']), "Unknown")
                tgt_label = label_map.get(str(edge['target_node_id']), "Unknown")

                if edge['source_node_id'] == primary_id:
                    labeled_map.append(f"[{primary_node['label']}] -> [{edge['relationship']}] -> [{tgt_label}]")
                elif edge['target_node_id'] == primary_id:
                    labeled_map.append(f"[{src_label}] -> [{edge['relationship']}] -> [{primary_node['label']}]")

            return "\n".join(labeled_map)

        return ""

    except Exception as e:
        audit_log_sync("webhook", "ERROR", f"Hybrid search error: {e}")
        return ""

