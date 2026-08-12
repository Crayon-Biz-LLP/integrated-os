"""Phase B1 — Beeper Matrix bridge-agent (Modal, zero hardware).

Syncs the user's Beeper stream via the PUBLIC Matrix homeserver
(`matrix.beeper.com` — B1 confirmed live Aug 12: the stored Matrix access
token authenticates from any network, 3,391 joined rooms, message
pagination works). For each sync tick it:

  - advances the per-tenant sync cursor (`core_config` key
    `beeper_sync_cursor:{uid}`),
  - detects the USER's own sends (`event.sender == own user id`) and
    routes them through `record_outgoing_message()` — which stores the
    outgoing row (direction='outgoing', danny_decision='responded', never
    surfaced) and fires the auto-resolve rule for the chat. That is the
    stale-decision fix: a pending approval in a chat the user already
    replied to stops being surfaced.

Incoming messages ARE routed through the same sieve → ask-detector → LLM
→ batch-RPC pipeline MacroDroid used (cutover: the bridge is now the
PRIMARY WhatsApp source). Native Matrix event ids are stored
(message_id) so re-delivered events dedup exactly at the DB level.

Chat-key resolution (grounded in live data):
  - Rooms with `m.room.name` → use the room name (matches the display-name
    chat keys the ingest pipeline writes, e.g. 'Jonathan Crosby ACC').
  - Unnamed 1:1 rooms → fall back to the WhatsApp phone extracted from the
    room creator (`@whatsapp_<phone>:beeper.local`); the phone is also
    stamped as `metadata.phone` so the auto-resolve rule (which matches
    `metadata->>'phone'` in addition to chat_id/sender_id) can bridge
    name-keyed and phone-keyed rows.

Run via: POST /api/beeper-sync (cron-job.org, every 60s) or the Modal
scheduled function. Per-tenant fanout mirrors the sentinel/pulse pattern.
"""

import os
import re
from datetime import datetime, timezone

import httpx

from core.lib.audit_logger import audit_log_sync
from core.services.db import (
    active_user_ids,
    core_config_upsert,
    tenant_aware_client,
    tenant_scope,
)

HOMESERVER = os.getenv("BEEPER_MATRIX_HOMESERVER", "https://matrix.beeper.com")
SYNC_CURSOR_KEY = "beeper_sync_cursor"
ROOM_MAP_KEY = "beeper_room_map"
DEFAULT_SYNC_TIMEOUT_MS = 30000
MAX_EVENTS_PER_TICK = 500  # cap per sync to bound a cold-start catch-up

# Room creators for bridged networks: @whatsapp_<phone>:beeper.local
_WHATSAPP_SENDER_RE = re.compile(r"^@whatsapp_([+0-9]+):")

# Members that are the user themselves or the bridge bot — excluded when
# counting real participants to distinguish 1:1 chats from groups.
_BRIDGE_BOT_USER_IDS = {"@whatsappbot:beeper.local"}


# ── Token resolution ────────────────────────────────────────────────────

def resolve_beeper_token(uid: str | None = None) -> str | None:
    """Per-tenant Beeper Matrix token: user_oauth_tokens → env fallback.

    Resolution order:
      1. `user_oauth_tokens` row with provider='beeper' for the tenant
         (multi-tenant world — each user brings their own Beeper account).
      2. Env `BEEPER_MATRIX_TOKEN` (legacy single-user world / Danny).
    Returns None when neither exists → the bridge skips that tenant.
    """
    if uid:
        try:
            res = (
                tenant_aware_client()
                .table("user_oauth_tokens")
                .select("refresh_token")
                .eq("user_id", uid)
                .eq("provider", "beeper")
                .limit(1)
                .maybe_single()
                .execute()
            )
            if res.data and res.data.get("refresh_token"):
                return str(res.data["refresh_token"]).strip() or None
        except Exception as e:
            audit_log_sync("beeper", "WARNING", f"token lookup failed for {uid}: {e}")
    token = os.getenv("BEEPER_MATRIX_TOKEN")
    return token.strip() if token else None


# ── Matrix client ───────────────────────────────────────────────────────

class BeeperMatrixClient:
    """Minimal Matrix client-server API wrapper for the sync path."""

    def __init__(self, token: str, homeserver: str = HOMESERVER):
        self._token = token
        self._hs = homeserver.rstrip("/")

    async def __aenter__(self):
        self._client = httpx.AsyncClient(
            base_url=self._hs,
            headers={"Authorization": f"Bearer {self._token}"},
            timeout=30.0,
        )
        return self

    async def __aexit__(self, *exc):
        await self._client.aclose()

    async def whoami(self) -> str | None:
        """Return the authenticated user's Matrix id (e.g. @danielyashwant:beeper.com)."""
        try:
            r = await self._client.get("/_matrix/client/v3/account/whoami")
            if r.status_code == 200:
                return (r.json() or {}).get("user_id")
        except Exception as e:
            audit_log_sync("beeper", "WARNING", f"whoami failed: {e}")
        return None

    async def sync(self, since: str | None = None,
                   timeout_ms: int = DEFAULT_SYNC_TIMEOUT_MS) -> dict:
        """Incremental /sync — returns the raw response dict (or {} on failure)."""
        params = {"timeout": timeout_ms}
        if since:
            params["since"] = since
        try:
            r = await self._client.get("/_matrix/client/v3/sync", params=params)
            if r.status_code == 200:
                return r.json() or {}
            audit_log_sync("beeper", "WARNING",
                           f"/sync HTTP {r.status_code}: {r.text[:200]}")
        except Exception as e:
            audit_log_sync("beeper", "WARNING", f"/sync failed: {e}")
        return {}


# ── Room identity ───────────────────────────────────────────────────────

def extract_room_name(state_events: list[dict]) -> str | None:
    """m.room.name from a room's state events, or None."""
    for ev in state_events or []:
        if ev.get("type") == "m.room.name":
            name = (ev.get("content") or {}).get("name", "")
            if name:
                return name.strip()
    return None


def extract_room_phone(state_events: list[dict]) -> str | None:
    """WhatsApp phone from the room creator (@whatsapp_<phone>:...), or None."""
    for ev in state_events or []:
        if ev.get("type") == "m.room.create":
            creator = ev.get("sender") or (ev.get("content") or {}).get("creator", "")
            m = _WHATSAPP_SENDER_RE.match(creator or "")
            if m:
                return m.group(1)
    return None


def resolve_chat_key(room_id: str, state_events: list[dict]) -> tuple[str, dict]:
    """Resolve a Matrix room to the ingest pipeline's chat key.

    Returns (chat_key, extra_metadata):
      - chat_key: room name when set (matches DB display-name keys), else
        the WhatsApp phone, else the raw room id (last resort).
      - extra_metadata: {'room_id', 'phone'} when resolvable.
    """
    name = extract_room_name(state_events)
    phone = extract_room_phone(state_events)
    if name:
        return name, {"room_id": room_id, "phone": phone}
    if phone:
        return phone, {"room_id": room_id, "phone": phone}
    return room_id, {"room_id": room_id, "phone": None}


def is_whatsapp_room(state_events: list[dict]) -> bool:
    """True when the room is a bridged WhatsApp room (creator is @whatsapp_...)."""
    for ev in state_events or []:
        if ev.get("type") == "m.room.create":
            creator = ev.get("sender") or (ev.get("content") or {}).get("creator", "")
            if "@whatsapp_" in (creator or ""):
                return True
    return False


def is_user_send(event: dict, own_user_id: str) -> bool:
    """True when a timeline event is the user's own message.

    Any m.room.message from the user's own Matrix id counts — including
    messages some bridges deliver as m.notice (a user's own notice is
    still their send). Reactions and edits are excluded upstream by
    event_body().
    """
    if not own_user_id:
        return False
    if event.get("type") != "m.room.message":
        return False
    return event.get("sender") == own_user_id


def is_group_room(state_events: list[dict], own_user_id: str | None = None) -> bool:
    """True when a room has 2+ real human participants besides the user.

    Counts m.room.member state events, excluding the user's own id and the
    bridge bot. A 1:1 WhatsApp room is exactly (user, contact, whatsappbot)
    — one real participant; a group has two or more.
    """
    humans = set()
    for ev in state_events or []:
        if ev.get("type") != "m.room.member":
            continue
        uid = ev.get("state_key") or ""
        if not uid or uid == own_user_id:
            continue
        if uid in _BRIDGE_BOT_USER_IDS or uid.startswith("@whatsappbot"):
            continue
        membership = (ev.get("content") or {}).get("membership")
        if membership in ("join", "invite"):
            humans.add(uid)
    return len(humans) >= 2


def event_body(event: dict) -> str:
    """Message body from a timeline event ('' for reactions/edits)."""
    content = event.get("content") or {}
    rel = content.get("m.relates_to") or {}
    if rel.get("rel_type") in ("m.replace", "m.annotation"):
        return ""  # edits and reactions are not standalone messages
    return str(content.get("body") or "").strip()


def event_sender_phone(event: dict) -> str | None:
    """WhatsApp phone from an event's sender id (@whatsapp_<phone>:...)."""
    m = _WHATSAPP_SENDER_RE.match(event.get("sender") or "")
    return m.group(1) if m else None


def event_ts_iso(event: dict) -> str | None:
    """origin_server_ts (ms epoch) → ISO string, or None."""
    ts = event.get("origin_server_ts")
    if not ts:
        return None
    try:
        return datetime.fromtimestamp(int(ts) / 1000, tz=timezone.utc).isoformat()
    except Exception:
        return None


# ── Sync cursor (per-tenant) ────────────────────────────────────────────

def _cursor_key(uid: str | None) -> str:
    return SYNC_CURSOR_KEY if not uid else f"{SYNC_CURSOR_KEY}:{uid}"


def _load_cursor(supabase, uid: str | None) -> str | None:
    try:
        res = (
            supabase.table("core_config")
            .select("content")
            .eq("key", _cursor_key(uid))
            .limit(1)
            .maybe_single()
            .execute()
        )
        data = res.data
        if data and isinstance(data.get("content"), dict):
            return data["content"].get("since")
    except Exception:
        pass
    return None


def _save_cursor(supabase, uid: str | None, since: str) -> None:
    try:
        core_config_upsert(supabase, {
            "key": _cursor_key(uid),
            "content": {"since": since, "updated_at": datetime.now(timezone.utc).isoformat()},
        })
    except Exception as e:
        audit_log_sync("beeper", "WARNING", f"cursor save failed: {e}")


# ── Room identity map (per-tenant, persisted) ──────────────────────────
# Matrix /sync only re-delivers state CHANGES after the first (full) sync:
# m.room.create is immutable and never re-sent, m.room.name only when it
# changes. So from tick 2 onward, stable rooms arrive with EMPTY
# state.events — resolving identity fresh each tick would silently drop
# every stable room. We persist room_id -> {chat_key, phone, is_whatsapp}
# per tenant, refresh it whenever state events are present, and fall back
# to it when they are not.


def _room_map_key(uid: str | None) -> str:
    return ROOM_MAP_KEY if not uid else f"{ROOM_MAP_KEY}:{uid}"


def _load_room_map(supabase, uid: str | None) -> dict:
    try:
        res = (
            supabase.table("core_config")
            .select("content")
            .eq("key", _room_map_key(uid))
            .limit(1)
            .maybe_single()
            .execute()
        )
        data = res.data
        if data and isinstance(data.get("content"), dict):
            return dict(data["content"])
    except Exception:
        pass
    return {}


def _save_room_map(supabase, uid: str | None, room_map: dict) -> None:
    try:
        core_config_upsert(supabase, {
            "key": _room_map_key(uid),
            "content": room_map,
        })
    except Exception as e:
        audit_log_sync("beeper", "WARNING", f"room map save failed: {e}")


# ── Event processing ────────────────────────────────────────────────────

async def _route_incoming(
    supabase, ev: dict, chat_key: str, room_meta: dict, own_user_id: str,
    is_group: bool,
) -> dict:
    """Route one incoming (other-side) message through the classification
    pipeline.

    Cutover path (B3+D): the bridge is now the PRIMARY WhatsApp source, so
    incoming messages must reach the same sieve → ask-detector → LLM →
    batch-RPC pipeline MacroDroid used. Identity is room-resolved (the
    Matrix stream has no "Chat: Participant" string): 1:1 uses the room
    chat key; groups pass the sender phone as participant.

    Returns {"status": "routed" | "ignored" | "duplicate" | "error"}.
    """
    body = event_body(ev)
    if not body:
        # event_body() already returns "" for reactions/edits (m.replace /
        # m.annotation) — this guard catches them.
        return {"status": "ignored", "reason": "no body / reaction / edit"}

    sender_phone = event_sender_phone(ev)
    if not sender_phone:
        return {"status": "ignored", "reason": "no sender phone"}

    try:
        from core.skills.whatsapp_ingest import process_whatsapp_message

        participant = sender_phone if is_group else None
        # Sender identity: for 1:1, chat_key IS the contact's display name
        # (resolve_chat_key returns the room name when set); for groups the
        # participant is the sender phone (Matrix gives no display names).
        sender_name = chat_key if not is_group else sender_phone
        result = await process_whatsapp_message(
            sender_name=sender_name,
            sender_phone=sender_phone,
            message_text=body,
            received_at=event_ts_iso(ev),
            chat_id=chat_key,
            participant=participant,
            event_id=ev.get("event_id"),
        )
        status = result.get("status")
        if status in ("ignored", "duplicate"):
            return {"status": "ignored", "reason": status}
        return {"status": "routed", "result": result}
    except Exception as e:
        audit_log_sync("beeper", "WARNING", f"incoming event failed: {e}")
        return {"status": "error", "reason": str(e)}


async def process_sync_tick(
    supabase, client: BeeperMatrixClient, payload: dict, own_user_id: str,
    uid: str | None, max_events: int = MAX_EVENTS_PER_TICK,
) -> dict:
    """Process one /sync payload: record user sends AND route incoming.

    Room identity: refresh the persisted per-tenant room map from any state
    events present in this payload, and fall back to the map when a room
    arrives with empty state (the steady-state /sync shape — m.room.create
    is never re-delivered).

    Returns a summary dict. Never raises (fail-open per event).
    """
    summary = {"outgoing": 0, "incoming": 0, "skipped": 0,
               "rooms_seen": 0, "errors": 0}

    room_map = _load_room_map(supabase, uid)
    dirty = False

    joined = (payload.get("rooms") or {}).get("join") or {}
    for room_id, room in joined.items():
        state_events = (room.get("state") or {}).get("events") or []

        # Fresh identity when state is present; persist it for later ticks.
        if state_events:
            chat_key, room_meta = resolve_chat_key(room_id, state_events)
            is_group = is_group_room(state_events, own_user_id)
            room_map[room_id] = {
                "chat_key": chat_key,
                "phone": room_meta.get("phone"),
                "is_whatsapp": is_whatsapp_room(state_events),
                "is_group": is_group,
            }
            dirty = True
        else:
            entry = room_map.get(room_id)
            if not entry:
                continue  # never seen with state — skip (defensive)
            chat_key = entry.get("chat_key")
            is_group = bool(entry.get("is_group"))
            room_meta = {"room_id": room_id, "phone": entry.get("phone")}

        if not room_map.get(room_id, {}).get("is_whatsapp"):
            continue  # WhatsApp rooms only (other networks in later phases)
        summary["rooms_seen"] += 1

        timeline = (room.get("timeline") or {}).get("events") or []
        for ev in timeline:
            if summary["outgoing"] + summary["incoming"] + summary["skipped"] >= max_events:
                break
            event_id = ev.get("event_id")

            if is_user_send(ev, own_user_id):
                # ── Outgoing: record + auto-resolve (Phase A/B1) ──
                body = event_body(ev)
                if not body:
                    summary["skipped"] += 1
                    continue
                ts_iso = event_ts_iso(ev)

                try:
                    from core.lib.ingest import record_outgoing_message

                    meta = dict(room_meta)
                    meta["matrix_sender"] = ev.get("sender")
                    meta["matrix_event_id"] = event_id
                    result = await record_outgoing_message(
                        chat_id=chat_key,
                        source="whatsapp",
                        body=body,
                        received_at=ts_iso,
                        tracking_id=event_id,
                        summary=body[:500],
                        metadata=meta,
                    )
                    if result.get("status") == "filed":
                        summary["outgoing"] += 1
                    elif result.get("status") == "duplicate":
                        summary["skipped"] += 1
                    else:
                        summary["errors"] += 1
                        audit_log_sync("beeper", "WARNING",
                                       f"record_outgoing failed: {result.get('reason')}")
                except Exception as e:
                    summary["errors"] += 1
                    audit_log_sync("beeper", "WARNING", f"event {event_id} failed: {e}")
            else:
                # ── Incoming: route through the classification pipeline ──
                res = await _route_incoming(
                    supabase, ev, chat_key, room_meta, own_user_id, is_group
                )
                if res["status"] == "routed":
                    summary["incoming"] += 1
                elif res["status"] == "ignored":
                    summary["skipped"] += 1
                else:
                    summary["errors"] += 1

    if dirty:
        _save_room_map(supabase, uid, room_map)
    return summary


# ── Top-level run (per-tenant) ──────────────────────────────────────────

async def _sync_once(uid: str | None) -> dict:
    token = resolve_beeper_token(uid)
    if not token:
        return {"tenant": uid, "skipped": True,
                "reason": "no Beeper token configured (user_oauth_tokens or env)"}

    supabase = tenant_aware_client()
    try:
        async with BeeperMatrixClient(token) as client:
            own_user_id = await client.whoami()
            if not own_user_id:
                return {"tenant": uid, "skipped": True, "reason": "whoami failed"}

            since = _load_cursor(supabase, uid)
            payload = await client.sync(since=since)
            if not payload:
                return {"tenant": uid, "skipped": True, "reason": "empty sync"}

            summary = await process_sync_tick(
                supabase, client, payload, own_user_id, uid
            )

            next_batch = payload.get("next_batch")
            if next_batch:
                _save_cursor(supabase, uid, next_batch)

            summary.update({"tenant": uid, "since_prev": bool(since),
                            "next_batch": bool(next_batch)})
            return summary
    except Exception as e:
        audit_log_sync("beeper", "ERROR", f"sync failed for tenant {uid}: {e}")
        return {"tenant": uid, "error": str(e)}


async def run_beeper_sync(uid: str | None = None) -> dict:
    """Run one bridge tick. uid=None → fan out over all active tenants.

    Returns a summary (per-tenant when fanning out). Fail-open: one
    tenant's sync failure never aborts the others.
    """
    if uid:
        with tenant_scope(uid):
            return await _sync_once(uid)

    uids = active_user_ids()
    if not uids:
        from core.services.db import channel_tenant_scope
        with channel_tenant_scope():
            return {"results": [await _sync_once(None)]}
    results = []
    for uid_ in uids:
        try:
            with tenant_scope(uid_):
                results.append(await _sync_once(uid_))
        except Exception as e:
            audit_log_sync("beeper", "ERROR", f"tenant {uid_} sync failed: {e}")
            results.append({"tenant": uid_, "error": str(e)})
    return {"tenants": len(uids), "results": results}
