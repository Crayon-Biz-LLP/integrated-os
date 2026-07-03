# 29. Conversation Threads & Workflow Engine

How Rhodey maintains conversational persistence across turns, enabling follow-up replies hours or days later without losing context.

## The Gap

Before this subsystem, every message was processed independently. The classifier determined intent from scratch each time. A user asking "what's the status on Armour Cyber?" and then replying "and the timeline?" would get two unrelated responses — the follow-up had no context of the prior query.

## Architecture

Three phases, built incrementally:

### Phase 1 — Persistent Threads (`core/lib/conversation.py`)

**Schema** (`db/09_conversation_threads.sql`):
- `conversation_threads` — durable conversation records with `id` (UUID PK), `chat_id`, `thread_type` (general/entity/workflow), `entity_type/id/label`, `active_anchor` (JSONB), `created_at/last_active_at`, `archived_at`, `summary`
- `conversation_workflows` — pending action workflows linked to threads

**Routing Chain** (`resolve_thread()`):

```
Inbound message
  → Open workflow?        Yes → resume thread with its anchor
  → Exact entity match?   Yes → existing entity thread
  → No existing thread?   Yes → create entity thread
  → Prior bot question?   Yes → last active thread (question pending)
  → Fallback general      → existing or new general thread
```

Each routing decision is logged to `audit_logs` with reason: `workflow_resume`, `exact_entity_match`, `prior_bot_question`, or `fallback_general`.

**K2 fallback contract**: When the entity resolver or LLM disambiguation fails, the routing path falls through to a general thread (inner catch) or a brand-new UUID session (outer catch). Neither path sends a user-visible error or receipt. See `core/FALLBACK_CONTRACTS.md` — K2 section for exact assertions.

**Legacy compatibility**: `get_or_create_session()` now maps transparently to thread IDs. All existing `log_exchange()` calls work unchanged.

### Phase 2 — Workflow State Engine (`core/webhook/workflows.py`)

**Producer wiring**: When `handle_project_update()` sends a question like "Add this to your calendar?", it creates a `conversation_workflows` row with `status=active`, `awaiting_user_input=true`, `expires_at=24h`.

**Consumer precedence**: Before classification, `handler.py` checks `check_and_resume_workflow()`. If an active workflow exists, the user's reply is evaluated against it first.

**Deterministic fast path** (`get_deterministic_decision()`):
- Set-based exact match: `CONFIRM_PHRASES` (yes, do it, go ahead, sure, ok, okay, yeah, please, absolutely) and `DECLINE_PHRASES` (no, nope, cancel, skip, nevermind, ignore, stop)
- Short-phrase heuristic (≤4 words): checks if any word is confirm/decline without mixing signals
- Bypasses LLM entirely for obvious replies

**LLM fallback**: For ambiguous replies (>4 words, mixed signals), evaluates via `CLASSIFICATION_MODEL` with JSON output: `confirm | decline | unrelated`.

**Unrelated note preservation**: Unrelated replies bypass the workflow without cancelling it. The workflow stays active until explicit decline, explicit cancel, or 24h expiry. This prevents: bot asks "Add to calendar?" → user sends "Marcus approved pricing" → workflow stays active → 2h later user says "yes" → action still executes.

**Atomic idempotency**: Update includes `.eq('status', 'active')` — concurrent resolution or Telegram retries cannot double-execute.

**Expired workflow pruning**: Sentinel piggyback marks workflows past `expires_at` as `expired`, preventing stale state accumulation.

### Phase 3 — Query Carry-Forward (`core/webhook/dispatch.py:interrogate_brain`)

**Persistent anchor**: When `interrogate_brain()` resolves an entity during a query (e.g., "what's happening with Armour Cyber?" → resolves to graph node `{id, name}`), it saves `active_anchor` to `conversation_threads`. The next query in the same thread loads this anchor via `resolve_thread()`.

**Anaphora prompt enhancement**: The entity resolution prompt now includes `Active entity context` — so a follow-up query "and the timeline?" sees the prior anchor and resolves "the" correctly without re-deriving from raw history.

**Carry-forward flow**: First query "what's happening with Armour Cyber?" → resolves anchor → persists to thread → stores bot reply in conversation history. Follow-up "and the timeline?" → same thread → loads anchor → anaphora resolves "the" → scopes search to Armour Cyber context.

### Phase 4 — Structured Anchor & Thread Summarization (Jun 28, 2026)

**Richer `active_anchor`**: The anchor is no longer a bare `{id, name}` dict. It now carries:
- `type` — resolved from `graph_nodes.type` (person, organization, project, etc.)
- `last_action` — status of the most recent non-done/cancelled task mentioning this entity
- `last_task_id`, `last_project_id`, `last_org_id` — foreign keys for drill-down
- `last_summary_snippet` — most recent memory featuring this entity (200 chars)
- `last_mentioned_at` — ISO timestamp of when the anchor was last set

Built in `_build_rich_anchor()` (`dispatch.py:895-924`).

**Anaphora prompt enhancement**: The entity resolution prompt now receives:
```
Active context: Equisoft (Type: organization)
Last activity: in_progress
Recent context: Equisoft pushed IAM recert timeline by 2 weeks
Earlier in conversation: Danny said "Prasad has bandwidth" ...
```

This lets the LLM rewrite "what's the status on that?" → "what's the status on the Equisoft IAM recertification deadline?" with concrete context instead of guessing from a name string.

**Thread summarization (`conversation.py`)**:
- When conversation history exceeds 5000 tokens, overflow pairs are compressed into an extractive summary (key user messages concatenated, capped at 800 chars)
- Summary stored on `conversation_threads.summary`
- Loaded by `get_thread_summary()` and injected into the anaphora prompt
- Compression happens lazily — only on first overflow per thread
- History window raised from 2000 → 5000 tokens (~5-8 exchanges preserved)

**No API or schema changes**: `active_anchor` is already JSONB; new keys merge in without migration.

### Phase 5 — Classification Context Boundary (Jul 3, 2026)

**Problem**: The classifier received raw `CONVERSATION HISTORY:` containing full user-bot exchange pairs. Bot receipts like "Repository link logged for the project vault. Now go be a dad." were present in the same context block as the new user query "Who is Binu?" — causing the classifier to misclassify the person query as NOTE by pattern-matching the URL receipt phrase.

**Fix — `format_classify_context()`** (`core/lib/conversation.py`):
Replaces raw history with a bounded context block containing only:

```
CONVERSATION HISTORY:
THREAD SUMMARY: User shared a repository link which was archived.
ACTIVE ENTITY: Binu (person)
PRECEDING TURN:
User: Who is Binu?
```

- **Bot responses are never included.** Only the last preceding user message survives.
- **`THREAD SUMMARY`** is an abstractive topic-only summary generated by a separate `_compress_to_classify_summary()` LLM call (gemini-3.1-flash-lite) with a prompt that explicitly forbids capturing actions, receipts, or outcomes.
- **`ACTIVE ENTITY`** is rendered only if both `name` and `type` are present, preventing stale anchors from leaking.
- **`PRECEDING TURN`** contains only the last user message for pronoun resolution (e.g., "What about that meeting?" → "Add Equisoft meeting Mon").
- **Background summary generation**: Fired from `log_exchange()` after bot response insert if thread has ≥2 user exchanges and summary doesn't exist. Idempotent via `.is_('summary', 'null')`. Fail-open — errors logged to audit_logs, never block user flow.
- **The `CONVERSATION HISTORY:` header preserved** so existing classify prompt rules ("Use the CONVERSATION HISTORY block above to disambiguate vague follow-ups") still match.

**Classifier prompt guards** (`core/prompts/classify.py`):
- Added `PERSON QUERIES` rule: "Who is [name]?" → always QUERY, never NOTE.
- Tightened `URL-ONLY` rule: only matches `^https?://\S+$` regex, with "NEVER use this receipt" guard.

**7-test verification suite**: S1-S7 covering URL + person query, summary presence, empty history, entity anchor, pronoun continuation, multi-turn receipt stripping, and full end-to-end thread resolution.

## Storage

All paths funnel through `core/lib/conversation.py`:

| Data | Table | Purpose |
|------|-------|---------|
| Threads | `conversation_threads` | Durable context for routing |
| Workflows | `conversation_workflows` | Pending action state |
| Exchange history | `conversations` | User/bot exchange pairs |
| Routing decisions | `audit_logs` | Debugging and recall analysis |

## Integrity Safeguards

- **Multiple workflow detection**: If >1 active workflow per chat, older ones are marked `cancelled`, processing falls open to normal
- **Atomic status guard**: Workflow cannot be resolved twice (`.eq('status', 'active')` on update)
- **24h expiry**: Orphaned workflows self-clean via sentinel
- **Thread archival**: Schema has `archived_at` and `summary` ready for future 30-45 day archival window
