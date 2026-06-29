# 15. LLM Architecture & Model Registry

## Unified LLM Module (`core/llm/`)

All LLM interactions flow through a centralized `core/llm/` module. This is the **single source of truth** — no file should create its own Gemini client, Supabase client, Google credentials, or retry logic.

### Module Structure

| File | Responsibility |
|------|---------------|
| `core/llm/client.py` | Creates and caches Gemini API clients. `get_gemini_clients()` returns a list of clients from up to 3 API keys (`GEMINI_API_KEY`, `GEMINI_API_KEY_2`, `GEMINI_API_KEY_3`). Multi-key failover: on `429`/`RESOURCE_EXHAUSTED`, `call_gemini()` tries the next key before falling back to Gemma. |
| `core/llm/constants.py` | All model names (`CLASSIFICATION_MODEL`, `SYNTHESIS_MODEL`, `EMBEDDING_MODEL`, `GEMMA_FALLBACK_MODEL`, `OPENROUTER_MODEL`), `EMBEDDING_DIMENSION`, `RETRYABLE_ERRORS`, `NON_RETRYABLE_ERRORS`, `SAFE_HOLD_CLASSIFICATION`, `Outcome`. |
| `core/llm/config.py` | `WorkloadProfile` profiles: INTERACTIVE (55s), SYNTHESIS (300s), BATCH (300s), EMBEDDING (120s). |
| `core/llm/providers.py` | `call_gemini()` and `call_openrouter()`. `call_gemini()` loops through all clients from `get_gemini_clients()` — on `429`/`quota` error, tries next key transparently before failing. Single `flash_lite_limiter` acquisition here. |
| `core/llm/fallback.py` | `generate_content_with_fallback()` — the master async fallback chain: Gemini → Gemma → OpenRouter. Rate limited at 14 RPM via shared `flash_lite_limiter`. |
| `core/llm/compat.py` | Sync wrappers for async code paths: `call_llm_with_fallback_sync()`, `get_embedding_sync()`, plus async wrappers `call_gemini_with_retry()` and `call_llm_with_fallback()`. Used by backfill scripts, standalone CLI tools. |
| `core/llm/embedding.py` | `get_embedding()` — async embedding with 500-entry LRU cache (md5-keyed), zero-vector fallback on failure, `output_dimensionality: EMBEDDING_DIMENSION`. |
| `core/llm/retry.py` | `get_jittered_backoff()` — shared exponential backoff with jitter for all retry logic. |
| `core/llm/errors.py` | Error types: `LLMError`, `ProviderTimeout`, `DeadlineExceeded`, `BreakerOpenError`, `ParseError`, `NonRetryableError`. |
| `core/llm/response.py` | Response types: `LLMResponse`, `EmbeddingResult`. |
| `core/llm/instrument.py` | Optional LLM call instrumentation (logs to `audit_logs`). |

### Safe Hold Behavior

When the classification LLM fails or falls back entirely (e.g., network error, rate limit), the system does not surface a `CLARIFICATION_NEEDED` intent that would require user disambiguation. Instead, `core/llm/constants.py` defines `SAFE_HOLD_CLASSIFICATION` which emits `{"intent": "NOTE", "confidence": 1.0}`. This means degraded classifications are silently vaulted as notes with embeddings — the content is persisted but never pollutes the task pipeline with false positives.

> **Full C3 fallback contract**: `core/FALLBACK_CONTRACTS.md` — C3 section. This is the authoritative source for test assertions.

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
| `gemini-3.1-flash-lite` | Intent classification, email classification | Gemini | Fast, cheap classification tasks |
| `gemini-embedding-2-preview` | Text embeddings (768-dim) | Gemini | All vector search operations |
| `gemma-4-31b-it` | LLM fallback | OpenRouter | Second-tier fallback for graph backfill |
| `nvidia/nemotron-3-super-120b-a12b:free` | LLM fallback (free tier) | OpenRouter | Last-resort fallback |

## Multi-Key Failover

To stay within Gemini free-tier daily quotas (500 RPD for Flash Lite, ~30 RPD for 3.5 Flash), the system supports up to **3 API keys**:

```python
GEMINI_API_KEY      # Primary key (required)
GEMINI_API_KEY_2    # Secondary key (optional — 2x quota)
GEMINI_API_KEY_3    # Tertiary key (optional — 3x quota)
```

`call_gemini()` in `core/llm/providers.py` loops through all available clients. On `429`/`RESOURCE_EXHAUSTED`/`quota` errors, it transparently retries with the next key. This means:
- **Flash Lite**: Up to ~1500 RPD (3 × 500)
- **3.5 Flash**: Up to ~100+ RPD
- **Gemma**: Up to ~600 RPD
- All benefits apply to backfill, pulse, webhook, and classification paths automatically

## The Triple Fallback Chain

Every LLM call goes through a structured retry chain with exponential backoff, defined in `core/llm/fallback.py`:

```
1. Gemini (primary) → 3 retries with jittered backoff
2. Gemma (OpenRouter) → 1 retry with jittered backoff
3. OpenRouter (free tier) → 1 retry with jittered backoff
```

### Retryable vs. Non-Retryable Errors

```python
# Defined in core/llm/constants.py
RETRYABLE_ERRORS = ['503', '504', '500', 'disconnected', 'timeout',
                    'deadline exceeded', 'unavailable', 'overloaded',
                    'rate limit']
NON_RETRYABLE_ERRORS = ['401', '403', '400', 'invalid']
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

A single sliding window rate limiter (`core/lib/rate_limiter.py`) protects the Gemini free tier across all instances:

```python
flash_lite_limiter = SlidingWindowLimiter(max_calls=14, per_seconds=60)
```

The limiter is acquired **exactly once** per call — in `core/llm/providers.py::call_gemini()`. There is no double-acquisition.

## Canonical Import Paths (DRY Rules)

**Every new file MUST use these imports. Never duplicate the underlying logic.**

| Need | Canonical Import |
|------|-----------------|
| Supabase client | `from core.services.db import get_supabase` |
| Gemini client | `from core.llm.client import get_gemini_client` (or `get_gemini_clients` for multi-key) |
| Google credentials | `from core.services.google_service import get_google_creds` |
| LLM call with fallback (async) | `from core.llm.fallback import generate_content_with_fallback` |
| LLM call with fallback (sync) | `from core.llm.compat import call_llm_with_fallback_sync` |
| Embedding (async) | `from core.llm import get_embedding` |
| Embedding (sync) | `from core.llm.compat import get_embedding_sync` |
| Model constants | `from core.llm.constants import CLASSIFICATION_MODEL, SYNTHESIS_MODEL, ...` |
| Error constants | `from core.llm.constants import RETRYABLE_ERRORS, NON_RETRYABLE_ERRORS` |
| Retry backoff | `from core.llm.retry import get_jittered_backoff` |
| Pending decision handler | `from core.webhook.utils import process_channel_pending_decision` |
| Audit logging | `from core.lib.audit_logger import log_audit, audit_log_sync, info, error` |
| Google service builder | `from core.services.google_service import get_service` |
| Time formatting | `from core.services.google_service import format_rfc3339` |

## Canonical Service Locations

| Service | File | Function |
|---------|------|----------|
| Supabase | `core/services/db.py` | `get_supabase()` — lazy singleton with `create_client()` |
| Gemini | `core/llm/client.py` | `get_gemini_clients()` — returns list from all configured keys |
| Google Auth | `core/services/google_service.py` | `get_google_creds()` — refresh token OAuth flow |
| Gmail | `core/services/google_service.py` | `get_service('gmail', 'v1')` |
| Google Calendar | `core/services/google_service.py` | `get_service('calendar', 'v3')` |
| Google Tasks | `core/services/google_service.py` | `get_tasks_service()` |
| Google Sheets | `core/services/google_service.py` | `get_service('sheets', 'v4')` |

## Model Registry

Every LLM call is logged to the `model_registry` table:

| Column | Example |
|--------|---------|
| `model_name` | gemini-3.1-flash-lite |
| `provider` | gemini |
| `input_tokens` | 1,234 |
| `output_tokens` | 567 |
| `latency_ms` | 2,345 |
| `success` | true |
| `error_message` | null |

This enables cost tracking across providers, performance comparison, latency monitoring, and failure rate analysis per model.

## Embedding Strategy

All embeddings use `gemini-embedding-2-preview` with `EMBEDDING_DIMENSION = 768` (defined in `core/llm/constants.py`). Embeddings are stored in:
- `memories.embedding` (VECTOR(768)) — primary semantic search
- `canonical_pages.embedding` (VECTOR(768)) — master page search
- `raw_dumps.embedding` (VECTOR(768)) — staging area search
- `resources.embedding` (VECTOR(768)) — resource similarity search

### Embedding Resilience

If embedding generation fails, the system returns an `EmbeddingResult(degraded=True)` which defaults to a zero vector `[0] * EMBEDDING_DIMENSION` (prevents crashes). Downstream systems see this and:
1. Set `embedding_status='failed'` on the memory
2. Queue the operation to `failed_queue` for retry
3. The Janitor workflow monitors failed embeddings and triggers retries

## Native Control Layer (Tool Calling & Prompt Mutation)

To guarantee deterministic outputs and prevent API rate-limit hammering, the system natively implements a control layer directly in `call_llm_with_fallback` and `run_agent_loop`:

1. **Tool Registry & Function Calling**: Instead of returning a monolithic JSON schema, the main pulse engine is equipped with a `ToolRegistry`. The LLM issues discrete function calls (e.g., `update_task_status`, `create_project`) which are validated against Pydantic models.
2. **Targeted Prompt Mutation**: Instead of blindly retrying, the engine appends dynamic correction hints to the prompt. If the model hallucinations a tool name or violates a constraint, the error is fed back dynamically.
3. **Jittered Backoff**: If validation or network calls fail, the retry loop applies `asyncio.sleep()` with an exponential backoff + random jitter (`_jitter()`). This prevents hammering the Gemini API and triggering 429 Rate Limits.
4. **Vercel-Friendly**: Built purely with native `async`/`await` primitives, ensuring zero event-loop blocking during the 60s serverless execution window.

## History

- **June 2026**: Consolidated all duplicated LLM infrastructure into `core/llm/`. Removed 17 redundant Supabase client creations, 3 Google credential factories, 2 fallback chains, 2 retry wrappers, and 6 model constant definitions. Added multi-key failover (3 API keys). Unified fallback chain with single rate limiter acquisition.
