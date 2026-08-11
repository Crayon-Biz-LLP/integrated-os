# Design: Thread-Aware Channel Classification (FINAL — hardened plan)

> Status: **Phase 1 IMPLEMENTED** (Aug 11 2026) — built + unit-tested; deploy =
> run the RPC migration + backfill, then ship. Phase 2 gated on data.
> Live-data findings incorporated: (a) chat identity is pre-encoded in
> `sender_id`, (b) group/1:1 is detectable by colon-structure, (c) current
> batching was broken across group participants (fixed via chat_id merge).
> Scope: WhatsApp first; Teams inherits the pipeline once proven.

---

## 1. The problem (evidence)

1. **Noise pile-up.** 2,075 undecided FYI items (1,573 WhatsApp). Media/emoji/reaction
   messages become FYI rows that never expire. Root cause: per-message triage, no sieve.
2. **Context-blind extraction.** CNF-account group thread — the second message
   ("Abhishek and Danny possible today? If yes what time") is only meaningful with the
   first ("2 issues on the CNF account… quite urgent — Henry"). Per-message triage misses
   the cross-message action.
3. **Chat identity is mixed.** Live rows show `sender_id` = `"Group Name: Participant"`
   for groups and `"Contact Name"` for 1:1. The current code treats `sender_id` as a
   stable chat key — so `batch_whatsapp_message` (3-min merge) only batches within ONE
   participant, never across a group (Henry's ask + Sunjula's "ok noted" never batch).

---

## 2. The pipeline (final)

```
 message arrives (sender_id = "Group: Participant" or "Contact")
   │
   ▼
 Stage 0: chat/participant split ──► chat_id + participant stored (metadata)
   │  colon in sender_id ⇒ group (chat_id = prefix, participant = suffix)
   │  no colon ⇒ 1:1 (chat_id = sender_id, participant = null)
   ▼
 Stage A: deterministic sieve ─────► noise (free, instant)
   │  media-only / emoji-only / reactions /
   │  single-token / automated-sender
   ▼ (real text survives)
 Stage B: ask-detector ────────────► noise (free, instant)
   │  no ask-verb / no name mention / no
   │  urgency / no question shape ⇒ drop
   ▼ (looks like an ask)
 Stage C: LLM classify WITH context ──► actionable / fyi / ignored
   │  prompt = message + episode (same chat_id,
   │  ≥30-min silence = boundary, ≤12 msgs) +
   │  sender/participant identity + graph knowledge
   │  + salience signals (mentions_user, urgency, chat_tier)
   ▼
 Stage D: golden-set eval harness ──► repeatable proof of quality
```

### Stage 0 — Chat/participant split (NEW; the fix the data demanded)

At ingest, parse `sender_id` once and persist the split:

```
"CirroCraft - Paulsons Ledgers: Nathan"   → chat_id="CirroCraft - Paulsons Ledgers", participant="Nathan"
"#General - Garden of Eden: Mention Mirror" → chat_id="#General - Garden of Eden", participant="Mention Mirror"
"Mohammed Yazir Crayon Employee"          → chat_id="Mohammed Yazir Crayon Employee", participant=null (1:1)
```

- Stored in `metadata.chat_id` / `metadata.participant` (no schema migration needed;
  `messages.metadata` is JSONB and already documented for channel-specific overflow)
- **Why this matters:** it fixes group batching (all participants of a chat now share
  `chat_id`), makes thread windows exact (query by `chat_id`, not the mixed string), and
  gives group-vs-1:1 detection as a deterministic rule instead of a guess
- Backfill: a one-off script splits existing rows (the 86 sampled chats → ~60 real
  chats after prefix-collapsing)
- Change `batch_whatsapp_message` lock/merge key from `sender_id` → `chat_id`

### Stage A — Deterministic sieve (`core/lib/message_sieve.py`)

Before any LLM call, mark `noise` (`classification='ignored'`, `danny_decision='skipped'`):
- **Media-only / content-less:** "Sent a picture", "Sent a voice note", image/video/audio
  URLs, 📷/🎤 markers with no surrounding text
- **Emoji-only / reaction-only:** ≤3 chars all-emoji, or single reaction tokens
  ("😂", "👍", "ok", "k", "oh", "lol")
- **Automated senders:** existing `NOREPLY_PATTERNS` + group-mirror markers ("Mention
  Mirror", forwarded adverts)

### Stage B — Ask-detector (`core/lib/ask_detector.py`)

Deterministic pre-filter deciding whether this message warrants an LLM call. Escalate on
ANY of:
- **Ask forms:** "can you", "could you", "please", "let me know", "book", "call me",
  "confirm", "send", "need", "want you to", "possible today", "what time", "when",
  "urgent", "asap", "remind", "schedule"
- **Name mention:** the user's first name or any resolved graph person name
- **Question/request shape:** ends with "?", "if yes", "let's", "shall we"

Everything else → noise, no LLM call. This inverts the cost model: Gemini sees only the
ask fraction.

### Stage C — Context-aware classification (modify `classify_whatsapp_message`)

Keep the real-time per-message path. The prompt now receives:

1. **Episode window (anti-mixing):** messages from the SAME `chat_id` (not sender_id)
   back to the last ≥30-min silence gap, capped at 12. Timestamps + participant names
   included so the LLM can see sub-conversations.
   ```
   CHAT: CirroCraft - Paulsons Ledgers (group, 6 participants)
   EPISODE (3 messages, 10:02–10:07):
     [10:02] Henry: ...2 issues on the CNF account... quite urgent
     [10:05] Sunjula: ok noted
     [10:07] Henry: Abhishek and Danny possible today?
   NEW MESSAGE: "Abhishek and Danny possible today? If yes what time"
   ```
2. **Graph knowledge** (verify-before-trust): resolve chat/participant/mentioned names
   via the entity resolver; inject compact lines ("Henry = work contact (Paulsons
   Ledgers); CNF Account = active project"). Low-confidence/unresolved → "unknown"
   (wrong context is worse than no context).
3. **Salience signals in JSON** (additive, downstream-safe): `mentions_user`,
   `urgency` ("urgent"|"normal"|"routine"), `chat_tier`.
4. **Prompt guidance:** trivial chit-chat → `ignored`, not `fyi`. "fyi" means *worth
   Danny seeing*, not *a human spoke*.

### Stage D — Golden-set eval harness (`tests/golden/whatsapp_classify/`)

- **~20 real threads** covering: work request, family request, group chit-chat,
  media-only, urgent scheduling, forwarded news, **interleaved group sub-conversations**
- Golden labels: expected classification + expected extracted action per message
- `pytest` harness runs sieve + ask-detector + classify (LLM mocked/recorded) and asserts
  golden matches
- The classifier change is "done" only when the harness passes

---

## 3. Phase 2 — Build only if the data says so (gated)

| Gated item | Build trigger (data) |
|---|---|
| **Nightly thread sweep** (`core/skills/thread_sweep.py` + cron): re-read day's episodes, extract missed cross-message actionables, collapse all-noise chats | Daily audit shows ≥1 missed cross-message actionable/week that a sweep would catch |
| **Chat-tier profiles via telemetry** (per-chat signal ratio → Stage A/B auto-noise, sender drift) | ≥3 observations per chat; Quick Confirmations reject-rate stays high |

Phase 1 already ships the 14-day FYI expiry and the app's ack/dismiss surfaces, so the
backlog stays bounded before Phase 2.

---

## 4. Build order (Phase 1)

| Step | Work | Impact | Effort |
|---|---|---|---|
| 1 | **Golden set** — pull 20 real threads, write labels | The measuring stick for everything after | Small |
| 2 | **Stage 0** chat/participant split + backfill + batch RPC key fix | Exact chat keys; fixes group batching | Small-Medium |
| 3 | **Stage A** sieve + unit tests | Kills media/reaction firehose | Small |
| 4 | **Stage B** ask-detector + unit tests | LLM cost drops to ask fraction | Small |
| 5 | **Stage C** prompt: episode window + graph knowledge + salience | CNF-style extraction; personal judgment | Medium |
| 6 | **Stage D** harness green on golden set | Proof, not vibes | Small |
| 7 | Deploy + 1-week observation vs metrics | Phase 2 go/no-go | — |

Steps 2–4 are independent; Step 5 depends on 1 (eval) and benefits from 2 (exact keys).

## 5. Success metrics

- New undecided FYI per day: ~100+/day → near 0
- CNF-style threads: ≥1 actionable extracted per qualifying episode (golden set)
- LLM classify calls/day: drops to the ask fraction (cost)
- Backlog: undecided FYI < ~100 within 30 days
- Group batching: Henry + Sunjula "ok noted" merge into one episode row (Stage 0 proof)

## 6. Anti-patterns to avoid

- Batch-waiting for N messages before classifying (latency kills urgency)
- Persisting every human message as FYI
- Hardcoding chat tiers (must come from learning loop, Phase 2)
- Sweep deleting rows (reclassify/upgrade only)
- Trusting unresolved graph names
- **Treating `sender_id` as a chat key** (the exact bug this plan fixes)
