# Part 62: Hardened Thread Layer — Person Routing, Awareness Layer & Auto-Archive

> **Date**: Jul 20-21, 2026
> **Parts**: 62 (Pre-Phase 2 infrastructure)
> **Files changed**: `core/lib/conversation.py`, `core/webhook/dispatch.py`, `core/pulse/sentinel.py`, `scripts/backfill_thread_entity_labels.py`

## Overview

Six fixes (A-D, H1-H2) that complete the thread layer foundation. The system now supports **person-scoped conversation threads**, **eager summaries** on every thread, **all-exchange embeddings** (not just QUERY), a **cross-thread awareness layer** that scans recent threads at query time, and **auto-archive** of stale entity threads.

Together, these deliver the "Chief of Staff" feel — Rhodey knows about other active conversations and can reference them when answering.

## What Changed

### Fix A: Eager Summary Generation
**File**: `core/lib/conversation.py` — `_background_summary_check()`

**Before**: Summaries generated lazily only when thread exceeded 5000 tokens. Some threads had no summary. Summaries were write-once (never updated).

**After**: Summaries generated every 3rd user exchange regardless of overflow. Always updated (`_store_thread_summary()` replaces `_store_thread_summary_if_missing()`). Uses `_compress_to_classify_summary()` (topic-only, no action receipts).

**Cost**: One async Flash Lite LLM call every 3 exchanges. Fire-and-forget, zero latency impact.

### Fix B: Person Entity Thread Routing
**File**: `core/lib/conversation.py` — `_resolve_person_candidates()` (NEW)

**Before**: `_fetch_entity_candidates()` only matched organizations and projects. "Back to Anita" fell through to the general thread with noisy context from other topics.

**After**: New `_resolve_person_candidates()` queries `graph_nodes` with `type='person'`, uses n-gram primary-topic detection (`_entity_is_primary_topic()`) to find the best candidate. Returns score 75 (below orgs at 80 and projects at 90). Wired into `_fetch_entity_candidates()` after org/project matching.

**Behavior**: "Anita" → Anita's person-scoped thread. "Marcus from Ashraya" → Ashraya (org) thread wins on score.

### Fix C: All-Exchange Embeddings
**File**: `core/lib/conversation.py` — `_store_exchange_embedding()` (NEW), wired into `log_exchange()`

**Before**: Embeddings only stored for QUERY exchanges (inside `interrogate_brain()`). TASK, NOTE, COMPLETION, PROJECT_UPDATE exchanges invisible to `match_conversations` RPC.

**After**: Every user exchange is embedded fire-and-forget, regardless of intent. The Phase 1 `match_conversations` RPC can now find semantically similar exchanges across all types.

### Fix D: Cross-Thread Awareness Layer
**File**: `core/webhook/dispatch.py` — `_build_active_context()` (NEW), `_fetch_active_context()` wrapper

**New mechanism**: Parallel Phase 2 task that:
1. Fetches all conversation threads with `last_active_at > 24h ago` (excludes current thread)
2. Scans their summaries for entity overlaps with the current query
3. Falls back to raw exchange text if no summary (last 2 exchanges)
4. Injects a **`ACTIVE CONVERSATION CONTEXT`** section into the LLM prompt

**Example output**:
```
[Active Conversation Context]
The following conversations are also active:
→ Anita Hariharan (person): Coordinating Ashraya audit paperwork and Form 16A tax documentation
→ FC Madras (organization): Website upgrade, marketing strategy PDF shared via WhatsApp
```

**Cost**: One parallel DB query. Zero additional latency (runs in Phase 2 alongside other tasks).

### H1: Auto-Archive Stale Threads
**File**: `core/pulse/sentinel.py` — new piggyback section

**What**: Every sentinel cycle (5 min), scans for entity threads with `last_active_at > 7 days` and `archived_at IS NULL`. Sets `archived_at` to now. Excludes the general thread. Limited to 50 per cycle.

**Why**: Prevents unbounded accumulation of stale entity threads. A thread about a one-off person (e.g., "Tell me about Judas Iscariot") auto-archives after 7 days instead of persisting forever.

### entity_label Fix
**File**: `core/lib/conversation.py` — `resolve_thread()` thread creation

Added `'entity_label': best.get('entity_name', '')` to the thread insert. All three resolvers (`_resolve_entity_to_candidates`, `_resolve_person_candidates`, `_llm_entity_disambiguation`) already populate `entity_name` in their candidate dicts — the thread creation code just wasn't passing it through.

## Backfills Executed

| Backfill | Rows | Status |
|---|---|---|
| **H2: Thread entity labels** | 6 updated, 9 skipped | ✅ Done |
| **H3: Non-QUERY exchange embeddings** | 1 exchange processed | ✅ Done |

## Key Files

| File | Change |
|---|---|
| `core/lib/conversation.py` | Fix A (eager summaries), Fix B (person routing), Fix C (all-exchange embeddings), entity_label fix |
| `core/webhook/dispatch.py` | Fix D (awareness layer — `_build_active_context()` + Phase 2 wiring) |
| `core/pulse/sentinel.py` | H1 (auto-archive stale threads piggyback) |
| `scripts/backfill_thread_entity_labels.py` | H2 (backfill script — NEW) |

## Live Verification

- **"back to Anita"** → Creates person-scoped thread with `entity_label='Anita Hariharan'`
- **"Marcus from Ashraya"** → Falls to general thread (org score wins)
- **Auto-archive** → Found 5 stale targets on first run
- **Ruff clean**, code reviewer clean, no regressions
