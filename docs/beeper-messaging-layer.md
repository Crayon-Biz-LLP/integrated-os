# Design: Beeper as Rhodey's Unified Messaging Layer (replacing WhatsApp ingestion)

> Status: **APPROVED + B0/B1 TESTED + PHASE A SHIPPED** (Aug 12 2026).
> Predecessor: `docs/thread-aware-classification.md` (Phase 1, deployed).
>
> **Phase A (direction + awaiting-reply + auto-resolve) — SHIPPED Aug 12:**
> - `direction` already existed on `messages` (db/01) — verified live; no column migration needed.
> - NEW `db/96_awaiting_reply.sql` — tenant-scoped `awaiting_reply` table (one open ask per owner+chat, TTL, grants).
> - NEW `core/services/awaiting_reply.py` — tracker (`mark_chat_awaiting_reply` / `find_open_ask` / `resolve_awaiting_reply` / `expire_stale_asks`) + the **auto-resolve rule** (`auto_resolve_on_outgoing`).
> - NEW `record_outgoing_message()` in `core/lib/ingest.py` — the Phase B1 receptor: stores outgoing rows with `danny_decision='responded'` (never surfaced — every pending feed filters `danny_decision IS NULL`, so zero feed-query changes) and fires the auto-resolve rule for the chat. Includes the Phase B3 parallel-run dedup guard (24h body-match, same channel+chat).
> - `'responded'` registered in `MESSAGES_DANNY_DECISIONS` (state_machines.py) — verified no DB CHECK constraint or code enforcement blocks it.
> - Tests: `tests/unit/test_awaiting_reply.py` (14 tests green; `tests/unit/test_inbox_feed.py` untouched and green).
>
> **Phase B1 — SHIPPED Aug 12:** the bridge-agent is live.
>
> **CUTOVER (B3 skipped by decision, Aug 12) — SHIPPED:** MacroDroid is
> retired WITHOUT the 1-week parallel-run validation window (user
> decision: "I will start using this new method itself"). Changes:
> - The bridge now routes INCOMING through the same sieve → ask-detector →
>   LLM → batch-RPC pipeline MacroDroid used (`_route_incoming`).
> - `db/97_batch_whatsapp_message_id.sql`: the batch RPC stores the native
>   Matrix `event_id` (`message_id` column) and returns `'duplicate'` for
>   re-delivered events — exact dedup at the DB level (unique_channel_message).
> - `process_whatsapp_message()` gained optional `chat_id`/`participant`/
>   `event_id` overrides (room-resolved identity from the bridge).
> - `POST /api/whatsapp-ingest` is now a 410 Gone stub — MacroDroid webhook
>   retired; disable the phone automation to stop the calls.
> - Room map stores `is_group` (member-count heuristic) so groups pass the
>   sender phone as participant while 1:1 chats use the room chat key.
> - NEW `core/skills/beeper_ingest.py` — Matrix `/sync` bridge: per-tenant
>   sync cursor (`core_config`), detects the user's own sends, routes them
>   through `record_outgoing_message()` (fires the auto-resolve rule).
> - NEW `infra/modal_app.py` scheduled function `beeper_bridge_sync`
>   (every 60s) + `/api/beeper-sync` route (cron fallback / manual ping).
> - **Chat-key normalization (grounded in live data):** rooms with
>   `m.room.name` use the room name (matches DB display-name keys, e.g.
>   'Jonathan Crosby ACC'); unnamed 1:1 rooms fall back to the WhatsApp
>   phone from the room creator (`@whatsapp_<phone>`), stamped as
>   `metadata.phone`. The auto-resolve rule now also matches
>   `metadata->>'phone'` so phone-keyed rows resolve name-keyed pending
>   items.
> - **B1 scope:** outgoing capture ONLY — incoming stays with MacroDroid
>   during the parallel run (no double-ingest). Incoming routing flips on
>   in B3 (the module already consumes the stream + learns room identity).
> - **Room identity is PERSISTED per tenant** (`core_config`
>   `beeper_room_map:{uid}`): Matrix /sync only re-delivers state CHANGES
>   after the first full sync (m.room.create is immutable and never
>   re-sent), so resolving identity fresh each tick would silently drop
>   every stable room from tick 2 onward. The bridge refreshes the map
>   whenever state events are present and falls back to it when empty.
> - Tests: `tests/unit/test_beeper_ingest.py` (pure logic + tick-level
>   tests incl. the incremental-sync persistence path; 26 tests green).
>
> **Phase B1 prerequisite (noted for the bridge-agent):** the auto-resolve
> rule matches `metadata->>'chat_id'` — it only fires if the outgoing row's
> chat_id equals the incoming rows' chat key. The bridge-agent MUST
> normalize Matrix room IDs to the same chat keys the ingest pipeline
> writes (`split_chat_identity` / `normalize_chat_key`). (Done: room name
> when set, phone fallback, both stamped on every row.)
> Maps onto the canonical 6-layer architecture (`product-summary/99-architecture-reference.md`).
>
> **Decision (Aug 12):** Beeper REPLACES the MacroDroid-based WhatsApp ingestion.
> MacroDroid is retired after a validation window. All other channels (email,
> calls, multimodal) are untouched by this plan.
>
> **B0/B1 verdict (verified live, Aug 12):** Beeper Desktop v4.3.20 running; 7
> networks connected (WhatsApp, Telegram, Signal, IG, LinkedIn, Discord, Matrix);
> the stored Matrix access token authenticates against the **public homeserver
> `https://matrix.beeper.com/`** (`whoami` HTTP 200, 3,391 joined rooms, message
> pagination returns real chat content). **B1 CONFIRMED — the bridge-agent runs on
> Modal; Path B2 (Mac/tunnel/mini-PC) is STRUCK from the plan.** Infra delta: zero.
>
> This document answers: *what happens to every stage of the Rhodey pipeline
> when Beeper becomes the message source — and exactly how the WhatsApp path
> is replaced.*

---

## 1. The decision, with pros and cons

### 1.1 Why replace WhatsApp ingestion at all?

| # | Option | Pros | Cons |
|---|---|---|---|
| 1 | **Keep MacroDroid** (current) | Already works for incoming; zero new work | Incoming-only (your sends invisible); fragile to WhatsApp/Beeper UI changes; no send path; phone is a single point of capture; mixed `sender_id` strings; dedup by body-matching |
| 2 | **Beeper Desktop API** (this plan) | Official API, no ToS risk; **both directions** (`isSender`); native chatID + message IDs; **send capability**; live WebSocket events; covers all networks, not just WhatsApp; can run headless on a ₹3k box; Beeper maintains the bridges | Needs a machine running Beeper Desktop (or Matrix token — to be tested); automation must stay human-paced (WhatsApp flags bulk sends); remote-access setup if the agent isn't local |
| 3 | Self-hosted Matrix + mautrix bridges | Full control; no Beeper dependency | **WhatsApp ToS violation → ban risk on your real number**; moderate-high maintenance (bridges break silently); ~1 week build; VPS + Postgres infra |
| 4 | WhatsApp Business Cloud API | Official webhooks both directions | **Business number ≠ your personal number** (people must message a different number); per-message fees; you lose your existing chats/contacts context |
| 5 | WhatsApp-Web bridge (Baileys/whatsmeow) | No new account | Unofficial → ban risk; maintenance; same ToS problem as #3 |

**Verdict:** Option 2 is the only one that gives bidirectional capture + send
+ zero ToS exposure + zero new hardware in the best case. Options 3–5 all
carry WhatsApp ban risk or a number change; option 1 can't see your sends.

### 1.2 Beeper delivery paths (the "where does the agent run" sub-decision)

| Path | Pros | Cons | When |
|---|---|---|---|
| **B0 → B1: Matrix token** | Capture runs on Modal — **zero local hardware**, zero Mac; true always-on | Modern Beeper may not expose the token; must live-test (30 min) | If the test passes |
| **B0 → B2: Desktop API + Remote Access** | Proven; official; real-time WS | Mac must be powered on (or a ₹3k always-on box); tunnel setup | If Matrix token fails |

---

## 2. Target architecture (WhatsApp replaced)

```
                ┌──────────────────────────────────────────┐
                │  Beeper Desktop (Mac / always-on box)    │
                │  REST :23373 · WS /v1/ws · MCP · tunnel  │
                └───────────────┬──────────────────────────┘
                                │  message.upserted events (both directions,
                                │  isSender, native chatID, network, msg ID, ts)
                                ▼
                ┌──────────────────────────────────────────┐
                │  Beeper bridge-agent (NEW)               │
                │  core/skills/beeper_ingest.py            │
                │  • WS listener (live) + catch-up cursor  │
                │  • normalize → unified message shape     │
                │  • direction-aware → Rhodey ingest       │
                └───────────────┬──────────────────────────┘
                                ▼
        ┌───────────────────────────────────────────────────┐
        │  Rhodey pipeline (existing, extended)             │
        │  dedup → split → sieve → ask-detector → LLM      │
        │  classify(episode+graph+awaiting-reply) → write  │
        └───────────────────────────────────────────────────┘
        (MacroDroid webhook: REMOVED after validation window)
```

### What is REPLACED (WhatsApp path)

| Component today | Disposition |
|---|---|
| MacroDroid notification listener (phone) | **Removed** after validation window (parallel-run first) |
| `POST /api/whatsapp-ingest` as *the* WhatsApp source | Replaced by the bridge-agent feeding a unified ingest (route stays only if needed for legacy/backfill) |
| Incoming-only capture | Replaced by bidirectional capture |
| Body-match dedup (24h window) | Replaced by native Beeper message-ID dedup |
| `sender_id` string parsing as primary identity | Superseded by native chatID/participant (split logic retained for legacy rows) |

### What is REUSED (unchanged logic)

- Classification pipeline: sieve → ask-detector → LLM classify with episode window + graph knowledge (`whatsapp_ingest.py` logic, channel-agnostic)
- `batch_whatsapp_message` RPC (3-min chat_id merge)
- Episode/thread machinery (`episode_context.py`)
- `messages` table + tenant scoping + enrichment queue + memory paths

### What is NEW

- `core/skills/beeper_ingest.py` — WS listener + catch-up + direction-aware ingest
- `direction` support through the pipeline (outgoing rows stored, never shown in approval)
- Awaiting-reply tracker (Phase A, **shipped**) — links replies to open asks across any gap
- Send path (Phase C) — Rhodey sends on your behalf via Beeper
- Internal `/api/beeper-ingest` route (bridge-agent → Modal)

---

## 3. Stage-by-stage impact (every pipeline stage)

### 3.1 Ingestion
- **Before:** MacroDroid posts incoming-only messages to `/api/whatsapp-ingest`.
- **After:** bridge-agent streams both directions from Beeper; `isSender` → `direction` column; native chatID + message IDs; live WS events.
- **Result:** your sends are recorded for the first time; dedup is exact; no phone dependency.

### 3.2 Classification
- **Before:** per-message classify with an episode window containing only the other side; 30-min gap boundary.
- **After:** both-side episodes; awaiting-reply state links replies across any gap; answers to open asks are no longer dropped by the ask-detector (they answer a known ask).
- **Result:** CNF-style cases classify correctly even when the reply comes 12 hours later.

### 3.3 Extraction
- **Before:** entities from text; sender identity from parsed `"Group: Participant"` strings.
- **After:** exact contact/participant names feed `resolve_person_in_query`; Danny's own sends become extractable signal (commitments, promises, relationship edges).
- **Result:** cleaner graph nodes, fewer "unknown person" rows, two-sided relationship data.

### 3.4 Enrichment
- **Before:** `pending_enrichment_jobs` builds edges from one-sided messages.
- **After:** two-sided, higher-confidence source material; reply→question link becomes an enrichment job; chat-tier telemetry gets full-thread signal.
- **Result:** richer edges, same queue mechanics, no infra change.

### 3.5 Memory building
- **Before:** `relationship_note` memories from incoming FYI only.
- **After:** two-sided relationship memory ("what did I tell Henry" becomes retrievable); outgoing commitments captured; episode-level memories for full exchanges.
- **Result:** retrieval answers questions about your own side for the first time.

### 3.6 Send path (NEW capability)
- Rhodey drafts a reply → you approve → Beeper sends → recorded as your outgoing message → chat marked awaiting-reply.
- **Result:** Rhodey becomes an executor (send reminders/confirmations/asks), not just a triage inbox.

### 3.7 Briefing / Pulse
- Briefing context includes both sides of open threads; awaiting-reply items surface until resolved ("Henry hasn't replied to your CNF question — asked yesterday").
- **Result:** actionable follow-ups, not just inbound triage.

### 3.8 Multi-tenant
- Beeper account per tenant; bridge-agent runs per `tenant_scope`; tenant facade (`p_owner`/RLS) unchanged. Credentials in Modal secrets per tenant.

---

## 4. Build order

| Phase | Work | Scope | New infra? | Exit criterion |
|---|---|---|---|---|
| **A** | `direction` support + awaiting-reply tracker | `api/index.py`, new `core/lib/awaiting_reply.py`, migration | No | Tracker links a reply to an open ask across >24h gap (test) |
| **B0** | **Matrix-token live test** (30 min) | verification | No | Token works → B1; else B2 |
| **B1** | Bridge-agent on Modal via Matrix sync | `beeper_ingest.py` | Best case: none | Both-direction messages ingested live |
| **B2** | Bridge-agent via Desktop API + Remote Access tunnel | same + tunnel | Mac powered on | Same |
| **B3** | **Parallel-run + cutover** | dual-write during validation window (e.g. 1 week) | No | Beeper parity ≥ MacroDroid (no missed incoming); then MacroDroid disabled |
| **C** | Send path (Rhodey replies through Beeper) | app UI + `beeper_send.py` | No | Approved reply lands in WhatsApp |
| **D** | Remove MacroDroid webhook + old dedup path; cross-network identity; chat-tier learning | cleanup + graph merge | No | `api/whatsapp-ingest` disabled; cleanup script run |

**Parallel-run rule (B3):** for one week both MacroDroid and Beeper feed the
pipeline; a diff check flags any message Beeper missed that MacroDroid caught.
Only when the diff is empty does the MacroDroid path get disabled. This is the
safety net for replacing a working ingestion path.

---

## 5. Layer-by-layer mapping (6-layer architecture)

Maps every change onto `product-summary/99-architecture-reference.md`.
Legend: **REPLACE** = existing component removed/superseded · **ADD** = new component.

### Layer 1 — Ingestion

| Component (today) | Change | Beeper replacement |
|---|---|---|
| `core/skills/whatsapp_ingest.py` + MacroDroid webhook (`POST /api/whatsapp-ingest`) | **REPLACE** (after B3 parallel-run validation) | `core/skills/beeper_ingest.py` — Matrix sync loop → unified ingest |
| Incoming-only capture | **REPLACE** | Bidirectional capture (`isSender` → `direction`) |
| Body-match dedup (24h) | **REPLACE** | Native Matrix event IDs (`event_id`) |
| `sender_id` string parsing | **REPLACE** | Native `room_id` + participant (split logic retained for legacy rows) |
| `core/skills/teams_ingest.py` (GHA cron) | **REPLACE** (later) | Same Beeper pipe covers Teams via bridge |
| `core/lib/ingest.py` unified contract | **ADD** `direction` param | Outgoing rows stored, never surfaced for approval |

### Layer 2 — Processing

| Component | Change | Details |
|---|---|---|
| `plan_actions()` / executor | **ADD** send action | `send_channel_message` op — approved sends via Matrix `PUT /rooms/{id}/send` |
| Enrichment queue | **ADD** job type | reply→question link + auto-resolve as queued jobs (3-retry, same mechanics) |
| `core/lib/awaiting_reply.py` (NEW) | **ADD** | tracker: chat marked awaiting-reply on outgoing ask; incoming reply linked across any gap |
| Auto-resolve rule (NEW) | **ADD** | outgoing in chat X at T → pending items in X (<T, <48h) → `danny_decision='responded'` |
| `core/lib/chat_split.py` | **ADD** | native room identity supersedes string parse for Beeper-sourced rows |

### Layer 3 — Intelligence

| Component | Change | Details |
|---|---|---|
| Associative retrieval | **ADD** signal source | search over Beeper timeline (`messages/search`) — primary evidence for "what did X say" |
| Knowledge graph | **ADD** | contacts per network (`accounts/{id}/contacts`) → person resolution exact; cross-network identity merge |
| Conversation threads | **ADD** | both-side episodes; awaiting-reply linking; cross-network continuity |
| Episode context (`episode_context.py`) | **ADD** | both-side windows (outgoing included) |
| Entity extraction | **ADD** | Danny's sends become extractable (commitments, promises, edges) |

### Layer 4 — Presentation

| Component | Change | Details |
|---|---|---|
| Decision Pulse | **ADD** filter | `responded` items excluded from pending approvals; audit note "you replied at T" |
| Pulse Engine / briefings | **ADD** | open-thread awareness ("Henry hasn't replied — asked yesterday"); both-side context |
| Inbox / Quick Confirmation | **ADD** | outgoing rows never shown; stale items auto-resolve; send-approval UI (Phase C) |
| Push | **ADD** | auto-resolve notifications (non-intrusive) |

### Layer 5 — Persistence

| Component | Change | Details |
|---|---|---|
| `messages` table | **ADD** `direction` column (+ `metadata.room_id`, `metadata.participant`) | no schema break; mirrors chat_id approach |
| `awaiting_reply` state (NEW table or JSONB) | **ADD** | tenant-scoped, migration in Phase A |
| `danny_decision` values | **ADD** `'responded'` | distinct from rejected/skipped/expired |
| State machines (`state_machines.py`) | **ADD** transitions | new decision value registered |
| Sync cursor (last `since` token) | **ADD** | per-tenant Matrix sync position (JSONB or small table) |

### Layer 6 — Integration

| Component | Change | Details |
|---|---|---|
| **Beeper Matrix homeserver** (NEW service) | **ADD** | `https://matrix.beeper.com/` — public, token-authed; bridge-agent connects from Modal |
| Modal secrets (`rhodey-os`) | **ADD** | `BEEPER_MATRIX_TOKEN`, `BEEPER_HOMESERVER`, per-tenant tokens |
| Bridge-agent on Modal (NEW) | **ADD** | scheduled sync fn (30–60s) + WS streaming; catch-up cursor |
| `GET /api/beeper-ingest` (NEW route) | **ADD** | internal route bridge-agent → ingest (auth via secret) |
| Send path (NEW) | **ADD** | approved sends via Matrix `PUT send`; `send_channel_message` action |
| MacroDroid webhook | **REMOVE** | after B3 validation window; `WHATSAPP_INGEST_SECRET` retired (Phase D) |

### The one-line summary per layer

> L1: one pipe replaces scrapers · L2: send action + awaiting-reply + auto-resolve ·
> L3: timeline search + exact contacts + both-side threads · L4: no stale approvals,
> conversation-aware briefings · L5: `direction` + `responded` + sync cursor ·
> L6: Beeper homeserver joins as an external service on Modal.

---

## 6. Infrastructure (what exists, what's new)

### 5.1 Current stack (unchanged base)

| Layer | Today | Beeper role |
|---|---|---|
| **Compute** | Modal app `rhodey-os` (`infra/modal_app.py`, Python 3.11, `min_containers=1`, 300s timeout) | Hosts the new `/api/beeper-ingest` route + bridge-agent logic + awaiting-reply tracker. Free-credit covered; a scheduled sync fn is negligible |
| **DB** | Supabase Postgres + pgvector + PostgREST + asyncpg pool | Hosts `awaiting_reply` state + `direction` column + all pipeline tables. No new service |
| **Cache** | Upstash Redis (fail-open) | Unchanged |
| **Secrets** | Modal secret `rhodey-os` (from `.env` via `scripts/create_modal_secret.py`) | Add `BEEPER_TOKEN`/`BEEPER_API_URL` (per-tenant in tenant secret config) + `BEEPER_INGEST_SECRET` |
| **Scheduling** | cron-job.org (sentinel 5m, pulse 30m, roundup) + GHA workflows | Reused for the bridge-agent's polling fallback / catch-up |
| **Monitoring** | `/api/health` + GHA `health.yml` (2h) | Extend with a "last beeper sync" heartbeat check |
| **LLM** | Gemini multi-key + OpenRouter fallback | Unchanged |

### 5.2 New infra, per delivery path

**Path B1 (Matrix token — best case):**
- **Nothing local.** Bridge-agent runs as a Modal scheduled function (every ~30–60s incremental Matrix sync; catch-up cursor in Postgres).
- No Mac, no box, no tunnel. Cost: ~0.

**Path B2 (Desktop API + Remote Access — fallback):**
| Component | What | Cost |
|---|---|---|
| Beeper Desktop | Runs on your Mac (powered on) **or** a ₹3k always-on mini PC / old laptop | Free / ~₹3k one-time |
| Remote Access | Beeper's built-in Cloudflare Tunnel or Tailscale — Modal reaches the local API remotely | Free tier |
| Bridge-agent | Either a small always-on daemon on the same machine (WS listener + forward to Modal), or a Modal scheduled fn polling the tunneled API every ~60s | ~0 |
| Uninterrupted power | Mac plugged in / mini PC on UPS | — |

**Both paths:**
- Modal secret additions: `BEEPER_TOKEN` (+ per-tenant), `BEEPER_API_URL`, `BEEPER_INGEST_SECRET` (bridge-agent→Modal auth).
- DB: Phase A migration — `awaiting_reply` state (JSONB column or small table, tenant-scoped) + `direction` in `messages.metadata` (no schema break; mirrors `chat_id` approach).
- During Phase B3 parallel-run: `WHATSAPP_INGEST_SECRET` (MacroDroid) stays; removed in Phase D.

### 5.3 What is NOT needed
- No new VPS / cloud provider (Modal already runs everything).
- No new database (Supabase covers it).
- No self-hosted Matrix/mautrix bridges, no Baileys/whatsmeow, no WhatsApp Business account.
- No new LLM provider.

---

## 7. Pros & cons of the whole plan

### Pros
- **Your sends are finally visible** — the single biggest gap in Rhodey today gets closed.
- **Official, zero ToS risk** — no WhatsApp ban exposure (unlike self-hosted bridges).
- **One pipe for all networks** — WhatsApp, Telegram, iMessage, SMS later; MacroDroid dies.
- **Live + exact** — WebSocket events, native message IDs, native chatID.
- **Send capability** — Rhodey evolves from triage to executor.
- **Two-sided memory & graph** — retrieval starts answering "what did I say".
- **Minimal reuse churn** — classification/enrichment/memory/tenant code all stay.

### Cons / costs
- **New machine dependency (worst case):** if the Matrix token fails, the Mac (or a ₹3k box) must be powered on — though nothing else runs on it.
- **New adapter to build & maintain** — the bridge-agent is new code with a live-verification step (B0).
- **Automation pacing constraint** — sends must stay human-approved to avoid WhatsApp flagging.
- **Beeper is a dependency** — if Beeper's service/API changes, the adapter follows (mitigated: thin consumer, bridges maintained by Beeper).
- **Validation window required** before MacroDroid is retired (1 week parallel-run).
- **Matrix token path may not work** — then B2 (tunnel) is slightly more setup.

---

## 8. Risks & mitigations

| Risk | Mitigation |
|---|---|
| WhatsApp flags automated sends | Sends remain user-approved + human-paced; no bulk automation |
| Matrix token path fails | B2 (Remote Access) fallback is proven; B0 decides before building |
| Beeper misses a message (regression vs MacroDroid) | B3 parallel-run diff check before cutover |
| Bridge-agent outage (WS drop) | Catch-up cursor + polling fallback; reconnects with backfill |
| Desktop API requires machine on | Matrix path (B1) or always-on box; Mac only needs power, not attention |
| Tenant cross-talk | Per-tenant scoped bridge-agent + existing `p_owner`/RLS facade |

## 9. Non-goals (deliberate)

- No self-hosted Matrix + mautrix bridges (ban risk on Danny's WhatsApp number, high maintenance).
- No replacement of the email pipeline (Gmail/Outlook APIs are official and working).
- No chat-app UI — Rhodey stays a Chief of Staff; app inbox/approval surfaces remain.
- No change to calls, multimodal, or Telegram-command channels in this plan.
