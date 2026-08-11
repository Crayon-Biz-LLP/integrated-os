"""Stage C helpers — episode windows + graph knowledge for classify prompts.

An "episode" is the set of messages from the SAME chat (chat_id) back to the
last ≥30-minute silence gap, capped at 12 messages. This is the anti-mixing
mechanism: conversations separated by silence never share a window.

Graph knowledge injects who the people are (sender, participants, mentioned
names) so the classifier judges with context, not in a vacuum. All lookups
fail open — a resolver error must never block classification.
"""

from datetime import datetime, timezone

from core.lib.chat_split import split_chat_identity

EPISODE_GAP_MINUTES = 30
EPISODE_MAX_MESSAGES = 12

# Strip the automated "Mention Mirror"/"Translator" noise from window lines
_NOISE_PREFIXES = ("mention mirror:", "translator:", "bridge bot:", "@all ")


def _parse_ts(ts) -> datetime | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts)
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def fetch_episode(
    supabase,
    chat_id: str,
    before_ts: str,
    gap_minutes: int = EPISODE_GAP_MINUTES,
    max_messages: int = EPISODE_MAX_MESSAGES,
) -> list[dict]:
    """Fetch the episode window for a chat, newest-first, capped.

    Messages are pulled from the DB (they're already persisted with the chat
    key) and the window is cut at the last ≥gap silence boundary.
    """
    try:
        res = (
            supabase.table("messages")
            .select("sender_id, sender_name, body, received_at, classification")
            .eq("channel", "whatsapp")
            .eq("metadata->>chat_id", chat_id)
            .lt("received_at", before_ts)
            .order("received_at", desc=True)
            .limit(max_messages * 2)  # headroom before cutting at the gap
            .execute()
        )
    except Exception:
        return []
    rows = res.data or []
    if not rows:
        return []

    # Newest first; cut at the first gap ≥ gap_minutes
    window = []
    prev_ts: datetime | None = None
    for r in rows:
        ts = _parse_ts(r.get("received_at"))
        if prev_ts is not None and ts is not None:
            gap = (prev_ts - ts).total_seconds() / 60.0
            if gap >= gap_minutes:
                break  # silence boundary — don't reach into an older episode
        window.append(r)
        prev_ts = ts
        if len(window) >= max_messages:
            break
    return window


def format_episode_lines(episode: list[dict]) -> list[str]:
    """Render episode rows as prompt lines (oldest first, with participant+time)."""
    lines = []
    for r in reversed(episode):  # oldest first for readability
        ts = _parse_ts(r.get("received_at"))
        time_str = ts.strftime("%H:%M") if ts else "--:--"
        participant = (split_chat_identity(r.get("sender_id") or "").get("participant")
                       or r.get("sender_name") or "?")
        body = (r.get("body") or "").replace("\n", " ")[:180]
        # skip automated mirror noise in the window itself
        low = body.lower()
        if any(low.startswith(p) for p in _NOISE_PREFIXES):
            continue
        lines.append(f"[{time_str}] {participant}: {body}")
    return lines


def resolve_graph_knowledge(
    supabase,
    sender_name: str | None,
    chat_id: str,
    user_name: str | None = None,
) -> str:
    """Compact graph-knowledge lines for the classify prompt. Fail-open.

    Resolves the sender and chat against the knowledge graph. Uses the same
    resolver as query-time person resolution (relationship + alias aware).
    Returns an empty string when nothing resolves — the prompt must handle
    "unknown" gracefully (wrong context is worse than no context).
    """
    lines = []
    try:
        from core.lib.graph_rules import resolve_person_in_query

        # Sender/participant resolution (e.g. "Marcus Durai" → work contact)
        sender_ctx = None
        if sender_name:
            sender_name = sender_name.strip()
            if ":" not in sender_name:  # skip "Chat: Participant" composites
                try:
                    sender_ctx = resolve_person_in_query(sender_name)
                except Exception:
                    sender_ctx = None
        if sender_ctx and sender_ctx.get("label"):
            lines.append(f"- {sender_name} = {sender_ctx['label']} (known person)")

        # Chat/organization resolution — exact label match first, then
        # substring (avoid "CirroCraft" vs "CirroCraft Holdings" collisions).
        org_ctx = None
        try:
            exact_res = (
                supabase.table("graph_nodes")
                .select("label, type")
                .eq("type", "organization")
                .eq("is_current", True)
                .eq("label", chat_id)
                .limit(1)
                .execute()
            )
            if exact_res.data:
                org_ctx = exact_res.data[0]
            else:
                sub_res = (
                    supabase.table("graph_nodes")
                    .select("label, type")
                    .eq("type", "organization")
                    .eq("is_current", True)
                    .ilike("label", f"%{chat_id}%")
                    .limit(1)
                    .execute()
                )
                if sub_res.data:
                    org_ctx = sub_res.data[0]
        except Exception:
            org_ctx = None
        if org_ctx and org_ctx.get("label"):
            lines.append(f"- {chat_id} = organization in knowledge graph ({org_ctx['label']})")
    except Exception:
        return ""
    return "\n".join(lines)


def build_episode_prompt_section(
    supabase,
    chat_id: str,
    before_ts: str,
) -> str:
    """Build the EPISODE CONTEXT section for the classify prompt."""
    episode = fetch_episode(supabase, chat_id, before_ts)
    if not episode:
        return ""
    lines = format_episode_lines(episode)
    if not lines:
        return ""
    return "EPISODE CONTEXT (recent messages in this chat):\n" + "\n".join(lines)
