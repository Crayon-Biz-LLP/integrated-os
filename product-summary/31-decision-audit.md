# Decision Audit (/why)

Rhodey OS includes a transparent decision auditing system that allows the user to interrogate the bot about its most recent response. This is critical for debugging hallucinations, context leaks, and misclassifications in production without needing to query the database manually.

## How it works

The decision audit system tracks the lifecycle of a single user request across four key stages:
1. **Classification**: What intent and entities did the LLM extract from the user's message?
2. **Routing**: Which internal subsystem was chosen to handle the request?
3. **Context Filter**: Which memories/tasks were kept or excluded during entity grounding, and why?
4. **Retrieval**: Which data sources were consulted before generating the final answer?

### The `decision_chain_id`

When a request enters the webhook (`process_webhook`), a unique `decision_chain_id` (UUIDv4) is generated and stored in a Python `contextvar`. This ID flows implicitly through the entire async execution context.

As the request passes through the four stages, the code calls `log_decision()`, which writes a structured JSON record to the `audit_logs` table (with `service='decision_audit'`).

When the bot finally sends its response via `send_telegram()`, the active `decision_chain_id` is persisted to the `conversation_threads` table under the `last_decision_chain_id` column.

### Triggering the Audit

The user can trigger the audit by replying to the bot or sending a new message with:
- `/why`
- "why did you..."
- "how come..."
- "explain why..."

This bypasses the normal classification flow and hits `handle_why()`. The system:
1. Resolves the active thread.
2. Fetches the `last_decision_chain_id`.
3. Queries all `decision_audit` records for that chain.
4. Formats them into a readable Telegram message.

## Reason Codes

The context filter stage uses standardized reason codes to explain why specific context items were excluded from the LLM prompt:
- `no_entity_overlap`: The item lacked any of the entities mentioned in the query.
- `below_threshold`: The semantic similarity score was too low.
- `top_k_truncated`: The item passed all gates but was cut by the hard limit (e.g., top 12).
- `hard_gate_rejected`: The strategy required an exact entity match, and none was found.

By surfacing these reason codes in the `/why` response, the user can instantly see if a hallucination was caused by a context leak (bad filtering) or an LLM inference error (bad generation).
