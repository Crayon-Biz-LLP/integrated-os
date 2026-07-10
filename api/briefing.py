"""
Briefing builder — assembles the structured home-surface briefing.

Called from:
  GET  /api/briefing   → returns full briefing
  POST /api/send-message → returns response_text + briefing_update

Sections built:
  morning   → greeting + next event + urgent/active tasks
  decisions → pending graph nodes + edges (omitted if empty)
  recent    → last few completed outcomes (max 3 items, last 30 min)
  traces    → paired input→outcome history (for Traces view)
"""

import os
from datetime import datetime, timedelta, timezone
from typing import TypedDict


# ── Typed dicts ──────────────────────────────────────────────────────────────

class BriefingItem(TypedDict):
    icon: str
    text: str
    status: str  # "urgent", "active", "pending", "done", "note"
    decision_id: str | None       # Pending item ID (for decision actions)
    decision_type: str | None     # "graph_node", "graph_edge", "email", "whatsapp", "call", "merge"

class BriefingSection(TypedDict):
    id: str
    title: str
    items: list[BriefingItem]

class TraceItem(TypedDict):
    time: str               # Human-readable time: "2m ago", "1h ago"
    input: str              # What the user said/asked (brief)
    resolution: str         # What happened / outcome

class BriefingResponse(TypedDict):
    greeting: str
    next_event: str | None
    sections: list[BriefingSection]
    pending_count: int
    traces: list[TraceItem]  # For the Traces view


# ── Helpers ──────────────────────────────────────────────────────────────────

IST = timezone(timedelta(hours=5, minutes=30))
ELLIPSIS = "\u2026"


# ── Greeting ─────────────────────────────────────────────────────────────────

def _greeting() -> str:
    now = datetime.now(IST)
    h = now.hour
    if h < 12:
        return "Good morning"
    if h < 17:
        return "Good afternoon"
    return "Good evening"


# ── Helpers ──────────────────────────────────────────────────────────────────

def _human_time(dt: datetime, now: datetime) -> str:
    """Human-readable relative time string."""
    delta = now - dt
    if delta.total_seconds() < 60:
        return "Just now"
    if delta.total_seconds() < 3600:
        mins = int(delta.total_seconds() / 60)
        return f"{mins}m ago"
    if delta.total_seconds() < 86400:
        hours = int(delta.total_seconds() / 3600)
        return f"{hours}h ago"
    days = int(delta.total_seconds() / 86400)
    return f"{days}d ago"


def _parse_dt(raw: str) -> datetime | None:
    """Parse ISO datetime string to IST, returning None on failure."""
    try:
        return datetime.fromisoformat(raw).astimezone(IST)
    except (ValueError, TypeError):
        return None


# ── Section builders ─────────────────────────────────────────────────────────

def _build_briefing_section(
    tasks: list[dict],
    events: list[dict],
) -> BriefingSection:
    """Build the morning/evening section: calendar + tasks."""
    items: list[BriefingItem] = []

    now = datetime.now(IST)
    soon = now + timedelta(hours=6)

    # — Calendar events (next few hours) —
    for ev in events:
        start_raw = ev.get("start", {}).get("dateTime", "")
        if not start_raw:
            continue
        start_dt = _parse_dt(start_raw)
        if start_dt is None:
            continue
        # Only show events within the next 6 hours
        if start_dt < now - timedelta(hours=1) or start_dt > soon:
            continue
        title = ev.get("summary", "Event").strip()
        time_str = f"{start_dt.hour:02d}:{start_dt.minute:02d}"
        is_within_30m = start_dt < now + timedelta(minutes=30)
        items.append(BriefingItem(
            icon="🔴" if is_within_30m else "📅",
            text=f"{title} at {time_str}",
            status="urgent" if is_within_30m else "active",
        ))

    # — Tasks sorted by urgency —
    task_items: list[BriefingItem] = []
    for t in tasks:
        title = t.get("title", "").strip()
        if not title or title.startswith("http"):
            continue
        deadline_raw = t.get("deadline")
        is_active = t.get("status") in ("todo", None)

        if deadline_raw:
            dl = _parse_dt(deadline_raw)
            if dl is not None:
                if dl < now:
                    task_items.append(BriefingItem(
                        icon="⚠️",
                        text=f"Overdue: {title}",
                        status="urgent",
                    ))
                elif dl < now + timedelta(hours=24):
                    time_left = int((dl - now).total_seconds() / 3600)
                    task_items.append(BriefingItem(
                        icon="⏰",
                        text=f"{title} — due in {time_left}h",
                        status="urgent",
                    ))
                else:
                    date_str = f"{dl.day:02d}/{dl.month:02d}"
                    task_items.append(BriefingItem(
                        icon="📝",
                        text=f"{title} — due {date_str}",
                        status="active",
                    ))
            elif is_active:
                task_items.append(BriefingItem(
                    icon="📝",
                    text=title,
                    status="active",
                ))
        elif is_active:
            task_items.append(BriefingItem(
                icon="📝",
                text=title,
                status="active",
            ))

    # Urgent tasks first, then active
    task_items.sort(key=lambda it: 0 if it["status"] == "urgent" else 1)
    items.extend(task_items)

    # Determine section title by time of day
    h = now.hour
    if h < 12:
        section_title = "Your morning"
    elif h < 17:
        section_title = "Your afternoon"
    else:
        section_title = "Your evening"

    return BriefingSection(
        id="briefing",
        title=section_title,
        items=items,
    )


def _build_decisions_section(
    graph_nodes: list[dict],
    graph_edges: list[dict],
    channel_items: list[dict],
) -> BriefingSection | None:
    """Build the Decisions section. Returns None if nothing is pending."""
    items: list[BriefingItem] = []

    # Graph nodes
    for gn in graph_nodes:
        label = gn.get("label", "Unknown")
        node_type = gn.get("type", "person")
        status = gn.get("status", "pending")
        if status not in ("pending", "flagged", "merge_proposed"):
            continue

        gn_id = str(gn.get("id", ""))

        if status == "merge_proposed":
            target = (gn.get("eval_context") or {}).get("linked_entity", "another node")
            items.append(BriefingItem(
                icon="🔀",
                text=f'Merge: "{label}" \u2192 "{target}"',
                status="pending",
                decision_id=gn_id,
                decision_type="merge",
            ))
        else:
            items.append(BriefingItem(
                icon="🔗",
                text=f'Add "{label}" as {node_type}?',
                status="pending",
                decision_id=gn_id,
                decision_type="graph_node",
            ))

    # Graph edges
    for ge in graph_edges:
        src = ge.get("source_label", "?")
        tgt = ge.get("target_label", "?")
        rel = ge.get("relationship", "relates_to")
        ctx = ge.get("context", "")
        label = f"{src} \u2192 {rel} \u2192 {tgt}"
        if ctx:
            label += f" ({ctx})"
        items.append(BriefingItem(
            icon="🔗",
            text=label,
            status="pending",
            decision_id=str(ge.get("id", "")),
            decision_type="graph_edge",
        ))

    # Channel items (email/whatsapp/call)
    for ci in channel_items:
        content = ci.get("content", "").strip()
        source = ci.get("source", "channel")
        if not content:
            continue
        source_type = source if source in ("email", "whatsapp", "call") else "channel"
        items.append(BriefingItem(
            icon="\uD83D\uDCE8",
            text=f"{content[:80]}{ELLIPSIS if len(content) > 80 else ''}",
            status="pending",
            decision_id=str(ci.get("id", "")),
            decision_type=source_type,
        ))

    if not items:
        return None

    return BriefingSection(
        id="decisions",
        title="Decisions",
        items=items,
    )


def _build_recent_section(
    recent_messages: list[dict],
    recent_tasks: list[dict],
) -> BriefingSection:
    """Build Recent section from the last ~30 min of activity. Max 3 items."""
    items: list[BriefingItem] = []
    now = datetime.now(IST)
    cutoff = now - timedelta(minutes=30)

    # Completed tasks
    for t in recent_tasks:
        if len(items) >= 3:
            break
        title = t.get("title", "").strip()
        if not title:
            continue
        completed_raw = t.get("completed_at") or t.get("updated_at", "")
        completed_dt = _parse_dt(completed_raw) or now
        if completed_dt < cutoff:
            continue
        items.append(BriefingItem(
            icon="\u2705",
            text=f"Done: {title}",
            status="done",
        ))

    # Recent messages (created items, notes)
    for m in recent_messages:
        if len(items) >= 3:
            break
        content = m.get("content", "").strip()
        if not content or content.startswith("http"):
            continue
        direction = m.get("direction", "")
        status = m.get("status", "")
        message_type = m.get("message_type", "")

        created_raw = m.get("created_at", "")
        created_dt = _parse_dt(created_raw) or now
        if created_dt < cutoff:
            continue

        # Outgoing (bot) responses that are confirmations
        if direction == "outgoing" and status == "completed":
            if any(word in content.lower() for word in ["created", "noted", "saved", "done", "\u2705"]):
                display = content[:100]
                if len(content) > 100:
                    display += "\u2026"
                items.append(BriefingItem(
                    icon="\u2705",
                    text=display,
                    status="done",
                ))
        # Inbound user notes
        elif direction == "inbound" and message_type == "note":
            items.append(BriefingItem(
                icon="\uD83D\uDCDD",
                text=f"Noted: {content[:80]}{ELLIPSIS if len(content) > 80 else ''}",
                status="note",
            ))

    # Fallback: if nothing recent, show a subtle prompt
    if not items:
        items.append(BriefingItem(
            icon="\uD83D\uDCAC",
            text="Speak or type to get started",
            status="note",
        ))

    return BriefingSection(
        id="recent",
        title="Recent",
        items=items[:3],
    )


def _build_traces(
    recent_messages: list[dict],
    recent_tasks: list[dict],
) -> list[TraceItem]:
    """Build traces from recent activity — pairs input with outcome.

    For the Traces view: shows a history of what the user asked and what
    changed as a result. Each trace has the original input (never the full
    text, always a brief summary) and the resolution (what Rhodey did).
    """
    traces: list[TraceItem] = []
    now = datetime.now(IST)
    cutoff = now - timedelta(hours=6)

    # Pair inbound messages with their responses
    # Messages are already sorted by created_at asc from the query
    inbound_queue: list[dict] = []
    for m in recent_messages:
        direction = m.get("direction", "")
        created_raw = m.get("created_at", "")
        created_dt = _parse_dt(created_raw)
        if created_dt is None or created_dt < cutoff:
            continue

        content = m.get("content", "").strip()
        if not content:
            continue

        if direction == "inbound":
            inbound_queue.append(m)
        elif direction == "outgoing" and inbound_queue:
            # Pair the latest inbound with this outgoing response
            inbound = inbound_queue.pop()
            in_content = inbound.get("content", "").strip()
            in_brief = in_content[:80] + ("\u2026" if len(in_content) > 80 else "")

            # Shorten the resolution
            out_brief = content[:120] + ("\u2026" if len(content) > 120 else "")

            traces.append(TraceItem(
                time=_human_time(created_dt, now),
                input=in_brief,
                resolution=out_brief,
            ))

    # Add completed tasks as traces (with no input — they were auto-processed)
    for t in recent_tasks:
        if len(traces) >= 20:
            break
        title = t.get("title", "").strip()
        if not title:
            continue
        completed_raw = t.get("completed_at") or t.get("updated_at", "")
        completed_dt = _parse_dt(completed_raw)
        if completed_dt is None or completed_dt < cutoff:
            continue
        traces.append(TraceItem(
            time=_human_time(completed_dt, now),
            input="(auto)",
            resolution=f"Completed: {title}",
        ))

    # Reverse to show most recent first (traces are built chronologically
    # since messages are processed in created_at ascending order)
    traces.reverse()
    return traces[:20]


# ── Main builder ─────────────────────────────────────────────────────────────

async def build_briefing(supabase) -> BriefingResponse:
    """Assemble the full briefing from Supabase data. All errors caught per-source."""
    # ── Gather data in parallel ──────────────────────────────────────────
    import asyncio

    async def _get_tasks():
        try:
            res = supabase.table("tasks")\
                .select("id, title, status, priority, deadline, created_at, completed_at, updated_at")\
                .eq("is_current", True)\
                .in_("status", ["todo"])\
                .order("created_at", desc=True)\
                .limit(30)\
                .execute()
            return list(res.data or [])
        except Exception as e:
            print(f"[Briefing] Tasks error: {e}")
            return []

    async def _get_events():
        try:
            from core.services.google_service import get_google_creds, format_rfc3339
            from googleapiclient.discovery import build

            today = datetime.now(IST)
            start_dt = today.replace(hour=0, minute=0, second=0)
            end_dt = start_dt.replace(hour=23, minute=59, second=59)
            rfc_start = format_rfc3339(start_dt)
            rfc_end = format_rfc3339(end_dt)

            service = build("calendar", "v3", credentials=get_google_creds())
            events_res = service.events().list(
                calendarId="primary",
                timeMin=rfc_start,
                timeMax=rfc_end,
                singleEvents=True,
                orderBy="startTime",
                maxResults=50,
            ).execute()
            return list(events_res.get("items", []))
        except Exception as e:
            print(f"[Briefing] Calendar error: {e}")
            return []

    async def _get_graph_nodes():
        try:
            res = supabase.table("pending_graph_nodes")\
                .select("id, label, type, status, eval_context")\
                .in_("status", ["pending", "flagged", "merge_proposed"])\
                .order("created_at", desc=True)\
                .limit(30)\
                .execute()
            return list(res.data or [])
        except Exception as e:
            print(f"[Briefing] Graph nodes error: {e}")
            return []

    async def _get_graph_edges():
        try:
            res = supabase.table("pending_graph_edges")\
                .select("id, source_label, target_label, relationship, status, context")\
                .in_("status", ["pending", "flagged"])\
                .limit(30)\
                .execute()
            return list(res.data or [])
        except Exception as e:
            print(f"[Briefing] Graph edges error: {e}")
            return []

    async def _get_channel_pending():
        try:
            res = supabase.table("raw_dumps")\
                .select("id, content, source, status, direction, created_at")\
                .in_("source", ["email", "whatsapp", "call"])\
                .eq("status", "pending")\
                .order("created_at", desc=True)\
                .limit(20)\
                .execute()
            return list(res.data or [])
        except Exception as e:
            print(f"[Briefing] Channel pending error: {e}")
            return []

    async def _get_recent_messages():
        try:
            recent_cutoff = (datetime.now(IST) - timedelta(minutes=30)).isoformat()
            res = supabase.table("raw_dumps")\
                .select("id, content, direction, status, message_type, created_at")\
                .gte("created_at", recent_cutoff)\
                .order("created_at", desc=True)\
                .limit(20)\
                .execute()
            return list(res.data or [])
        except Exception as e:
            print(f"[Briefing] Recent messages error: {e}")
            return []

    async def _get_recent_done_tasks():
        try:
            recent_cutoff = (datetime.now(IST) - timedelta(minutes=30)).isoformat()
            res = supabase.table("tasks")\
                .select("id, title, status, completed_at, updated_at")\
                .eq("is_current", True)\
                .eq("status", "done")\
                .gte("completed_at", recent_cutoff)\
                .order("completed_at", desc=True)\
                .limit(10)\
                .execute()
            return list(res.data or [])
        except Exception as e:
            print(f"[Briefing] Recent done tasks error: {e}")
            return []

    # Also fetch messages from the last 6 hours for traces
    async def _get_traces_messages():
        try:
            traces_cutoff = (datetime.now(IST) - timedelta(hours=6)).isoformat()
            res = supabase.table("raw_dumps")\
                .select("id, content, direction, status, message_type, created_at")\
                .gte("created_at", traces_cutoff)\
                .order("created_at", asc=True)\
                .limit(100)\
                .execute()
            return list(res.data or [])
        except Exception as e:
            print(f"[Briefing] Traces messages error: {e}")
            return []

    async def _get_traces_done_tasks():
        try:
            traces_cutoff = (datetime.now(IST) - timedelta(hours=6)).isoformat()
            res = supabase.table("tasks")\
                .select("id, title, status, completed_at, updated_at")\
                .eq("is_current", True)\
                .eq("status", "done")\
                .gte("completed_at", traces_cutoff)\
                .order("completed_at", desc=True)\
                .limit(30)\
                .execute()
            return list(res.data or [])
        except Exception as e:
            print(f"[Briefing] Traces done tasks error: {e}")
            return []

    tasks_fut = _get_tasks()
    events_fut = _get_events()
    gnodes_fut = _get_graph_nodes()
    gedges_fut = _get_graph_edges()
    channel_fut = _get_channel_pending()
    recent_msgs_fut = _get_recent_messages()
    recent_tasks_fut = _get_recent_done_tasks()
    traces_msgs_fut = _get_traces_messages()
    traces_tasks_fut = _get_traces_done_tasks()

    tasks, events, gnodes, gedges, channel_items, recent_msgs, recent_tasks, traces_msgs, traces_tasks = (
        await asyncio.gather(
            tasks_fut, events_fut, gnodes_fut, gedges_fut,
            channel_fut, recent_msgs_fut, recent_tasks_fut,
            traces_msgs_fut, traces_tasks_fut,
        )
    )

    # ── Assemble sections ────────────────────────────────────────────────
    greeting = _greeting()
    name = os.getenv("USER_NAME", "Danny")

    # Next event for greeting
    next_event: str | None = None
    now = datetime.now(IST)
    for ev in events:
        start_raw = ev.get("start", {}).get("dateTime", "")
        if not start_raw:
            continue
        start_dt = _parse_dt(start_raw)
        if start_dt is None:
            continue
        if start_dt > now - timedelta(minutes=30):
            time_str = f"{start_dt.hour:02d}:{start_dt.minute:02d}"
            title = ev.get("summary", "Event").strip()
            next_event = f"{title} at {time_str}"
            break

    sections: list[BriefingSection] = []

    # 1. Briefing block
    briefing_section = _build_briefing_section(tasks, events)
    sections.append(briefing_section)

    # 2. Decisions block (conditional — omitted if empty)
    decisions_section = _build_decisions_section(gnodes, gedges, channel_items)
    if decisions_section is not None:
        sections.append(decisions_section)

    # 3. Recent block (hard cap 3)
    recent_section = _build_recent_section(recent_msgs, recent_tasks)
    sections.append(recent_section)

    # Pending count for notification dots
    pending_count = len(gnodes) + len(gedges) + len(channel_items)

    greeting_line = f"{greeting}, {name}."
    if next_event:
        greeting_line += f" {next_event}."

    # 4. Traces block (for Traces view — pairs inputs with outcomes)
    traces = _build_traces(traces_msgs, traces_tasks)

    return BriefingResponse(
        greeting=greeting_line,
        next_event=next_event,
        sections=sections,
        pending_count=pending_count,
        traces=traces,
    )
