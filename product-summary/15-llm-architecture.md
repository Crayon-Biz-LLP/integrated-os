# 15. LLM Architecture & Model Registry

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

### Rate Limiting

A sliding window rate limiter (`core/lib/rate_limiter.py`) protects the Gemini free tier (15 RPM limit):

```python
flash_lite_limiter = SlidingWindowLimiter(max_calls=12, per_seconds=60)
```

The limiter operates at 12 RPM (leaving 3 RPM headroom) and supports both sync and async contexts:
- `acquire()` — uses `time.sleep()` for synchronous code
- `acquire_async()` — uses `await asyncio.sleep()` for async code

Applied in `classify.py` before every Gemini flash-lite call:
```python
if "flash-lite" in model:
    await flash_lite_limiter.acquire_async()
```

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

This enables:
- Cost tracking across providers
- Performance comparison (Gemini vs. Gemma vs. OpenRouter)
- Latency monitoring for briefing generation
- Failure rate analysis per model

## Embedding Strategy

All embeddings use `gemini-embedding-2-preview` with 768 dimensions. Embeddings are stored in:
- `memories.embedding` (VECTOR(768)) — primary semantic search
- `canonical_pages.embedding` (VECTOR(768)) — master page search
- `raw_dumps.embedding` (VECTOR(768)) — staging area search
- `resources.embedding` (VECTOR(768)) — resource similarity search

The `match_memories` RPC function uses cosine distance (`<=>` operator) for vector similarity search:
```sql
ORDER BY cp.embedding <=> query_embedding
LIMIT match_count
```

### Embedding Resilience

If embedding generation fails, the system:
1. Sets `embedding_status='failed'` on the memory
2. Queues the operation to `failed_queue` for retry
3. Returns a zero vector `[0] * 768` as fallback (prevents crashes)
4. The Janitor workflow monitors failed embeddings and triggers retries

## Stringent Prompt Engineering

The system prompts are aggressively constrained to prevent hallucination:
- Classification prompt: 14 explicit rules about what NOT to do
- Briefing prompt: 30+ constraints, including data fidelity (STRICTLY FORBIDDEN from listing non-existent tasks)
- Email classification: NOREPLY patterns pre-filter to save API calls
- Multimodal processing: "PROHIBIT ACTION HALLUCINATION" — never say "I'll ping", "I'll check"

## Native Control Layer (Validation & Prompt Mutation)

To guarantee deterministic JSON outputs and prevent API rate-limit hammering, the system natively implements a control layer directly in `call_llm_with_fallback`:

1. **Strict Pydantic Validation**: LLM outputs are validated against Pydantic schemas (e.g., `PulseOutput`). If the schema fails, the response is rejected.
2. **Targeted Prompt Mutation**: Instead of blindly retrying, the engine appends a dynamic correction hint to the prompt (e.g., `System Correction: Your previous response failed validation: <error>. Please correct this.`).
3. **Jittered Backoff**: If validation fails, the retry loop applies `asyncio.sleep()` with an exponential backoff + random jitter (`_jitter()`). This prevents hammering the Gemini API and triggering 429 Rate Limits.
4. **Vercel-Friendly**: Built purely with native `async`/`await` primitives, ensuring zero event-loop blocking during the 60s serverless execution window.
