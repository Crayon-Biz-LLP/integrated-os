# LLM Layer Consolidation (T-402)

**Date**: Jun 16, 2026 | **Commits**: ~10 across 45+ files

## Problem

The codebase had 11 duplicated infrastructure patterns — every module created its own Supabase client, Gemini client, Google credentials, and retry loop. This caused:
- Inconsistent error handling across callers
- 17 duplicate `create_client()` calls
- Double rate limiting (one in fallback chain, one in provider)
- Hardcoded model strings scattered everywhere
- 3 redundant pending decision handler files (~300 lines)

## What Changed

| Pattern | Before | After |
|---------|--------|-------|
| Supabase client | 17 `create_client()` calls | `get_supabase()` singleton |
| Gemini client | Per-module `genai.Client(...)` | `get_gemini_client()` + `get_gemini_clients()` (multi-key) |
| Google credentials | Inline OAuth in email, calls, drive | `get_google_creds()` factory |
| Fallback chain | `backfill_graph.py` own retry logic | `call_llm_with_fallback_sync()` |
| Rate limiting | Two `acquire()` calls per request | Single limiter in provider |
| Model strings | `"gemini-3.5-flash"` hardcoded | `SYNTHESIS_MODEL` constant |
| Decision handlers | 3 files (call/whatsapp/teams) | `process_channel_pending_decision()` |
| Embedding | Per-module | `get_embedding()` multi-key failover |

## Impact

- **Effective throughput doubled** (one rate limiter instead of two throttling each other)
- **3 API keys for Gemini** with transparent failover on 429/RESOURCE_EXHAUSTED
- **~500 lines deleted** (redundant handlers, retry logic, constants)
- **All imports centralized** in `core/llm/constants.py` and `core/services/db.py`

## Key Files

| File | Purpose |
|------|---------|
| `core/llm/client.py` | get_gemini_client(), get_gemini_clients() |
| `core/llm/constants.py` | Model names, retryable errors, embedding dim |
| `core/llm/compat.py` | Unified fallback chain, sync embed |
| `core/llm/embedding.py` | Multi-key failover embedding |
| `core/services/db.py` | get_supabase() singleton |
| `core/services/google_service.py` | get_google_creds() factory |
| `core/webhook/utils.py` | process_channel_pending_decision() |

## Related Docs

- [Graph KG Hardening](44-graph-kg-hardening.md) (same period)
- [LLM Architecture](15-llm-architecture.md)
