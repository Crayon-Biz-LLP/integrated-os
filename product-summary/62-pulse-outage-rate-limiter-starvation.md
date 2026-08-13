# 62 — Pulse Outage: Rate-Limiter Self-Feeding Starvation

## Symptom

Pulse stopped delivering briefings even though the cron job (cron-job.org →
`/api/pulse-cron`) reported **success on every run**. Zero `main` pulse runs
completed in 48h. The sentinel kept running (heartbeats fresh), so the
"no tenant due" path was healthy — the briefings specifically were dying.

## Root Cause (4W1H)

**Root Cause:** The Aug-11 deploy (`894cb5c`) switched `/api/pulse-cron` from
sequential briefings to **fan-out per-tenant Modal workers** (up to 4 tenants
concurrently at slot time), all sharing one Redis-backed Gemini rate limiter
(`flash_3_5_limiter`, 4 rpm/key) with the sentinel (every 5 min × 4 tenants).
When the pool saturated, `redis_rate_limit_check` **added the caller to the
sorted set BEFORE checking the over-limit condition** — so every blocked poll
grew the count while refreshing the key TTL, and `acquire_async`'s unbounded
recursion re-polled forever. The blocked workers fed the very count that
blocked them: a self-sustaining starvation loop that hung each `brief_tenant`
worker inside `await limiter.acquire_async()` — **before** the LLM call, so
the LLM deadline/retry machinery never engaged — until Modal's 900s timeout
killed it. No briefing ever completed; cron still saw its 200.

**What:** (1) `redis_rate_limit_check` now uses count-then-add ordering —
prune, count, and only write a member when there is headroom; blocked callers
never touch the set. (2) `acquire_async` is now bounded by a `max_total_wait`
budget (120s default) and audits a WARNING before proceeding without a token —
a worker can never block indefinitely again. (3) The sentinel gets its own
limiter pool (`sentinel_flash_limiter`) so the two workloads fail
independently. (4) Bonus bug: `get_calendar_context()` was called without its
required `target_date`, throwing a TypeError on every pulse and silently
dropping calendar context — now passes the tenant's resolved `now`.

**Where:** `core/lib/redis_cache.py` (`redis_rate_limit_check`),
`core/lib/rate_limiter.py` (`acquire_async`, new `sentinel_flash_limiter`),
`core/pulse/sentinel.py` (limiter override), `core/pulse/briefing.py`
(`_wrap_calendar_context(now)`), `core/llm/providers.py` + `core/llm/fallback.py`
(limiter override threading).

**When:** Any slot-time burst where concurrent brief_tenant workers + sentinel
polls exceed the shared flash window (4 rpm × #keys). Pre-fix this was a
permanent hang; post-fix it's a bounded ≤120s wait, then the call proceeds and
the provider's own 429 handling absorbs the excess.

**How:** The invariant is now structural — a blocked caller can never write to
the set it's blocked by, and no caller can wait longer than the budget. The
self-feeding loop is impossible by construction. Regression tests pin both
behaviors (blocked polls must not grow the set; acquire must bail after the
budget) and the live-Redis reproduction confirms `zcard` stays flat under
blocked load (was climbing 4→12 pre-fix).

## Evidence Trail

- Audit logs: cron `main` runs at 12:30/13:30 UTC Aug 12 **hung and
  timeout-killed** after "Phase 2 context fetched"; 14 min of silence.
- Local UAT runs complete the same pipeline in ~70s — environment-specific.
- Live reproduction (throwaway key, max=4): 4 allowed → 8 blocked polls grew
  `zcard` 4→12 with wait never draining. Post-fix: `zcard` pinned at 4, wait
  decays and re-admits when the window slides.
- Bonus: `get_calendar_context() missing target_date` seen in the hung
  worker's audit trail — confirmed real in current code, now fixed.

## Verification

- 7 new tests in `tests/test_rate_limiter.py`: starvation regression
  (blocked polls don't grow the set + re-admission after window slide),
  under-limit admission, bounded acquire (bails after budget, admits when
  token frees), sentinel pool isolation, limiter-override threading (positive
  and negative).
- Full suite: **623 passed, 0 failed** (120 live-DB skips as always), ruff clean.
- M9.2 / M9.3 / M9.4 gates GREEN.
- Live-Redis repro re-run post-fix: `zcard` stays flat under blocked load.

## Honest Caveats

- The read-then-write in `redis_rate_limit_check` is **not atomic** — a burst
  can over-admit by a couple of calls. Acceptable: the LLM provider's 429
  handling (multi-key failover in `call_gemini`) absorbs the excess. The
  invariant that matters — blocked callers never grow the set — is what the
  tests pin.
- The live-DB suites (UAT, sim) were not re-run this pass; the fix touches
  the shared limiter that every LLM call passes through, so one `LIVE_DB=true`
  run before deploy is recommended.
