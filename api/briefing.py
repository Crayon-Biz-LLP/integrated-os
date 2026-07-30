"""
Briefing builder — assembles the structured home-surface briefing.

Called from:
  GET  /api/briefing   → returns full briefing
  POST /api/send-message → returns response_text + briefing_update

Sections built:
  morning   → greeting + next event + urgent/active tasks
  decisions → pending graph nodes + edges (shows summary count)
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
    is_stale: bool | None         # True if pulse flagged this as overdue/stale

class BriefingSection(TypedDict):
    id: str
    title: str
    items: list[BriefingItem]

class TraceItem(TypedDict):
    time: str               # Human-readable time: "2m ago", "1h ago"
    input: str              # What the user said/asked (brief)
    resolution: str         # What happened / outcome

class DeltaItem(TypedDict):
    icon: str               # Emoji: "🆕", "✅", "🔴"
    text: str               # Human-readable description
    time: str               # Human-readable time: "2m ago", "1h ago"


class BriefingResponse(TypedDict):
    greeting: str
    next_event: str | None
    sections: list[BriefingSection]
    pending_count: int
    traces: list[TraceItem]  # For the Traces view
    latest_response: str | None  # Most recent bot response text
    # Pulse intelligence (from app_intelligence table)
    context_bar: str | None         # "Closing the loop — clear banking before sign-off"
    voice_line: str | None          # Rhodey's voice line (1-2 sentences)
    pulse_mode: str | None          # "morning", "afternoon", "closing_loop", "weekend", etc.
    insights: list[str]             # ["Banking is the main blocker", "2 tasks waiting on others"]
    vaulted_count: int              # Tasks hidden behind the pulse vault
    # Home screen mode (drives Flutter layout)
    home_mode: str                  # "proceed" | "decide" | "sprint" | "catch_up" | "wrap"
    vaulted_urgent_count: int       # Count of vaulted urgent tasks
    vaulted_high_count: int         # Count of vaulted high-priority tasks
    # Catch-up delta: what changed since the user was last active
    delta_items: list[DeltaItem]    # 🆕 New tasks, ✅ done tasks, 🔴 new decisions


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
    stale_task_names: set[str] | None = None,
    overdue_task_names: set[str] | None = None,
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
            is_stale=False,
        ))

    # — Tasks sorted by urgency —
    task_items: list[BriefingItem] = []
    stale_or_overdue = (stale_task_names or set()) | (overdue_task_names or set())
    for t in tasks:
        title = t.get("title", "").strip()
        if not title or title.startswith("http"):
            continue
        deadline_raw = t.get("deadline")
        is_active = t.get("status") in ("todo", None)
        is_stale = title.lower().strip() in stale_or_overdue

        if deadline_raw:
            dl = _parse_dt(deadline_raw)
            if dl is not None:
                if dl < now:
                    task_items.append(BriefingItem(
                        icon="⚠️",
                        text=f"Overdue: {title}",
                        status="urgent",
                        is_stale=is_stale,
                    ))
                elif dl < now + timedelta(hours=24):
                    time_left = int((dl - now).total_seconds() / 3600)
                    task_items.append(BriefingItem(
                        icon="⏰",
                        text=f"{title} — due in {time_left}h",
                        status="urgent",
                        is_stale=is_stale,
                    ))
                else:
                    date_str = f"{dl.day:02d}/{dl.month:02d}"
                    task_items.append(BriefingItem(
                        icon="📝",
                        text=f"{title} — due {date_str}",
                        status="active",
                        is_stale=is_stale,
                    ))
            elif is_active:
                task_items.append(BriefingItem(
                    icon="📝",
                    text=title,
                    status="active",
                    is_stale=is_stale,
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
    """Build the Decisions section. Returns a single summary item with count.

    Instead of listing every pending item, shows a concise summary:
      "📋 30 items awaiting your decision in Inbox"
    with a breakdown by type (graph nodes, edges, channels).
    """
    total = len(graph_nodes) + len(graph_edges) + len(channel_items)
    if total == 0:
        return None

    # Build breakdown
    parts: list[str] = []
    if graph_nodes:
        parts.append(f"{len(graph_nodes)} graph node{'s' if len(graph_nodes) != 1 else ''}")
    if graph_edges:
        parts.append(f"{len(graph_edges)} edge{'s' if len(graph_edges) != 1 else ''}")
    if channel_items:
        parts.append(f"{len(channel_items)} channel item{'s' if len(channel_items) != 1 else ''}")

    breakdown = ", ".join(parts)

    text = f"\uD83D\uDCCB {total} item{'s' if total != 1 else ''} awaiting your decision"
    detail = f"({breakdown}) — review in Inbox"

    items: list[BriefingItem] = [
        BriefingItem(
            icon="\uD83D\uDCCB",
            text=f"{text}. {detail}",
            status="pending",
            decision_id=None,
            decision_type=None,
        ),
    ]

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

        if direction == "incoming":
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
                .select("id, title, status, priority, deadline, reminder_at, created_at, completed_at, updated_at")\
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
            res = supabase.table("pending_nodes")\
                .select("id, label, type:node_type, status, eval_context")\
                .in_("status", ["pending", "flagged"])\
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
                .select("id, source_label, target_label, relationship, status")\
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
                .order("created_at", desc=False)\
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

    # ── Horizon guard: filter far-future tasks ──
    async def _filter_horizon(tasks_raw: list[dict]) -> list[dict]:
        """Remove tasks with deadline/reminder more than 2 days out."""
        horizon_cutoff = datetime.now(IST) + timedelta(days=2)
        filtered = []
        for t in tasks_raw:
            deadline = t.get('deadline')
            reminder = t.get('reminder_at')
            future_date = None
            if deadline:
                dt = _parse_dt(deadline)
                if dt and dt > horizon_cutoff:
                    future_date = dt
            if reminder and not future_date:
                dt = _parse_dt(reminder)
                if dt and dt > horizon_cutoff:
                    future_date = dt
            if future_date:
                continue  # Skip tasks more than 2 days in the future
            filtered.append(t)
        return filtered

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

    # ── Compute vault segmentation BEFORE horizon filter ──
    # raw_tasks is the unfiltered list, tasks will be filtered below
    raw_tasks_before_filter = list(tasks)
    horizon_task_ids = set()
    vaulted_urgent = 0
    vaulted_high = 0
    for t in raw_tasks_before_filter:
        tid = t.get("id")
        if tid:
            horizon_task_ids.add(str(tid))

    # ── Apply horizon guard to tasks ──
    tasks = await _filter_horizon(tasks)

    # ── Compute delta items (since last pulse) for catch_up mode ──
    # Collect raw items with timestamps, sort globally by recency
    delta_items: list[DeltaItem] = []
    try:
        # Find the last pulse timestamp (reuse ai_res result computed later)
        # But we need it now for the delta query — read it here
        last_pulse_at = (datetime.now(IST) - timedelta(hours=6)).isoformat()
        ai_ts_res = supabase.table("app_intelligence") \
            .select("created_at") \
            .order("created_at", desc=True) \
            .limit(1) \
            .execute()
        if ai_ts_res.data:
            last_pulse_at = ai_ts_res.data[0]["created_at"]

        now = datetime.now(IST)
        raw_deltas: list[tuple[datetime, str, str]] = []  # (timestamp, icon, text)

        # Tasks created since last pulse
        new_tasks_res = supabase.table("tasks") \
            .select("title, created_at") \
            .eq("is_current", True) \
            .gte("created_at", last_pulse_at) \
            .order("created_at", desc=True) \
            .limit(15) \
            .execute()
        for t in new_tasks_res.data or []:
            title = t.get("title", "").strip()
            if not title or title.startswith("http"):
                continue
            created_raw = t.get("created_at", "")
            created_dt = _parse_dt(created_raw)
            if created_dt is None:
                continue
            raw_deltas.append((created_dt, "\U0001F195", f"New: {title}"))  # 🆕

        # Tasks completed since last pulse
        done_tasks_res = supabase.table("tasks") \
            .select("title, completed_at") \
            .eq("is_current", True) \
            .eq("status", "done") \
            .gte("completed_at", last_pulse_at) \
            .order("completed_at", desc=True) \
            .limit(15) \
            .execute()
        for t in done_tasks_res.data or []:
            title = t.get("title", "").strip()
            if not title:
                continue
            done_raw = t.get("completed_at", "")
            done_dt = _parse_dt(done_raw)
            if done_dt is None:
                continue
            raw_deltas.append((done_dt, "\u2705", f"Done: {title}"))  # ✅

        # New pending decisions since last pulse
        new_edges_res = supabase.table("pending_graph_edges") \
            .select("source_label, target_label, relationship, created_at") \
            .gte("created_at", last_pulse_at) \
            .order("created_at", desc=True) \
            .limit(10) \
            .execute()
        for e in new_edges_res.data or []:
            src = (e.get("source_label") or "?").strip()
            tgt = (e.get("target_label") or "?").strip()
            rel = (e.get("relationship") or "relates_to").strip()
            created_raw = e.get("created_at", "")
            created_dt = _parse_dt(created_raw)
            if created_dt is None:
                continue
            raw_deltas.append((created_dt, "\U0001F517", f"New edge: {src} → {rel} → {tgt}"))  # 🔗

        new_nodes_res = supabase.table("pending_nodes") \
            .select("label, node_type, created_at") \
            .gte("created_at", last_pulse_at) \
            .order("created_at", desc=True) \
            .limit(10) \
            .execute()
        for n in new_nodes_res.data or []:
            label = (n.get("label") or "").strip()
            ntype = (n.get("node_type") or "entity").strip()
            if not label:
                continue
            created_raw = n.get("created_at", "")
            created_dt = _parse_dt(created_raw)
            if created_dt is None:
                continue
            raw_deltas.append((created_dt, "\U0001F464", f"New {ntype}: {label}"))  # 👤

        # Sort globally by recency (newest first), then build DeltaItems
        raw_deltas.sort(key=lambda x: x[0], reverse=True)
        for ts, icon, text in raw_deltas[:20]:
            time_str = _human_time(ts, now)
            delta_items.append(DeltaItem(icon=icon, text=text, time=time_str))
    except Exception as e:
        print(f"[Briefing] Delta items error (non-critical): {e}")
        delta_items = []

    # ── Now compute which of the raw tasks didn't make the cut for vault segmentation ──
    filtered_ids = {str(t.get("id")) for t in tasks if t.get("id")}
    vaulted_total = len(horizon_task_ids) - len(filtered_ids)
    for t in raw_tasks_before_filter:
        tid = str(t.get("id"))
        if tid not in filtered_ids:
            p = t.get("priority", "")
            if p == "urgent":
                vaulted_urgent += 1
            elif p == "high":
                vaulted_high += 1

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

    # ── Read latest pulse intelligence (from app_intelligence table) ──
    stale_task_names: set[str] = set()
    overdue_task_names: set[str] = set()
    pulse_mode = None
    insight_text = ""
    vaulted_count = 0
    voice_line = None
    home_mode = "proceed"
    try:
        ai_res = supabase.table("app_intelligence")\
            .select("voice_line, pulse_mode, nag_list, stale_list, overdue_list, vaulted_count, context, insights, home_mode")\
            .order("created_at", desc=True)\
            .limit(1)\
            .execute()
        if ai_res.data:
            row = ai_res.data[0]
            voice_line = row.get("voice_line") or None
            pulse_mode = row.get("pulse_mode", "")
            insight_text = row.get("context", "")
            vaulted_count = row.get("vaulted_count") or 0
            home_mode = row.get("home_mode") or "proceed"
            raw_overdue = row.get("overdue_list") or []
            raw_stale = row.get("stale_list") or []
            if raw_overdue:
                overdue_task_names = set(t.lower().strip() for t in raw_overdue if isinstance(t, str))
            if raw_stale:
                stale_task_names = set(t.lower().strip() for t in raw_stale if isinstance(t, str))
    except Exception as e:
        print(f"[Briefing] App intelligence error (table may not exist yet): {e}")

    # ── Fallback: read from raw_dumps metadata if app_intelligence returned empty ──
    if not voice_line and not insight_text:
        try:
            fb_res = supabase.table("raw_dumps")\
                .select("metadata")\
                .eq("source", "pulse_engine")\
                .eq("message_type", "pulse_briefing")\
                .eq("status", "completed")\
                .order("created_at", desc=True)\
                .limit(1)\
                .execute()
            if fb_res.data:
                meta = fb_res.data[0].get("metadata") or {}
                pulse_mode = pulse_mode or meta.get("briefing_mode", "")
                insight_text = insight_text or meta.get("insight", "")
                vaulted_count = vaulted_count or (meta.get("vaulted_count") or 0)
                raw_overdue_fb = meta.get("overdue_tasks", []) or []
                raw_stale_fb = meta.get("stale_tasks", []) or []
                if not overdue_task_names and raw_overdue_fb:
                    overdue_task_names = set(t.lower().strip() for t in raw_overdue_fb if isinstance(t, str))
                if not stale_task_names and raw_stale_fb:
                    stale_task_names = set(t.lower().strip() for t in raw_stale_fb if isinstance(t, str))
        except Exception as e:
            print(f"[Briefing] Fallback pulse read error: {e}")

    # 1. Briefing block (with pulse intelligence)
    briefing_section = _build_briefing_section(
        tasks, events,
        stale_task_names=stale_task_names,
        overdue_task_names=overdue_task_names,
    )
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

    # 4. Latest response text (from raw_dumps outgoing)
    latest_response = None
    try:
        lr_res = supabase.table("raw_dumps")\
            .select("content, created_at")\
            .eq("direction", "outgoing")\
            .eq("source", "telegram_bot")\
            .eq("status", "completed")\
            .order("created_at", desc=True)\
            .limit(1)\
            .execute()
        if lr_res.data:
            content = lr_res.data[0].get("content", "")
            created_raw = lr_res.data[0].get("created_at", "")
            if content and created_raw:
                created_dt = _parse_dt(created_raw)
                if created_dt and (now - created_dt).total_seconds() < 3600:
                    latest_response = content[:300] + ("\u2026" if len(content) > 300 else "")
    except Exception as e:
        print(f"[Briefing] Latest response error: {e}")

    # 5. Traces block (for Traces view — pairs inputs with outcomes)
    traces = _build_traces(traces_msgs, traces_tasks)

    # ── Build pulse insights ──
    insights_list: list[str] = []
    if overdue_task_names:
        insights_list.append(f"🔴 {len(overdue_task_names)} stale task{'s' if len(overdue_task_names) != 1 else ''} — needs attention")
    if vaulted_count > 0:
        insights_list.append(f"📦 {vaulted_count} item{'s' if vaulted_count != 1 else ''} vaulted behind the pulse")
    if pulse_mode:
        mode = pulse_mode.lower()
        # Map actual pulse mode strings to insights labels
        if 'morning' in mode:
            insights_list.insert(0, '☀️ Morning focus — move the needle today')
        elif 'afternoon' in mode:
            insights_list.insert(0, '🌤️ Afternoon check — keep building')
        elif 'closing' in mode or 'sign off' in mode:
            insights_list.insert(0, '🌇 Closing the loop — wrap up before sign-off')
        elif 'weekend' in mode or 'chores' in mode:
            insights_list.insert(0, '🌿 Weekend mode — rest and recharge')
        elif 'pre-monday' in mode:
            insights_list.insert(0, '📈 Pre-Monday — loading the board')
        elif 'intel' in mode or 'vaulted' in mode:
            insights_list.insert(0, '🌙 Intel: Vaulted — secure the board')

    # Build context bar from pulse insight
    context_bar = None
    if insight_text:
        clean = insight_text.strip().rstrip('.')
        if overdue_task_names:
            clean += f" · {len(overdue_task_names)} stale"
        if vaulted_count:
            clean += f" · {vaulted_count} vaulted"
        context_bar = clean[:120]

    return BriefingResponse(
        greeting=greeting_line,
        next_event=next_event,
        sections=sections,
        pending_count=pending_count,
        traces=traces,
        latest_response=latest_response,
        context_bar=context_bar,
        voice_line=voice_line,
        pulse_mode=pulse_mode,
        insights=insights_list,
        vaulted_count=vaulted_count,
        home_mode=home_mode,
        vaulted_urgent_count=vaulted_urgent,
        vaulted_high_count=vaulted_high,
        vaulted_count=vaulted_total,
        delta_items=delta_items,
    )
