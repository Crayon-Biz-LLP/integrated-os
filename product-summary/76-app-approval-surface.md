# 76. App Approval Surface (Rhodey)

> Verified 2026-08-15. The Flutter app's decision/approval UI — the surface
> where the learning loop (doc 71) meets the user's thumbs.

## Quick Confirmations

The inbox (`lib/screens/inbox_screen.dart`) is the approval hub:

- **Type filtering** — filter pending items by channel/type (task, email,
  WhatsApp, call, graph) instead of one mixed wall.
- **Selection-mode batch approve/reject** — select multiple items, act on all
  at once. Server-side, these batch paths use the parallel LLM pipeline with a
  no-retry contract (`core/actions/executor.py`) and record **per-item**
  decisions — batch is not a blunt instrument, each item still trains its
  subsystem.
- **Channel batch approve** — one tap approves every pending item for a given
  channel (email/WhatsApp/Teams/call).

## Real priority buckets

The inbox renders actual priority buckets (overdue / due today / upcoming /
snoozed) rather than a flat list — decisions are surfaced by *when they matter*,
matching the vision's "when to show what".

## Beeper reply flow

WhatsApp replies now round-trip through the Beeper bridge (`core/skills/beeper_*`
+ `core/services/reply_delivery.py`) — approving a WhatsApp-suggested task can
acknowledge the sender on the original thread.

## Draft editing

Suggested email drafts (`email_drafts`) are editable before approval — approve
the draft, not just the suggestion.

## Voice-rendered acknowledgments

Acks are rendered through the voice path (`core/services/message_voice.py`,
`message_voice.py`) — Rhodey speaks confirmations in persona, and replies don't
depend on Telegram (`core/services/reply_delivery.py`).

## Per-item undo (side-effect reversal)

Every manual approve/reject is **reversible**: the UI offers per-item undo, and
the backend reverses the side effects through the `undo_*` decision flow —
task re-opened, graph edge demoted (`emit_undo_correction`), Google sync
un-applied where reversible. Undo is itself a ledger decision (`superseded_by`
chain in `decisions`), so the audit trail stays honest.

## Suggestion Cards (Active Ingestion)

For active ingestion (App Chat messages & Document Uploads), the UI presents a **Suggestion Card** (`suggestion_card.dart`). 
- **Smart Threshold:** Cards only interrupt the user if the backend detects *new* entities to learn, or multiple tasks to review. Simple tasks with known entities auto-execute silently.
- **Unified Confirm:** Both document and chat suggestions are confirmed via a single endpoint (`/api/suggestions/confirm`).
- **WYSIWYG Entity Linking:** The backend caches the exact `EntityContext` inside the card's `raw_dumps` metadata. When the user confirms, their UI choices (like `merge_with` an existing entity) are natively merged into this context, ensuring the tasks created are linked exactly as shown on the screen, without double LLM extraction.

## Where the routes live

- Backend: `/api/auto-decisions/*` (confirm/reject/undo) in `api/index.py` +
  `core/webhook/feedback_loop.py`; graph callbacks in `core/webhook/graph.py`.
- Frontend verify/reject routes prefer `metadata.learn_features` for the
  correct subsystem (fallback: source→subsystem map for pre-fix decisions).
- Tests: `tests/unit/test_decision_undo.py`, the app widget tests, and the
  on-device integration tests cover the flow.
