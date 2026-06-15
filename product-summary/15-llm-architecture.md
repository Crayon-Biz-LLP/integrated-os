# 15. LLM Architecture & Model Registry

## Core Architecture (Phase 1/2)
The project is currently migrating to a unified LLM module (`core/llm/`) to centralize rate limiting, deadline budgets, circuit breaking, and degraded payload semantics (Safe Hold). 
Phase 1 implements the new engine and Phase 2 delegates non-tool-calling paths to it via `compat.py`. 
`call_llm_with_fallback` is being retained for tool-calling/Pydantic features until Phase 4.

### Safe Hold Behavior

When the classification LLM fails or falls back entirely (e.g., network error, rate limit), the system does not surface a `CLARIFICATION_NEEDED` intent that would require user disambiguation. Instead, `core/llm/constants.py` defines `SAFE_HOLD_CLASSIFICATION` which emits `{"intent": "NOTE", "confidence": 1.0}`. This means degraded classifications are silently vaulted as notes with embeddings — the content is persisted but never pollutes the task pipeline with false positives.

### Workload Profiles & Timeouts

Defined in `core/llm/config.py`. Each workload profile specifies `timeout_s`, `max_retries`, and `limiter_mode`:

| Profile | timeout_s | max_retries | limiter_mode | Used By |
|---------|-----------|-------------|-------------|---------|
| `INTERACTIVE` | 55s | 3 | consume_deadline | Webhook classification, Telegram responses |
| `SYNTHESIS` | 300s | 4 | wait | Pulse briefing, canonical page generation |
| `BATCH` | 300s | 5 | wait | Nightly brain synthesis, backfill jobs |
| `EMBEDDING` | 120s | 3 | consume_deadline | All embedding generation (memories, resources) |

Timeout values were increased (from 15s/45s/120s/10s respectively) to align with infrastructure limits and prevent premature timeout failures on slow Gemini responses.

## Models Used

| Model | Purpose | Provider | Context |
|-------|---------|----------|---------|
| `gemini-3.5-flash` | Pulse briefing generation | Gemini | Primary LLM for the intelligence cycle |
| `gemini-3.1-flash-lite-preview` | Intent classification, email classification | Gemini | Fast, cheap classification tasks |
| `gemini-embedding-2-preview` | Text embeddings (768-dim) | Gemini | All vector search operations |
| `gemma-4-31b-it` | LLM fallback | OpenRouter | Second-tier fallback for graph backfill |
| `nvidia/nemotron-3-super-120b-a12b:free` | LLM fallback (free tier) | OpenRouter | Last-resort fallback |

## The Triple Fallback Chain

Every LLM call goes through a structured retry chain with exponential backoff:

### For Briefing & Critical Calls (Pulse Engine)
```
1. Gemini 3.5 Flash → 3 retries (1s/2s/4s backoff for 503s)
2. If ALL Gemini retries fail → fallback defaults (empty arrays, raw text briefing)
```

### For Graph Backfill (Backup Provider Chain)
```
1. Gemini → 2 retries (8s delay)
2. Gemma → 1 retry (4s delay) 
3. OpenRouter → 1 retry (4s delay)
```

### Retryable vs. Non-Retryable Errors

```python
RETRYABLE = {503, 504, 500, 'disconnected', 'timeout', 'deadline_exceeded',
             'unavailable', 'overloaded', 'rate limit', 429}
NON_RETRYABLE = {401, 403, 400, 'invalid'}
```

Non-retryable errors (auth failures, bad requests) are logged immediately without retry.

### Embedding LRU Cache

To avoid redundant embedding API calls, `core/llm/embedding.py` maintains a process-lifetime LRU cache using `collections.OrderedDict`:

- **Keyed by**: `md5(text)` — fast string hash, no per-process coordination needed
- **Max size**: 500 entries (peak pulse workload is ~30 unique texts)
- **Eviction**: Least-recently-used entries are dropped when cache is full
- **Saves**: 2–6 seconds per pulse run by deduplicating embedding requests for repeated memory/resource content

If the embedding call still fails, the system falls back to a zero vector and queues to `failed_queue` (existing resilience path, unchanged).

### Rate Limiting

A distributed sliding window rate limiter (`core/lib/rate_limiter.py` backed by Upstash Redis) protects the Gemini free tier (15 RPM limit) across all serverless instances. If Redis is unavailable, the limiter falls back to an in-memory `SlidingWindowLimiter`:

```python
flash_lite_limiter = SlidingWindowLimiter(max_calls=12, per_seconds=60)
```

The limiter operates at 12 RPM (leaving 3 RPM headroom) and supports both sync and async contexts:
- `acquire()` — uses `time.sleep()` for synchronous code
- `acquire_async()` — uses `await asyncio.sleep()` for async code

## Model Registry

Every LLM call is logged to the `model_registry` table:

| Column | Example |
|--------|---------|
| `model_name` | gemini-3.1-flash-lite-preview |
| `provider` | gemini |
| `input_tokens` | 1,234 |
| `output_tokens` | 567 |
| `latency_ms` | 2,345 |
| `success` | true |
| `error_message` | null |

This enables cost tracking across providers, performance comparison, latency monitoring, and failure rate analysis per model.

## Embedding Strategy

All embeddings use `gemini-embedding-2-preview` with 768 dimensions. Embeddings are stored in:
- `memories.embedding` (VECTOR(768)) — primary semantic search
- `canonical_pages.embedding` (VECTOR(768)) — master page search
- `raw_dumps.embedding` (VECTOR(768)) — staging area search
- `resources.embedding` (VECTOR(768)) — resource similarity search

### Embedding Resilience

If embedding generation fails, the system returns an `EmbeddingResult(degraded=True)` which defaults to a zero vector `[0] * 768` (prevents crashes). Downstream systems see this and:
1. Set `embedding_status='failed'` on the memory
2. Queue the operation to `failed_queue` for retry
3. The Janitor workflow monitors failed embeddings and triggers retries

## Native Control Layer (Tool Calling & Prompt Mutation)

To guarantee deterministic outputs and prevent API rate-limit hammering, the system natively implements a control layer directly in `call_llm_with_fallback` and `run_agent_loop`:

1. **Tool Registry & Function Calling**: Instead of returning a monolithic JSON schema, the main pulse engine is equipped with a `ToolRegistry`. The LLM issues discrete function calls (e.g., `update_task_status`, `create_project`) which are validated against Pydantic models.
2. **Targeted Prompt Mutation**: Instead of blindly retrying, the engine appends dynamic correction hints to the prompt. If the model hallucinations a tool name or violates a constraint, the error is fed back dynamically.
3. **Jittered Backoff**: If validation or network calls fail, the retry loop applies `asyncio.sleep()` with an exponential backoff + random jitter (`_jitter()`). This prevents hammering the Gemini API and triggering 429 Rate Limits.
4. **Vercel-Friendly**: Built purely with native `async`/`await` primitives, ensuring zero event-loop blocking during the 60s serverless execution window.
