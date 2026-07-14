# WhatsApp Batch Ingest — Conversation Batching

## Problem
Each individual WhatsApp message was stored as a separate `messages` row with `danny_decision=NULL`, requiring individual approval in the Decision Pulse. A 5-minute conversation produced 15–20 items for manual approval — redundant.

## Solution
**Batch at ingest**: Same-sender messages within a 3-minute window get merged into one row via an atomic Postgres RPC.

## Architecture

### Database RPC: `batch_whatsapp_message()`
**File:** `db/21_whatsapp_batch_rpc.sql`

Uses `pg_advisory_xact_lock(hashtext(p_sender_id))` to serialize concurrent messages from the same sender:

```
Msg 1 arrives → LLM classify → RPC (acquires lock, no pending row found → INSERT)
Msg 2 arrives 30s later → LLM classify → RPC (acquires lock, finds msg1's row → APPEND body)
Msg 3 arrives 1min later → LLM classify → RPC (acquires lock, appends)
```

**The RPC does two things atomically:**
1. **Batch path** (pending row exists within 3 min window): appends `body || E'\n---\n' || new_body`, upgrades classification to `actionable` if the new message is actionable (even if existing was `fyi`)
2. **Insert path** (no pending row): creates a new `messages` row with all fields

### Concurrency Safety
| Concern | Mitigation |
|---------|-----------|
| Two messages arrive simultaneously | `pg_advisory_xact_lock` serializes per-sender — second caller waits, sees first's insert, appends |
| Lost update (read body, write body) | Lock guarantees no two transactions read-modify-write concurrently |
| Message loss on lock wait | Lock is held only for the RPC execution (~10ms), not the LLM call. LLM classification happens *before* the lock |
| `received_at` semantics | Unchanged for batched rows — the original timestamp from the first message is preserved |

### Classification Behavior
- **First message in burst**: classified by Gemini normally
- **Subsequent messages**: their classification is used only to upgrade the existing row (actionable > fyi). No LLM call is repeated.
- **Batched row shows in Decision Pulse as one item**: the full conversation transcript goes to `process_single_dump()` when approved

### Files Changed
| File | Change |
|------|--------|
| `db/21_whatsapp_batch_rpc.sql` | NEW — `batch_whatsapp_message()` RPC with advisory lock |
| `core/skills/whatsapp_ingest.py` | REPLACED Python-side inserts with single `supabase.rpc('batch_whatsapp_message', ...)` call |

## Key Design Decisions
1. **Advisory lock over application mutex**: DB-side lock survives serverless cold starts and concurrent invocations. No external dependency.
2. **3-minute window**: Covers typical conversation pacing. Adjustable in the RPC (change `INTERVAL '3 minutes'`).
3. **Upgrade-only classification**: Batched rows never downgrade (actionable → fyi). Highest priority wins.
4. **`received_at` frozen** at first message's timestamp. Prevents ordering drift in Decision Pulse.
5. **Ignored messages bypass the RPC**: Inserted directly with `danny_decision='skipped'` — no batch window needed since they never enter the approval flow.
