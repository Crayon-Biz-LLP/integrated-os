# Classification Context Boundary

**Date**: Jul 3, 2026 | **Phase**: 13, T-CLASSIFY-001 | **Commits**: ~5

## Problem

Persistent threads leaked bot response context into the classify prompt. "Who is Binu?" following a URL was misclassified as NOTE with receipt "Repository link logged for the project vault. Now go be a dad." — because the bot receipt was present in the `CONVERSATION HISTORY:` block alongside the user message. The classifier pattern-matched the URL receipt phrase as context for the new message.

## Solution

### `format_classify_context()` (core)

Replaced raw `CONVERSATION HISTORY:` with a **bounded context block** containing only:
1. Optional `THREAD SUMMARY:` — topic-only summary (no actions, receipts, or bot responses)
2. Optional `ACTIVE ENTITY:` — `name (type)` from graph nodes
3. `PRECEDING TURN:` — `User: <last user message only>`

**Bot responses are never included in classify context.** The header label is kept as `CONVERSATION HISTORY:` so existing classify prompt rules still fire.

### Summary Pipeline

- `_compress_to_classify_summary()` — Separate LLM call (gemini-3.1-flash-lite) with tight topic-only prompt. Forbidden from including specific actions, receipts, or outcomes.
- `_store_thread_summary_if_missing()` — Idempotent write via `.is_('summary', 'null')` guard.
- `_background_summary_check()` — Non-blocking async job fired after bot response insert. Errors logged but never crash the request.

### Prompt Hardening

- Added `PERSON QUERIES` rule: "Who is [name]?" → always QUERY, never NOTE
- Tightened `URL-ONLY` regex with "NEVER use this receipt" guard
- Fixed `\S` escape sequence in regex

### Tests

7 simulation tests (S1-S7) covering:
- URL + person query
- Summary present/absent
- Empty history
- Entity anchor in context
- Pronoun continuation
- Multi-turn bot receipt stripping
- Full end-to-end with real Supabase thread

## Key Files

| File | Purpose |
|------|---------|
| `core/lib/conversation.py` | format_classify_context(), summary pipeline |
| `core/prompts/classify.py` | PERSON QUERIES rule, URL guard |
| `core/webhook/handler.py` | Both classify paths use format_classify_context |
| `tests/sim/test_thread_classification.py` | 7 sim tests |

## Related Docs

- [Conversation Threads & Workflows](29-conversation-threads-and-workflows.md)
