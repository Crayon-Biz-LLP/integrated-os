# 60. Thread & Memory Architecture (Redesign)

> **STATUS: Direction B core IMPLEMENTED (Aug 2026).** This document is the target
> architecture AND the record of what shipped. The transcript is no longer fed to any
> prompt — it is an input log + audit trail; only a bounded last-2-user-turns window
> survives for immediate follow-up coherence. See §6 for the shipped-vs-pending map.
> Current thread system (pre-change) reference: `29-conversation-threads-and-workflows.md`.
> Session record: `session-notes/70-thread-lifecycle-redesign.md`.
> Successor to the thread layer in `04b-intelligence-tiers.md` (session working memory).

## 1. Why this redesign exists

Two recurring bug classes trace to the same structural flaw:

1. **Eternal conversation.** The general thread for a chat is a single bucket reused
   forever; messages never expire; everything non-entity-scoped accumulates in it.
   (Reported: *"all messages were linked to the first message we sent"*.)
2. **Transcript-as-memory.** The pulse, daily brief, and query paths inject the **raw,
   unbounded conversation transcript** into the LLM prompt. The model is forced to arbitrate
   between transcript claims and live board state — and it repeatedly trusts the transcript.
   (Reported: a Wrap-up briefing named a task completed 3 days prior, repeating the original
   Aug 1 phrasing verbatim.)

This is the **append-only-transcript anti-pattern**: bloat plus contradictory truth. The
system already has the correct memory substrate (tasks, knowledge graph, decision table,
deltas, thread summaries, embeddings). The transcript keeps winning in the prompt anyway.

## 2. Design principles

P1. **The transcript is working RAM, not disk.** Raw chat logs are a *log of what was said*;
they are not Rhodey's knowledge. Rhodey's knowledge is structured state.

P2. **Threads are topic-scoped conversations with a lifecycle** — start → auto-title →
summarize → close. They are bounded by **topic and activity**, never allowed to grow into
eternal buckets.

P3. **Long-term memory is retrieved, never re-fed whole.** Archived threads and facts are
reached via retrieval (`match_conversations`, KG, summaries), not injected verbatim.

P4. **Structured state wins in every conflict.** When a conversation claim contradicts the
board, the board is truth. (Enforced in prompts by the CONVERSATION TRUTH GUARD and at the
data level by bounding what is injected.)

## 3. The tiered model

```
TIER 0 — WORKING CONTEXT (assembled per call)
  · system + persona + pinned constraints
  · the board (active tasks, calendar, entities, delta_context)   ← primary truth
  · episodic window: current thread summary + last 3–5 exchanges  ← coherence only
  · classification: THREAD SUMMARY + preceding turn (already the pattern)

TIER 1 — EPISODIC (threads)
  · entity threads (person/org/project) — unchanged, already topic-scoped
  · general sessions — NOW topic-scoped, auto-titled, rotated (see §4)
  · every exchange still logged + embedded for retrieval (unchanged)

TIER 2 — LONG-TERM (persistent, unchanged)
  · tasks, knowledge graph, decisions, patterns, practices
  · thread summaries as a searchable episodic index
  · match_conversations RPC for semantic recall
```

## 4. Thread lifecycle

### 4.1 Routing (resolve_thread — unchanged priorities, changed fallback)

```
Inbound message
  → Open workflow?        Yes → resume thread with its anchor
  → Exact entity match?   Yes → existing entity thread
  → No existing thread?   Yes → create entity thread
  → Prior bot question?   Yes → last active thread (question pending)
  → Fallback general      → topic-scoped general session (see 4.2)
```

### 4.2 General sessions (the fix)

The general fallback no longer reuses one eternal bucket:

1. **Topic scoping**: an incoming non-entity message is matched against the most recent
   general session(s) using the existing topic machinery (`_check_topic_overlap`,
   `_entity_is_primary_topic`). Strong match → continue that session. Otherwise → new session.
2. **Auto-titling**: a Flash Lite call titles each general session after its first turn
   (e.g., "Bosch toolkit", "Insurance call"), stored on `conversation_threads.title`.
3. **Rotation backstops**:
   - *Inactivity*: a session older than 30 min with no pending bot question is not resumed
     (rule already exists).
   - *Time cap*: a general session older than the cap (proposed **7 days**) is archived on
     next fallback and a fresh session created. The bucket can never be eternal.
   - *Size cap (open question)*: alternatively/probably additionally rotate on an exchange
     count so an intense multi-day topic isn't split mid-conversation.

### 4.3 Context assembly rules (what each surface sees)

| Surface | Episodic context | Structured context |
|---|---|---|
| Pulse briefing | current session summary + last 3 turns | board, `delta_context`, entities, decisions |
| Daily brief | session summary + last 3 turns | calendar, ACTIVE TASKS, RECENTLY COMPLETED (24h) |
| Query / interrogate_brain | session summary + last 3–5 turns | entity context, KG, retrieval |
| Anaphora resolution | last 2–3 turns only | — |
| Classification | THREAD SUMMARY + preceding turn (unchanged — already correct) |

Rule: **no raw transcript older than the current session's window is ever injected.** Old
topics are reachable only via `match_conversations` / KG / summaries.

### 4.4 Close-extraction and retention

- **Close-extraction** (sentinel piggyback): when a thread is archived, a background pass
  extracts durable facts (decisions made, commitments given, preferences stated) and writes
  them to the structured stores — feeding the learning loop, per the vision
  ("learns from every decision you make").
- **Retention**: archived threads remain searchable (embeddings, summaries). Rows in threads
  archived > retention horizon (proposed 90 days) are hard-pruned to bound storage.

## 5. Current-state mapping

| Piece | Keep | Change |
|---|---|---|
| Entity threads + `active_anchor` | ✅ | — |
| `_background_summary_check` (summary every 3 exchanges) | ✅ | — |
| All-exchange embeddings + `match_conversations` | ✅ | — |
| `format_classify_context` (summary + last turn) | ✅ | become the shared pattern |
| `format_history_for_prompt` (raw, unbounded) | ❌ | replaced by episodic window (§4.3) |
| `resolve_thread` general fallback (eternal bucket) | ❌ | topic-scoped + rotated (§4.2) |
| Sentinel H1/H2 (archive by inactivity) | ✅ | + time-cap rotation, close-extraction, retention prune |
| CONVERSATION TRUTH GUARD (prompts) | ✅ | defense-in-depth on remaining windows |

## 6. Migration plan — shipped vs pending

**SHIPPED (Aug 2026):**
- **Phase 1 — Context bounding (DONE).** `format_classify_context()` in
  `core/lib/conversation.py` is now the ONLY conversation-derived context block
  (ACTIVE ENTITY + last 2 user turns). Wired everywhere: pulse (`core/pulse/briefing.py`),
  daily brief + query + anaphora (`core/webhook/dispatch.py`), handler
  (`core/webhook/handler.py`), prompt builders (`core/prompts/query.py`, `briefing.py`).
  The cross-thread awareness layer (`_build_active_context`) was **deleted**. Raw
  `format_history_for_prompt` was removed (zero callers). `match_conversations` semantic
  recall is retained (relevance-bounded, tagged `[BACKGROUND — NOT a current task]`).
- **Phase 2 — General-thread rotation (DONE).** `resolve_thread` general fallback archives
  a general thread older than `GENERAL_THREAD_MAX_DAYS` (7) and starts a fresh session.
  The eternal bucket can no longer exist.

**RETIRED BY DESIGN (Direction B):**
- **Phase 3 — Topic scoping + auto-titling.** Consumer-chat pattern-matching with no value
  for a state-driven Chief of Staff; the transcript is an input channel, not a chat sidebar.
  Routing stays entity-based (`resolve_thread` priorities unchanged).

**PENDING:**
- **Phase 4 — Close-extraction + retention prune (sentinel).** Extract durable facts from
  closing threads into the learning loop; prune rows in threads archived > retention
  horizon. Thread summaries remain written (every 3 exchanges) as the episodic index for
  this future pass.

## 7. Success criteria

1. A task completed >24h ago is never named as pending by any briefing or query (regression
   test: the Bosch scenario).
2. The general thread for an active chat contains only the current session's exchanges
   (never >7 days of history).
3. No prompt surface receives raw transcript older than the current session window.
4. Follow-up coherence ("reschedule the 2pm", "and the timeline?") still works — covered by
   the episodic window + summary.
5. Archived conversations remain searchable via `match_conversations` (regression: existing
   retrieval behavior unchanged).

## 8. Open questions

1. Topic detection on the general path: heuristic (`_check_topic_overlap`) sufficient, or an
   LLM topic-classification step (latency/cost)?
2. Rotation triggers: time cap (7d), exchange count, or both?
3. Should `conversation_threads.title` be surfaced in the app UI (grouped conversation view)?
4. Retention horizon for hard-prune: 90 days, or configurable?
