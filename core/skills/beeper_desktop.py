"""Phase B2 — Beeper Desktop API bridge (local Mac runner, ingest-only).

Syncs the user's Beeper stream via the OFFICIAL Beeper Desktop API
(`http://localhost:23373` — the local HTTP API inside Beeper Desktop, the
same one the `bdapi_...` tokens authenticate against). This is the
interim transport while a Matrix access token for `matrix.beeper.com`
remains unavailable: Beeper Desktop must be running on this Mac, and the
bridge only captures while the Mac is on.

For each tick it:

  - lists WhatsApp chats (paginated), skipping chats without new activity
    since their persisted per-chat cursor (`core_config` key
    `beeper_desktop_cursor:{chat_id}`),
  - pages each active chat's messages backward (`cursor` param) so a
    long Mac-off gap never loses messages, bounded by MAX_MESSAGES_PER_TICK,
  - detects the USER's own sends (`isSender: true` — the Desktop API
    exposes both directions natively, which is the exact gap the Matrix
    path was missing) and routes them through `record_outgoing_message()`
    — stored as direction='outgoing', danny_decision='responded', never
    surfaced, and fires the auto-resolve rule for the chat (stale-decision
    fix),
  - routes incoming messages through the same sieve → ask-detector → LLM
    → batch-RPC pipeline as the Matrix bridge (`process_whatsapp_message`).

Native Desktop API message ids are stored (scoped as
`{chat_id}:{message_id}` so per-chat id sequences cannot collide on the
`unique_channel_message` (channel, message_id) constraint) and dedup at
the DB level exactly like Matrix event ids.

Chat identity comes straight from the API (no Matrix state-event puzzle):
  - chat_key = chat title (matches the display-name keys the ingest
    pipeline writes, e.g. 'Dr Mallika', 'ACC Elders + Danny'),
  - sender phones resolved from participants (senderID → phoneNumber map),
    which also handles WhatsApp's newer `@whatsapp_lid-...` sender ids that
    the Matrix regex cannot parse,
  - is_group = chat type != 'single' (participant phone passed through).

Ingest-only by construction: nothing in this module sends or drafts.

Run via: `python -m core.skills.beeper_desktop` (one tick) — wired to a
launchd agent (scripts/beeper-desktop-sync.sh, StartInterval 60) so it
runs while the Mac is on, alongside Beeper Desktop.
"""

import asyncio
import os
from datetime import datetime, timedelta, timezone

import httpx

from core.lib.audit_logger import audit_log_sync
from core.services.db import (
    channel_tenant_scope,
    core_config_upsert,
    tenant_aware_client,
)

DESKTOP_API_URL = os.getenv("BEEPER_DESKTOP_API_URL", "http://localhost:23373")
CURSOR_KEY_PREFIX = "beeper_desktop_cursor"
MAX_CHATS_TOTAL = 2000        # safety cap on listing chats
MAX_CHATS_PER_TICK = 60       # chats visited per tick (cold-start backfill)
MAX_MESSAGES_PER_TICK = 500   # messages routed per tick (mirrors Matrix cap)
COLD_START_WINDOW_DAYS = 30   # backfill chats active within this window only

# ── Token resolution ────────────────────────────────────────────────────

def resolve_desktop_token() -> str | None:
    """Desktop API token: BEEPER_DESKTOP_TOKEN → BEEPER_MATRIX_TOKEN.

    Today the only token that exists is the `bdapi_...` Desktop API token,
    which historically lives in `.env` under BEEPER_MATRIX_TOKEN (it is
    NOT a Matrix token — that misnomer is the root cause of the bridge
    outage). BEEPER_DESKTOP_TOKEN lets us graduate the name; the fallback
    keeps the runner working with the current `.env`.
    """
    for name in ("BEEPER_DESKTOP_TOKEN", "BEEPER_MATRIX_TOKEN"):
        tok = os.getenv(name)
        if tok and tok.strip():
            return tok.strip()
    return None


# ── Desktop API client ─────────────────────────────────────────────────

class BeeperDesktopClient:
    """Minimal client for the Beeper Desktop API (localhost:23373)."""

    def __init__(self, token: str, base_url: str = DESKTOP_API_URL):
        self._token = token
        self._base = base_url.rstrip("/")

    async def __aenter__(self):
        self._client = httpx.AsyncClient(
            base_url=self._base,
            headers={"Authorization": f"Bearer {self._token}"},
            timeout=30.0,
        )
        return self

    async def __aexit__(self, *exc):
        await self._client.aclose()

    async def info(self) -> dict | None:
        """GET /v1/info — health + token check (401 → None)."""
        try:
            r = await self._client.get("/v1/info")
            if r.status_code == 200:
                return r.json() or {}
            audit_log_sync("beeper", "WARNING",
                           f"/v1/info HTTP {r.status_code}: {r.text[:200]}")
        except Exception as e:
            audit_log_sync("beeper", "WARNING", f"/v1/info failed: {e}")
        return None

    async def list_chats(self, cursor: str | None = None) -> dict | None:
        """GET /v1/chats — one page (newest-first), or None on failure."""
        params = {"cursor": cursor} if cursor else None
        try:
            r = await self._client.get("/v1/chats", params=params)
            if r.status_code == 200:
                return r.json() or {}
            audit_log_sync("beeper", "WARNING",
                           f"/v1/chats HTTP {r.status_code}: {r.text[:200]}")
        except Exception as e:
            audit_log_sync("beeper", "WARNING", f"/v1/chats failed: {e}")
        return None

    async def list_all_chats(self, stop_before: datetime | None = None) -> list[dict]:
        """All chats across pages (bounded by MAX_CHATS_TOTAL).

        The chats list is sorted newest-first by lastActivity, so when
        `stop_before` is given (the cold-start activity window) listing
        halts as soon as a page's NEWEST chat is older than it — every
        later page is older still and contains nothing visitable. That
        bounds listing to a few pages instead of walking thousands of
        stale chats every tick.
        """
        out: list[dict] = []
        cursor = None
        while len(out) < MAX_CHATS_TOTAL:
            page = await self.list_chats(cursor=cursor)
            if not page:
                break
            items = page.get("items") or []
            if not items:
                break
            if stop_before is not None:
                newest_in_page = max(
                    (_iso_dt(c.get("lastActivity"))
                     for c in items if c.get("lastActivity")),
                    default=None,
                )
                if newest_in_page is None or newest_in_page < stop_before:
                    break  # this page and everything older is stale
            out.extend(items)
            if not page.get("hasMore"):
                break
            cursor = page.get("oldestCursor")
        return out

    async def list_messages(self, chat_id: str,
                            cursor: str | None = None) -> dict | None:
        """GET /v1/chats/{chatID}/messages — newest-first page.

        `cursor` = a message sortKey; the API returns messages OLDER than
        it (verified live), so backward paging walks history.
        """
        params = {"cursor": cursor} if cursor else None
        try:
            r = await self._client.get(f"/v1/chats/{chat_id}/messages", params=params)
            if r.status_code == 200:
                return r.json() or {}
            audit_log_sync("beeper", "WARNING",
                           f"messages {chat_id[:30]} HTTP {r.status_code}: {r.text[:200]}")
        except Exception as e:
            audit_log_sync("beeper", "WARNING", f"messages {chat_id[:30]} failed: {e}")
        return None


# ── Chat identity ───────────────────────────────────────────────────────

def resolve_chat_meta(chat: dict) -> dict | None:
    """Map a Desktop API chat to the ingest pipeline's identity.

    Returns None for non-WhatsApp networks. Otherwise:
      {desktop_chat_id, chat_key, is_group, phone, phones, last_activity}
    where `phones` maps senderID → phoneNumber (handles the newer
    `@whatsapp_lid-...` sender ids the Matrix regex cannot parse).
    """
    if chat.get("accountID") != "whatsapp":
        return None
    participants = {
        p.get("id"): p
        for p in (chat.get("participants") or {}).get("items", [])
        if p.get("id")
    }
    phones = {
        pid: p.get("phoneNumber")
        for pid, p in participants.items()
        if p.get("phoneNumber")
    }
    contact_phone = next(
        (p.get("phoneNumber")
         for p in participants.values()
         if not p.get("isSelf") and p.get("phoneNumber")),
        None,
    )
    title = (chat.get("title") or "").strip()
    return {
        "desktop_chat_id": chat.get("id"),
        "chat_key": title or chat.get("id"),
        "is_group": chat.get("type") != "single",
        "phone": contact_phone,
        "phones": phones,
        "last_activity": chat.get("lastActivity"),
    }


def _sort_key(msg: dict) -> int:
    """Numeric message sortKey (monotonic per chat); 0 when unparseable."""
    try:
        return int(msg.get("sortKey") or 0)
    except (TypeError, ValueError):
        return 0


# ── Per-chat cursor (persisted) ────────────────────────────────────────

def _cursor_key(chat_id: str) -> str:
    return f"{CURSOR_KEY_PREFIX}:{chat_id}"


def _load_cursor(supabase, chat_id: str) -> dict | None:
    """Load the persisted per-chat cursor from core_config.

    core_config.content is a TEXT column — PostgREST returns it as a JSON
    string, NOT a dict. The old isinstance(content, dict) check was always
    False, so cursors NEVER loaded: every tick re-fetched each chat's newest
    page and re-routed the same messages (the ~230 "duplicates" per tick),
    and gap-free backfill (page_back=bool(cursor)) never engaged.
    """
    try:
        import json
        res = (
            supabase.table("core_config")
            .select("content")
            .eq("key", _cursor_key(chat_id))
            .limit(1)
            .maybe_single()
            .execute()
        )
        data = res.data if res else None
        if not data:
            return None
        content = data.get("content")
        if isinstance(content, dict):
            return dict(content)
        if isinstance(content, str) and content.strip():
            parsed = json.loads(content)
            return dict(parsed) if isinstance(parsed, dict) else None
    except Exception:
        pass
    return None


def _save_cursor(supabase, chat_id: str, content: dict) -> None:
    try:
        # .execute() is mandatory — supabase-py builds requests lazily and a
        # bare core_config_upsert(...) is a silent no-op (the Matrix bridge's
        # cursor/room-map saves had exactly this bug: zero rows ever written).
        core_config_upsert(supabase, {
            "key": _cursor_key(chat_id),
            "content": content,
        }).execute()
    except Exception as e:
        audit_log_sync("beeper", "WARNING", f"desktop cursor save failed: {e}")


# ── Visit filter ────────────────────────────────────────────────────────

def _iso_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def should_visit_chat(chat_last_activity: str | None, cursor: dict | None,
                      cutoff: datetime) -> bool:
    """True when a chat needs a tick.

    - No cursor → cold start: visit only if active within the cutoff window
      (bounds the first-run backfill to current context).
    - Cursor present → visit only when lastActivity advanced past what we
      last recorded (a user's own send updates lastActivity, so this
      captures the sent-message gap too). Missing cursor lastActivity
      (older cursor shape) → visit defensively.
    """
    last = _iso_dt(chat_last_activity)
    if cursor is None:
        if last is None:
            return False  # no activity signal — nothing to capture
        return last >= cutoff
    cursor_last = _iso_dt(cursor.get("lastActivity"))
    if cursor_last is None:
        return True
    if last is None:
        return False
    return last > cursor_last


# ── Message collection (gap-free backward paging) ──────────────────────

async def collect_new_messages(client: BeeperDesktopClient, chat_id: str,
                               cursor_sort_key: int,
                               page_back: bool = True) -> list[dict]:
    """Messages newer than the per-chat cursor, newest-first.

    - cursor present (page_back=True): pages backward via the API's `cursor`
      param until the cursor is crossed or history is exhausted, so a long
      Mac-off gap never loses messages.
    - cold start (page_back=False, no cursor yet): newest page ONLY — the
      bounded recent-events start (mirrors the Matrix bridge's initial
      sync); walking the full history here would grind through years of
      old messages and flood the decision pipeline.
    """
    collected: list[dict] = []
    page = await client.list_messages(chat_id)
    while page and page.get("items"):
        items = page["items"]
        collected.extend(m for m in items if _sort_key(m) > cursor_sort_key)
        oldest_in_page = _sort_key(items[-1])
        if not page.get("hasMore") or oldest_in_page <= cursor_sort_key or not page_back:
            break
        page = await client.list_messages(chat_id, cursor=items[-1].get("sortKey"))
        if not page or not page.get("items"):
            break
    return collected


# ── Message routing ─────────────────────────────────────────────────────

def _scoped_message_id(chat_id: str, msg: dict) -> str | None:
    """Native message id, scoped by chat so per-chat id sequences cannot
    collide on the unique_channel_message (channel, message_id) constraint."""
    mid = msg.get("id")
    return f"{chat_id}:{mid}" if mid else None


async def route_message(supabase, msg: dict, meta: dict) -> tuple[str, str | None]:
    """Route one Desktop API message through the ingest pipeline.

    Returns (status, reason): status ∈
    {'outgoing', 'incoming', 'ignored', 'duplicate', 'error'}.
    """
    text = str(msg.get("text") or "").strip()
    if not text:
        return "ignored", "no text"

    chat_id = meta["desktop_chat_id"]
    scoped_id = _scoped_message_id(chat_id, msg)
    ts_iso = msg.get("timestamp") or datetime.now(timezone.utc).isoformat()

    if msg.get("isSender"):
        # ── Outgoing: record + auto-resolve (the sent-message gap) ──
        try:
            from core.lib.ingest import record_outgoing_message

            result = await record_outgoing_message(
                chat_id=meta["chat_key"],
                source="whatsapp",
                body=text,
                received_at=ts_iso,
                tracking_id=scoped_id,
                summary=text[:500],
                metadata={
                    "desktop_chat_id": chat_id,
                    "phone": meta.get("phone"),
                    "desktop_message_id": msg.get("id"),
                },
            )
            status = result.get("status")
            if status == "filed":
                return "outgoing", None
            if status == "duplicate":
                return "duplicate", status
            return "error", f"record_outgoing_message: {result.get('reason')}"
        except Exception as e:
            return _classify_error(e)

    # ── Incoming: sieve → ask-detector → LLM → batch-RPC ──
    sender_phone = meta["phones"].get(msg.get("senderID"))
    if not sender_phone:
        return "ignored", "no sender phone"
    try:
        from core.skills.whatsapp_ingest import process_whatsapp_message

        result = await process_whatsapp_message(
            sender_name=msg.get("senderName") or meta["chat_key"],
            sender_phone=sender_phone,
            message_text=text,
            received_at=ts_iso,
            chat_id=meta["chat_key"],
            participant=sender_phone if meta["is_group"] else None,
            event_id=scoped_id,
        )
        status = result.get("status")
        if status in ("ignored", "duplicate"):
            return "ignored", status
        return "incoming", None
    except Exception as e:
        return _classify_error(e)


def _classify_error(e: Exception) -> tuple[str, str | None]:
    """Map an exception to a status: DB-level dedup (unique_channel_message,
    23505) is a duplicate, not an error — the pre-insert dedup queries use a
    24h window, so an OLD message re-delivered outside that window hits the
    constraint instead. That rejection is correct behavior, not a failure."""
    msg = str(e)
    if "23505" in msg or "duplicate key" in msg.lower():
        return "duplicate", "unique_channel_message"
    return "error", msg


# ── Per-chat processing ─────────────────────────────────────────────────

async def process_chat(supabase, client: BeeperDesktopClient, meta: dict,
                       cursor: dict | None, summary: dict) -> None:
    """Fetch + route new messages for one chat, then advance its cursor.

    Cursor advances to the sortKey of the NEWEST message actually routed
    this tick; if the per-tick budget cuts processing short, the remainder
    stays newer than the cursor and is picked up next tick (no gaps).
    """
    cursor_sort = int((cursor or {}).get("sortKey") or 0)
    collected = await collect_new_messages(client, meta["desktop_chat_id"],
                                           cursor_sort, page_back=bool(cursor))
    if not collected:
        return

    # Oldest-first so rows land chronologically (episodes stay sane).
    new_cursor_sort = cursor_sort
    for msg in reversed(collected):
        if summary["processed"] >= MAX_MESSAGES_PER_TICK:
            break
        status, reason = await route_message(supabase, msg, meta)
        summary["processed"] += 1
        if status == "outgoing":
            summary["outgoing"] += 1
        elif status == "incoming":
            summary["incoming"] += 1
        elif status == "duplicate":
            summary["duplicate"] += 1
        elif status == "error":
            summary["errors"] += 1
            audit_log_sync("beeper", "WARNING",
                           f"desktop msg {msg.get('id')} failed: {reason}")
        else:  # ignored
            summary["ignored"] += 1
        new_cursor_sort = max(new_cursor_sort, _sort_key(msg))

    _save_cursor(supabase, meta["desktop_chat_id"], {
        "sortKey": new_cursor_sort,
        "lastActivity": meta.get("last_activity"),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    })


# ── Top-level run ───────────────────────────────────────────────────────

async def run_desktop_sync() -> dict:
    """One bridge tick against the local Beeper Desktop API.

    Runs under the channel tenant scope (Danny's single account — the
    legacy single-user path the Matrix bridge used with the env token).
    Fail-open: any per-chat error is counted and the tick continues.
    """
    token = resolve_desktop_token()
    if not token:
        return {"skipped": True,
                "reason": "no Desktop token (BEEPER_DESKTOP_TOKEN / BEEPER_MATRIX_TOKEN)"}

    summary = {
        "skipped": False, "chats_seen": 0, "visited": 0,
        "outgoing": 0, "incoming": 0, "ignored": 0,
        "duplicate": 0, "errors": 0, "processed": 0,
    }

    with channel_tenant_scope():
        supabase = tenant_aware_client()
        try:
            async with BeeperDesktopClient(token) as client:
                info = await client.info()
                if not info:
                    summary.update({"skipped": True,
                                    "reason": "Desktop API unreachable — is Beeper Desktop running?"})
                    return summary

                cutoff = datetime.now(timezone.utc) - timedelta(days=COLD_START_WINDOW_DAYS)
                chats = await client.list_all_chats(stop_before=cutoff)
                summary["chats_seen"] = len(chats)
                visited = 0
                for chat in chats:
                    if visited >= MAX_CHATS_PER_TICK or summary["processed"] >= MAX_MESSAGES_PER_TICK:
                        break
                    meta = resolve_chat_meta(chat)
                    if not meta:
                        continue
                    cursor = _load_cursor(supabase, meta["desktop_chat_id"])
                    if not should_visit_chat(meta.get("last_activity"), cursor, cutoff):
                        continue
                    visited += 1
                    summary["visited"] += 1
                    await process_chat(supabase, client, meta, cursor, summary)

            # ── Liveness heartbeat ──
            # The VPS is now the SINGLE capture path (Mac agent + Modal
            # Matrix bridge retired). The Sentinel (Modal, every 5 min,
            # VPS-independent) watches this key — if a tick doesn't land
            # within its alert window, you get a Telegram alert instead of
            # discovering the feed went stale days later.
            try:
                core_config_upsert(supabase, {
                    "key": "beeper_desktop_last_tick",
                    "content": {
                        "tick_ts": datetime.now(timezone.utc).isoformat(),
                        **{k: summary[k] for k in
                           ("chats_seen", "visited", "outgoing", "incoming",
                            "ignored", "duplicate", "errors", "processed")},
                    },
                }).execute()
            except Exception as hb_err:
                audit_log_sync("beeper", "WARNING",
                               f"desktop heartbeat save failed: {hb_err}")
            return summary
        except Exception as e:
            audit_log_sync("beeper", "ERROR", f"desktop sync failed: {e}")
            return {"skipped": True, "error": str(e)}


def main() -> None:
    """CLI entry: run one tick and print the summary (launchd runs this)."""
    import json
    print(json.dumps(asyncio.run(run_desktop_sync()), indent=2))


if __name__ == "__main__":
    main()
