"""B3: Pre-computed Entity Briefs.

Maintains compressed ~400-char status snapshots for active entities.
Refreshed every 5 minutes via sentinel piggyback. Read by the query
path after anaphora resolution — if a fresh brief exists, it replaces
the entire 17-section context assembly for entity-anchored queries.

Key functions:
  select_entities_to_refresh() — picks top ~10 active entities by activity score
  refresh_entity_brief() — fetches current state, compresses via flash-lite
  get_entity_brief() — reads from entity_briefs table
  refresh_top_entity_briefs() — orchestrator for sentinel piggyback
"""

import asyncio
from datetime import datetime, timezone, timedelta

from core.services.db import get_supabase
from core.lib.audit_logger import audit_log_sync
from core.llm.fallback import generate_content_with_fallback
from core.llm.config import WorkloadProfile
from core.llm.constants import CLASSIFICATION_MODEL
from core.prompts.voice import RHODEY_VOICE

MAX_ENTITIES_PER_CYCLE = 10
BRIEF_FRESHNESS_MINUTES = 5


def select_entities_to_refresh(
    supabase, max_entities: int = MAX_ENTITIES_PER_CYCLE
) -> list[dict]:
    """Pick the top ~N active entities to refresh briefs for.

    Scores entities by:
      3 pts — entity name appears in open task titles (active engagement)
      2 pts — entity was discussed in last 24h (from conversation_threads)
      1 pt  — entity is an org/project with open tasks (likely to be queried)

    Returns list of {"name": str, "type": str, "score": int} sorted by score desc.
    """
    now = datetime.now(timezone.utc)
    scores = {}  # entity_name_lower -> {"name": str, "type": str, "score": int}

    def _add_score(name: str, etype: str, points: int):
        key = name.lower().strip()
        if not key or len(key) < 2:
            return
        if key not in scores:
            scores[key] = {"name": name, "type": etype, "score": 0}
        scores[key]["score"] += points

    # 1. Open tasks — extract entity names from titles (3 pts)
    try:
        tasks_res = supabase.table("tasks") \
            .select("id, title, project_id, organization_id") \
            .eq("is_current", True) \
            .not_.in_("status", ["done", "cancelled"]) \
            .limit(200) \
            .execute()
        for t in (tasks_res.data or []):
            title_words = t.get("title", "").split()
            # If title starts with a capitalized word that looks like an entity name
            if title_words:
                first = title_words[0]
                if first[0].isupper() and len(first) > 2:
                    _add_score(first, "project", 3)
    except Exception as e:
        audit_log_sync("entity_briefs", "WARNING", f"select: open tasks query failed: {e}")

    # 2. Projects and orgs with open tasks — always include (1 pt)
    try:
        proj_res = supabase.table("projects") \
            .select("id, name, organization_id") \
            .eq("status", "active") \
            .eq("is_current", True) \
            .execute()
        for p in (proj_res.data or []):
            _add_score(p["name"], "project", 1)

        org_res = supabase.table("organizations") \
            .select("id, name") \
            .eq("is_active", True) \
            .execute()
        for o in (org_res.data or []):
            _add_score(o["name"], "organization", 1)
    except Exception as e:
        audit_log_sync("entity_briefs", "WARNING", f"select: projects/orgs query failed: {e}")

    # 3. Recent conversation threads — entities discussed in last 24h (2 pts)
    try:
        cutoff = (now - timedelta(hours=24)).isoformat()
        threads_res = supabase.table("conversation_threads") \
            .select("entity_label, entity_type") \
            .gt("last_active_at", cutoff) \
            .is_("archived_at", "null") \
            .neq("entity_label", "") \
            .not_.is_("entity_label", "null") \
            .limit(50) \
            .execute()
        for t in (threads_res.data or []):
            _add_score(t["entity_label"], t.get("entity_type") or "entity", 2)
    except Exception as e:
        audit_log_sync("entity_briefs", "WARNING", f"select: threads query failed: {e}")

    # Sort by score, return top N
    ranked = sorted(scores.values(), key=lambda x: x["score"], reverse=True)
    return ranked[:max_entities]


async def refresh_entity_brief(entity_name: str, entity_type: str) -> bool:
    """Fetch current state for a single entity and store a compressed brief.

    Queries 5 data sources in parallel:
      1. Open tasks mentioning entity
      2. Recently completed tasks (last 7d)
      3. Graph edges from entity's graph node
      4. Recent conversations mentioning entity
      5. Pending delegations/waiting_on

    Compresses into ~400-char brief via flash-lite, writes to entity_briefs table.
    Returns True if brief was stored, False on failure.
    """
    supabase = get_supabase()
    now = datetime.now(timezone.utc)

    try:
        # Step 1: Fetch all 5 data sources in parallel
        search_val = entity_name.lower().strip()

        async def _fetch_open_tasks():
            try:
                res = supabase.table("tasks") \
                    .select("id, title, status, priority, created_at") \
                    .eq("is_current", True) \
                    .not_.in_("status", ["done", "cancelled"]) \
                    .ilike("title", f"%{search_val}%") \
                    .order("created_at", desc=True) \
                    .limit(10) \
                    .execute()
                return res.data or []
            except Exception:
                return []

        async def _fetch_completed_tasks():
            try:
                seven_days_ago = (now - timedelta(days=7)).isoformat()
                res = supabase.table("tasks") \
                    .select("id, title") \
                    .eq("is_current", True) \
                    .eq("status", "done") \
                    .gte("updated_at", seven_days_ago) \
                    .ilike("title", f"%{search_val}%") \
                    .limit(5) \
                    .execute()
                return res.data or []
            except Exception:
                return []

        async def _fetch_graph_edges():
            try:
                node_res = supabase.table("graph_nodes") \
                    .select("id") \
                    .ilike("label", search_val) \
                    .eq("is_current", True) \
                    .limit(1) \
                    .execute()
                if not node_res or not node_res.data:
                    return []
                node_id = node_res.data[0]["id"]
                edge_res = supabase.table("graph_edges") \
                    .select("source_node_id, target_node_id, relationship_type") \
                    .or_(f"source_node_id.eq.{node_id},target_node_id.eq.{node_id}") \
                    .limit(10) \
                    .execute()
                if not edge_res.data:
                    return []

                # Resolve labels for connected nodes
                peer_ids = set()
                for e in edge_res.data:
                    peer_ids.add(e["source_node_id"])
                    peer_ids.add(e["target_node_id"])
                peer_ids.discard(node_id)

                peer_res = supabase.table("graph_nodes") \
                    .select("id, label") \
                    .in_("id", list(peer_ids)) \
                    .execute()
                label_map = {n["id"]: n["label"] for n in (peer_res.data or [])}

                result = []
                for e in edge_res.data:
                    src = label_map.get(e["source_node_id"], "?")
                    tgt = label_map.get(e["target_node_id"], "?")
                    rel = e.get("relationship_type", "related_to")
                    result.append(f"{src} {rel} {tgt}")
                return result
            except Exception:
                return []

        async def _fetch_recent_conversations():
            try:
                cutoff = (now - timedelta(hours=24)).isoformat()
                res = supabase.table("conversations") \
                    .select("content, created_at") \
                    .eq("role", "user") \
                    .gte("created_at", cutoff) \
                    .ilike("content", f"%{search_val}%") \
                    .order("created_at", desc=True) \
                    .limit(3) \
                    .execute()
                return res.data or []
            except Exception:
                return []

        async def _fetch_pending_delegations():
            try:
                res = supabase.table("tasks") \
                    .select("id, title, committed_to") \
                    .eq("is_current", True) \
                    .eq("status", "todo") \
                    .eq("direction", "waiting_on") \
                    .ilike("title", f"%{search_val}%") \
                    .not_.is_("committed_to", "null") \
                    .limit(5) \
                    .execute()
                return res.data or []
            except Exception:
                return []

        all_results = await asyncio.gather(
            _fetch_open_tasks(),
            _fetch_completed_tasks(),
            _fetch_graph_edges(),
            _fetch_recent_conversations(),
            _fetch_pending_delegations(),
        )

        open_tasks, completed_tasks, graph_edges, recent_convs, delegations = all_results

        # Step 2: Build state summary
        open_task_count = len(open_tasks)
        completed_count = len(completed_tasks)
        open_task_lines = []
        for t in open_tasks[:3]:
            pri = f" ({t.get('priority', '')})" if t.get('priority') else ""
            open_task_lines.append(f"- {t['title']}{pri}")
        open_task_str = "\n".join(open_task_lines) if open_task_lines else "None"

        edge_str = "; ".join(graph_edges[:5]) if graph_edges else "None"
        conv_str = ""
        if recent_convs:
            most_recent = recent_convs[0].get("created_at", "")
            if most_recent:
                try:
                    dt = datetime.fromisoformat(str(most_recent).replace("Z", "+00:00"))
                    mins_ago = int((now - dt).total_seconds() / 60)
                    if mins_ago < 60:
                        conv_str = f"Discussed {mins_ago}min ago"
                    else:
                        conv_str = f"Discussed {mins_ago // 60}h ago"
                except Exception:
                    conv_str = "Recently discussed"

        del_str = ""
        if delegations:
            names = [d.get("committed_to", "someone") for d in delegations[:2]]
            del_str = f"Waiting on {', '.join(names)}"

        # Step 3: Compress via flash-lite — in Rhodey's voice, human-readable
        brief_prompt = f"""{RHODEY_VOICE}

Current state for "{entity_name}" ({entity_type}):
- {open_task_count} open tasks:\n{open_task_str}
- {completed_count} completed last 7d\n{('' + chr(10) + chr(10).join(['- ' + t['title'] for t in completed_tasks[:3]]) + chr(10)) if completed_tasks else ''}
- Relationships: {edge_str}
- {conv_str or 'No recent conversations'}
{('- ' + del_str) if del_str else ''}

Write a brief update for Danny answering "what's happening with {entity_name}?". Start with the open task count. List top items naturally. Mention relationships or pending items if notable. Keep it under 400 characters — concise but human. Output ONLY the update — no labels, no JSON, no markdown."""

        res = await generate_content_with_fallback(
            prompt=brief_prompt,
            workload=WorkloadProfile.INTERACTIVE,
            primary_model=CLASSIFICATION_MODEL,
        )
        brief_text = (res.text or "").strip() if res else ""
        if not brief_text or len(brief_text) < 50:
            # Fallback: build a simple text brief without LLM
            parts = [f"{open_task_count} open task(s)"]
            if open_tasks:
                parts.append(f"top: {open_tasks[0]['title'][:60]}")
            if edge_str != "None":
                parts.append(f"relations: {edge_str[:80]}")
            if conv_str:
                parts.append(conv_str)
            brief_text = f"{entity_name}: {', '.join(parts)}"

        # Truncate to 600 chars max
        brief_text = brief_text[:600]

        # Step 4: Store in entity_briefs table
        supabase.table("entity_briefs").upsert({
            "entity_name": entity_name.lower(),
            "entity_type": entity_type,
            "brief_text": brief_text,
            "open_task_count": open_task_count,
            "updated_at": now.isoformat(),
        }, on_conflict="entity_name").execute()

        audit_log_sync("entity_briefs", "INFO",
            f"Refreshed brief for {entity_name} ({entity_type}): "
            f"{open_task_count} tasks, {completed_count} completed, "
            f"{len(graph_edges)} edges")
        return True

    except Exception as e:
        audit_log_sync("entity_briefs", "WARNING",
            f"Failed to refresh brief for {entity_name}: {e}")
        return False


def get_entity_brief(entity_name: str, max_stale_minutes: int = BRIEF_FRESHNESS_MINUTES) -> dict | None:
    """Read a fresh brief from the entity_briefs table.

    Returns dict with brief_text, entity_type, updated_at, open_task_count
    or None if no fresh brief exists.
    """
    if not entity_name or entity_name.lower() == "none":
        return None
    try:
        supabase = get_supabase()
        res = supabase.table("entity_briefs") \
            .select("entity_name, entity_type, brief_text, open_task_count, updated_at") \
            .eq("entity_name", entity_name.lower()) \
            .limit(1) \
            .execute()
        if not res or not res.data:
            return None
        row = res.data[0]
        updated = row.get("updated_at")
        if updated:
            try:
                updated_dt = datetime.fromisoformat(str(updated).replace("Z", "+00:00"))
                age_min = (datetime.now(timezone.utc) - updated_dt).total_seconds() / 60
                if age_min > max_stale_minutes:
                    return None  # Too stale
            except Exception:
                pass
        return row
    except Exception as e:
        audit_log_sync("entity_briefs", "WARNING",
            f"Failed to read brief for {entity_name}: {e}")
        return None


async def refresh_top_entity_briefs(max_entities: int = MAX_ENTITIES_PER_CYCLE) -> int:
    """Orchestrator for sentinel piggyback. Picks top entities and refreshes.

    Runs as a background piggyback — all errors are caught and logged.
    Returns number of briefs refreshed.
    """
    supabase = get_supabase()
    now = datetime.now(timezone.utc)

    # Check throttle: only run if >3 min since last entity briefs cycle
    try:
        last_run = supabase.table("audit_logs") \
            .select("id") \
            .eq("service", "entity_briefs") \
            .ilike("message", "entity_briefs cycle%") \
            .gte("created_at", (now - timedelta(minutes=3)).isoformat()) \
            .limit(1) \
            .execute()
        if last_run and last_run.data:
            return 0  # Skip — too soon
    except Exception:
        pass  # Fail-open: run anyway

    try:
        entities = select_entities_to_refresh(supabase, max_entities)
        if not entities:
            audit_log_sync("entity_briefs", "INFO", "entity_briefs cycle: no entities to refresh")
            return 0

        refreshed = 0
        for entity in entities:
            success = await refresh_entity_brief(entity["name"], entity["type"])
            if success:
                refreshed += 1

        audit_log_sync("entity_briefs", "INFO",
            f"entity_briefs cycle: refreshed {refreshed}/{len(entities)} briefs "
            f"(top: {[e['name'] for e in entities[:3]]})")
        return refreshed

    except Exception as e:
        audit_log_sync("entity_briefs", "WARNING",
            f"entity_briefs cycle error: {e}")
        return 0
