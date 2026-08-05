# Part 70: Thread & Memory Redesign — Investigation, Research, Direction B Implementation

> **Date**: Aug 5, 2026
> **Status**: **Direction B core IMPLEMENTED** — the transcript is no longer fed to any
> prompt; all context comes from structured state + a bounded last-2-user-turns window.
> Earlier in this session the prompt-level CONVERSATION TRUTH GUARD was added to
> `core/prompts/briefing.py`; once Direction B shipped, the guards became redundant and
> were removed along with the injection.
> **Canonical design doc**: `product-summary/60-thread-and-memory-architecture.md`

## The two incidents that triggered this

1. **"All messages linked to the first message"** (post-Phase-2 thread rollout). Every
   non-entity message routed into the single general thread for the chat. Follow-ups
   received the entire unrelated history as context.
2. **The Bosch resurrection (Aug 5)**. A "Wrap-up" pulse briefing claimed *"picking up the
   Bosch tool kit tomorrow and calling health insurance on Monday are on your list"* — but the
   Bosch task was **completed Aug 2**. Verified: task 2557 `status='done'`; the pulse's live
   task query correctly excluded it; the claim came from `CONVERSATION HISTORY` fed raw from
   the `conversations` table, which contained only two Aug 1 user messages in that thread
   ("Remind me to pick up the Bosch Tool kit tomorrow at 9am", "…call the health insurance on
   Monday…"). The model treated 4-day-old transcript claims as current board state.

## Root cause (verified, not assumed)

The thread system conflates **three different memories** into one mechanism, and the general
thread has no lifecycle:

| Memory | Current implementation | Problem |
|---|---|---|
| Episodic (what was said) | Raw `conversations` rows, injected whole via `get_history()` → `format_history_for_prompt()` | **No recency bound** — only a 5000-token cap; under the cap, everything since thread creation is included regardless of age |
| Working (what's happening now) | Same raw history, used for anaphora/coherence | Over-supplied: 4-day-old turns are not "working memory" |
| Semantic/structured (what Rhodey knows) | Tasks, KG, decisions, delta_context, embeddings + `match_conversations` | ✅ Correct — but it **loses in the prompt** to the raw transcript |

Two structural defects:
1. **Eternal general bucket**: `resolve_thread()` priority 5 reuses the general thread
   forever. Sentinel archives it only after **14 days inactive** (H2) — an active chat's
   general thread never archives. Every non-entity message accumulates in one bucket.
2. **Transcript-as-memory anti-pattern**: the LLM is asked to arbitrate between raw
   transcript claims and structured state — and it often picks the transcript. This is the
   named anti-pattern from the memory-architecture literature (append-only transcript →
   bloat + contradictory truth).

## Research findings (industry model)

All well-built systems (ChatGPT, Claude, Gemini, MemGPT/Letta, Mem0, Zep, LangGraph, OpenAI
Threads) converge on a **tiered memory architecture**:

- **Tier 0 — Working context (per call)**: system + pinned constraints + active task list +
  a small recent-turns buffer. Volatile.
- **Tier 1 — Episodic (threads)**: *topic-scoped* conversations with a lifecycle —
  start → auto-title → summarize → close. Not time-bounded in consumer apps; bounded by
  **topic** (new thread on topic shift) with inactivity as the continuity rule.
- **Tier 2 — Long-term (persistent)**: typed, mutable structured state (entities, facts,
  decisions) maintained with ADD/UPDATE/DELETE. Retrieval-based, never re-fed whole.

Key practices:
- Transcripts are **working RAM, not disk**. Raw chat logs as memory = anti-pattern.
- **Auto-titling**: a lightweight model titles each thread after 1–3 turns.
- **Session-close extraction**: on thread close, a background pass extracts durable facts
  into the persistent store; the raw transcript becomes searchable, not re-injected.

## Where Rhodey already does this right

| Mechanism | Verdict |
|---|---|
| Entity-scoped threads (person/org/project), `active_anchor` | ✅ Topic-scoping, correct |
| Thread summaries every 3 exchanges (`_background_summary_check`, `_compress_to_classify_summary`) | ✅ Aging mechanism exists |
| All-exchange embeddings + `match_conversations` RPC | ✅ Recall layer exists |
| KG, decision table, learning loop, `classifier_corrections` | ✅ Real Tier-2 memory — the vision's engine |
| `delta_context` (🆕 NEW / 📍 moved off board, from live task snapshots) | ✅ Structured "what changed" |
| `format_classify_context()` — summary + last user turn only ("Replaces raw conversation history to prevent bot receipt leakage") | ✅ The bounded pattern EXISTS — but only classification uses it |
| Pulse / daily brief / query paths | ❌ Use raw unbounded `format_history_for_prompt` |

## Proposed model (see canonical doc for full detail)

1. **Topic-scoped general sessions**: route non-entity messages to topic threads using the
   existing topic machinery (`_check_topic_overlap`, `_entity_is_primary_topic`). Topic
   shift → new thread, auto-titled via a Flash Lite call. Backstops: 30-min inactivity rule
   (already exists) + a time cap (7d) so the bucket can never be eternal.
2. **Transcript demotion**: all prompt surfaces get recent 3–5 turns + current thread
   summary — never whole history. Long-term knowledge comes from the board, KG, delta,
   decisions.
3. **Close-extraction**: thread close → extract decisions/commitments → learning loop.
4. **Archival**: archived threads stay searchable (embeddings + `match_conversations`) but
   out of context; retention prune for very old rows.

## Implementation plan (proposed order)

- **Phase 1 — Context bounding** (kills the bug class at the data level):
  `core/lib/conversation.py` — new `format_episodic_context()` (summary + recent N turns);
  wire into `core/pulse/briefing.py`, `core/webhook/dispatch.py` (daily brief + query +
  anaphora), `core/webhook/handler.py`. Reuse `get_thread_summary()`.
- **Phase 2 — General-thread rotation**: `resolve_thread()` general fallback archives the
  current general thread when older than the cap and creates a fresh one.
- **Phase 3 — Topic scoping + auto-titling**: topic detection on fallback routing; Flash
  Lite auto-title stored on `conversation_threads.title`.
- **Phase 4 — Close-extraction + retention**: sentinel piggyback; extraction pass on
  archived threads; prune rows in threads archived >90 days.

## Open questions (for review)

- Topic-scoping on the general path: is `_entity_is_primary_topic`-style detection enough,
  or does routing need an LLM topic-classification step (cost/latency trade-off)?
- Rotation cap: 7 days? Activity-based instead (e.g., rotate when the thread exceeds N
  exchanges) so active multi-day topics aren't split mid-conversation?
- Does the app UI (conversation screen) need thread titles/grouping surfaced, or is the
  merged chronological view fine?
- Retention horizon for hard-prune (90 days? configurable?).

## Direction B — what shipped (Aug 5, 2026)

User directive: "I don't want patch offering." → implemented the state-driven memory
architecture, not the bounded-threads variant. Core principle: **the chat transcript is an
input log + audit trail; it is NOT Rhodey's memory. Rhodey's memory is structured state
(tasks, KG, decisions, deltas), plus a bounded last-2-user-turns window for immediate
follow-up coherence ("and the timeline?").**

### Files changed

| File | Change |
|---|---|
| `core/lib/conversation.py` | `format_classify_context()` — now the ONLY conversation-derived context block: ACTIVE ENTITY + last 2 user turns; thread-summary param removed. `GENERAL_THREAD_MAX_DAYS=7` rotation in `resolve_thread` general fallback (selects `created_at`; archives old bucket, starts fresh). Removed dead `format_history_for_prompt`. Summary machinery kept as episodic index (docstring updated). |
| `core/prompts/query.py` | `build_interrogate_brain_prompt` no longer takes/injects conversation_history. Deleted dead `build_anaphora_resolution_prompt` (encoded the removed anti-pattern). |
| `core/prompts/briefing.py` | Removed conversation_history + CONVERSATION TRUTH GUARD from pulse prompt, daily brief, and system instruction (guards became dead text). |
| `core/pulse/models.py` | Removed `conversation_history` from `BriefingContext`. |
| `core/pulse/briefing.py` | Removed the transcript-fetch block; `PREVIOUS SESSION` (last briefing, a state artifact) retained. |
| `core/webhook/dispatch.py` | `resolve_anaphora` no longer reads thread summaries; `route_by_intent` no longer builds full history; `handle_daily_brief`/`interrogate_brain` dropped the param; **deleted `_build_active_context` (147-line cross-thread awareness layer)**; `match_conversations` recall retained. |
| `core/webhook/handler.py` | `/today`, `?query`, `/note`, and the main path no longer feed transcript context. |
| Tests | `test_url_shortcut.py` — removed dead `format_history_for_prompt` patches (would have raised AttributeError). `test_thread_classification.py` — fixture no longer patches `get_thread_summary`; `test_s2`/`test_s7` flipped to assert thread summaries are EXCLUDED from prompts. |

### Verification

- `py_compile` + `ruff` clean on all changed files.
- `tests/unit/`: 138 passed; 7 failures **proven pre-existing** (identical on clean tree via
  stash — DB connection refused).
- `tests/sim/`: 20 failures **proven pre-existing** (identical on clean tree).
- Code review (deepseek-flash) findings addressed: the rotation select was missing
  `created_at` (rotation would never fire — fixed); thread summaries confirmed write-only
  (kept deliberately as episodic index, documented); `general.data=[]` mutation replaced
  with a local variable; dead code removed.

### Behavior changes (intended)

- A completed task can never be resurrected by a briefing/query — no transcript to carry it.
- Follow-ups referencing turns older than the last 2 exchanges lose chat-level context;
  long-term continuity comes from the board, KG, and entity context instead. This is the
  accepted trade-off of Direction B.
- General conversations now expire: the bucket rotates every 7 days.

### Deploy

Server-only: `modal deploy infra/modal_app.py`. No APK rebuild. Requires nothing in DB.

## Files touched this session (full list)

- `core/prompts/briefing.py` — truth-guard added then removed with the injection (net: prompt restored to state-only).
- Direction B changes above.
- Docs: `session-notes/70-thread-lifecycle-redesign.md` (this), `product-summary/60-thread-and-memory-architecture.md` (canonical, marked IMPLEMENTED).
