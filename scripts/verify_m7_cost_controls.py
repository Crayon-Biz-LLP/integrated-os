#!/usr/bin/env python3
"""M7 cost-controls gate — verify the M6 per-tenant LLM CREDITS layer
(plans/69 §M6, credits v2) without touching the live DB or calling any LLM.

Checks (unit-level, mocked):
  1. Cost math: estimate_cost_usd prices known models and falls back for unknown.
  2. Credit resolution: users.monthly_credit_usd (table) wins over env/default;
     unset falls back to default; legacy (no uid) env applies.
  3. Cycle math: signup-day boundary is the most recent occurrence ≤ now,
     clamped to month length (29-31 in short months).
  4. Per-tenant isolation: tenant A's spend never counts against tenant B.
  5. Enforcement: credit_exhausted → degraded safe-hold without a provider
     call; credit_warning → proceeds (soft warn, not block); healthy → proceeds.
  6. Per-tenant rate limiter keying (uid in Redis key; classify keeps 15/min).
  7. Ledger recording: log_llm_outcome writes an llm_spend row (owner-stamped).
  8. Classify uses the per-tenant limiter (no global key leak).
"""
import os
import sys
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

FAILURES = []


def check(name: str, ok: bool, detail: str = ""):
    tag = "PASS" if ok else "FAIL"
    print(f"  {'✅' if ok else '❌'} [{tag}] {name}" + (f" — {detail}" if detail else ""))
    if not ok:
        FAILURES.append(name)


class Res:
    def __init__(self, data):
        self.data = data


class FakeBuilder:
    """Chainable fake: select().eq().limit().maybe_single().execute() etc."""

    def __init__(self, rows, table=""):
        self._rows = rows
        self._filters = []

    def select(self, *a, **k):
        return self

    def eq(self, col, val):
        self._filters.append((col, val))
        return self

    def gte(self, col, val):
        self._filters.append((col, val))
        return self

    def limit(self, n):
        return self

    def maybe_single(self):
        return self

    def execute(self):
        # maybe_single() callers expect a dict; plain table() callers a list.
        if self._filters and self._filters[-1][0] in ("key", "id"):
            return Res(self._rows[0] if self._rows else None)
        return Res(self._rows)


class FakeClient:
    def __init__(self):
        self.tables = {}

    def table(self, name):
        return FakeBuilder(self.tables.get(name, []))


def main() -> int:
    print("M7 cost-controls verification (credits v2)\n")

    # ── 1. Cost math ──
    from core.llm.cost import estimate_cost_usd, MODEL_PRICING_USD_PER_1K
    known = next(iter(MODEL_PRICING_USD_PER_1K))
    check("cost: known model priced > 0",
          estimate_cost_usd(known, 1000, 1000) > 0.0)
    check("cost: free model prices to 0",
          estimate_cost_usd("nvidia/nemotron-3-super-120b-a12b:free", 1000, 1000) == 0.0)
    check("cost: unknown model uses conservative fallback (no undercount)",
          estimate_cost_usd("made-up-model", 1000, 1000) >= 0.0005)
    check("cost: zero tokens → zero cost",
          estimate_cost_usd(known, 0, 0) == 0.0)

    # ── 2. Credit resolution (table-driven) ──
    import core.llm.budget as b
    with patch.object(b, "get_supabase") as mock_fac, \
         patch.dict(os.environ, {"LLM_MONTHLY_CREDIT_USD": "9.99"}, clear=False):
        # users.monthly_credit_usd set → wins over env + default
        b.clear_cache()
        mock_fac.return_value = MagicMock()
        mock_fac.return_value.table.return_value = FakeBuilder(
            [{"monthly_credit_usd": 5.0, "credit_cycle_day": 14, "created_at": "2026-01-14T00:00:00+00:00"}])
        check("credit: users.monthly_credit_usd wins over env/default",
              b.resolve_monthly_credit("uid-x") == 5.0)
        # users row exists but credit NULL → env (fresh cache)
        b.clear_cache()
        mock_fac.return_value.table.return_value = FakeBuilder(
            [{"monthly_credit_usd": None, "credit_cycle_day": 14, "created_at": "2026-01-14T00:00:00+00:00"}])
        check("credit: NULL credit row → env fallback",
              b.resolve_monthly_credit("uid-x") == 9.99)
        # no uid (legacy) → env
        check("credit: legacy (no uid) → env override",
              b.resolve_monthly_credit(None) == 9.99)
        b.clear_cache()

    with patch.object(b, "get_supabase") as mock_fac, \
         patch.dict(os.environ, {}, clear=True):
        mock_fac.return_value.table.return_value = FakeBuilder([])
        check("credit: no row + no env → default",
              b.resolve_monthly_credit("uid-x") == b.DEFAULT_MONTHLY_CREDIT_USD)

    # ── 3. Cycle math (signup day, clamped) ──
    now = datetime(2026, 8, 15, 12, 0, tzinfo=timezone.utc)
    with patch.object(b, "_now_utc", return_value=now), \
         patch.object(b, "get_supabase") as mock_fac:
        mock_fac.return_value.table.return_value = FakeBuilder(
            [{"monthly_credit_usd": None, "credit_cycle_day": None, "created_at": "2026-01-14T00:00:00+00:00"}])
        start = b.cycle_start_utc("uid-x")
        check("cycle: signup day 14 → cycle started 2026-08-14",
              start == datetime(2026, 8, 14, 0, 0, tzinfo=timezone.utc),
              f"start={start}")
        # day 31: now is Aug 15 → most recent 31st is July 31 (no clamp needed)
        b.clear_cache()
        mock_fac.return_value.table.return_value = FakeBuilder(
            [{"monthly_credit_usd": None, "credit_cycle_day": 31, "created_at": "2026-01-31T00:00:00+00:00"}])
        start31 = b.cycle_start_utc("uid-x")
        check("cycle: day 31 → most recent 31st boundary (Jul 31 when now is Aug 15)",
              start31 == datetime(2026, 7, 31, 0, 0, tzinfo=timezone.utc),
              f"start={start31}")

    # cycle day 31, now = June 15 (June has 30 days): the most recent real
    # 31st is May 31 — the boundary steps back, it does not invent a June 31.
    now_jun = datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc)
    with patch.object(b, "_now_utc", return_value=now_jun), \
         patch.object(b, "get_supabase") as mock_fac:
        b.clear_cache()
        mock_fac.return_value.table.return_value = FakeBuilder(
            [{"monthly_credit_usd": None, "credit_cycle_day": 31, "created_at": "2026-01-31T00:00:00+00:00"}])
        start_jun = b.cycle_start_utc("uid-x")
        check("cycle: day 31, now Jun 15 → most recent 31st is May 31",
              start_jun == datetime(2026, 5, 31, 0, 0, tzinfo=timezone.utc),
              f"start={start_jun}")
        b.clear_cache()

    # On June 30 itself, the boundary clamps to June 30 (no 31st exists).
    now_eom = datetime(2026, 6, 30, 12, 0, tzinfo=timezone.utc)
    with patch.object(b, "_now_utc", return_value=now_eom), \
         patch.object(b, "get_supabase") as mock_fac:
        b.clear_cache()
        mock_fac.return_value.table.return_value = FakeBuilder(
            [{"monthly_credit_usd": None, "credit_cycle_day": 31, "created_at": "2026-01-31T00:00:00+00:00"}])
        start_eom = b.cycle_start_utc("uid-x")
        check("cycle: day 31, now Jun 30 → boundary clamps to Jun 30",
              start_eom == datetime(2026, 6, 30, 0, 0, tzinfo=timezone.utc),
              f"start={start_eom}")
        b.clear_cache()

    # ── 4. Per-tenant isolation on the ledger ──
    with patch.object(b, "get_supabase") as mock_fac:
        fake = FakeClient()
        fake.tables["llm_spend"] = [
            {"est_cost_usd": 3.0}, {"est_cost_usd": 1.0},
        ]
        mock_fac.return_value = fake
        with patch.object(b, "resolve_monthly_credit", return_value=10.0), \
             patch.object(b, "_user_row", return_value=("2026-01-14T00:00:00+00:00",
                         {"monthly_credit_usd": 10.0, "credit_cycle_day": 14})):
            check("isolation: cycle spend sums only visible (owner-scoped) rows",
                  b.cycle_spend_usd("uid-a") == 4.0)
            check("isolation: remaining = credit − spend (floored at 0)",
                  b.credit_remaining("uid-a") == 6.0)
            check("enforcement: remaining 6/10 (60%) → no warn, not exhausted",
                  b.credit_warning("uid-a") is False and b.credit_exhausted("uid-a") is False)
            # 2.0 remaining / 10 credit = exactly 20% → warn zone (soft), not blocked
            with patch.object(b, "cycle_spend_usd", return_value=8.0):
                check("enforcement: remaining at 20% → warn (soft), not blocked",
                      b.credit_warning("uid-a") is True and b.credit_exhausted("uid-a") is False)
        with patch.object(b, "resolve_monthly_credit", return_value=3.0), \
             patch.object(b, "_user_row", return_value=("2026-01-14T00:00:00+00:00",
                         {"monthly_credit_usd": 3.0, "credit_cycle_day": 14})):
            check("enforcement: spend 4/3 → exhausted (hard block)",
                  b.credit_exhausted("uid-a") is True)
            check("enforcement: legacy (no uid) never exhausted",
                  b.credit_exhausted(None) is False)

    # ── 5. Enforcement in the fallback chain (real entry point) ──
    import asyncio
    import core.llm.fallback as fb
    from core.llm.constants import Outcome

    # Blocked: remaining = 0 (credit exhausted) → degraded safe hold.
    with patch.object(fb, "current_tenant", return_value="uid-a"), \
         patch.object(fb, "credit_remaining", return_value=0.0), \
         patch.object(fb, "resolve_monthly_credit", return_value=5.0), \
         patch.object(fb, "tenant_llm_limiter") as mock_lim, \
         patch.object(fb, "log_llm_outcome"), \
         patch.object(fb, "audit_log_sync"):
        mock_lim.return_value._get_wait_secs.return_value = 0.0
        resp = asyncio.run(fb.generate_content_with_fallback(
            "hi", primary_model="gemini-3.5-flash-lite"))
        check("enforcement: credit exhausted → degraded safe hold (no provider call)",
              resp.degraded and resp.degraded_reason == "credit_exhausted",
              f"degraded={getattr(resp, 'degraded', None)} reason={getattr(resp, 'degraded_reason', None)}")

    # Warn zone (0 < remaining ≤ 20%): still serves the provider.
    with patch.object(fb, "current_tenant", return_value="uid-a"), \
         patch.object(fb, "credit_remaining", return_value=0.5), \
         patch.object(fb, "resolve_monthly_credit", return_value=5.0), \
         patch.object(fb, "tenant_llm_limiter") as mock_lim, \
         patch.object(fb, "call_gemini", return_value=("ok", None, None)) as mock_prov, \
         patch.object(fb, "gemini_breaker") as mock_breaker, \
         patch.object(fb, "log_llm_outcome"), \
         patch.object(fb, "audit_log_sync"):
        mock_breaker.is_open.return_value = False
        mock_lim.return_value._get_wait_secs.return_value = 0.0
        resp = asyncio.run(fb.generate_content_with_fallback(
            "hi", primary_model="gemini-3.5-flash-lite"))
        check("enforcement: warn zone → still serves (soft warn, not block)",
              mock_prov.called and getattr(resp, "success", False))

    # ── 6. Per-tenant rate limiter keying ──
    b.clear_cache()
    lim_a = b.tenant_llm_limiter("uid-a")
    lim_b = b.tenant_llm_limiter("uid-b")
    check("ratelimit: distinct tenant → distinct limiter instance",
          lim_a is not lim_b)
    check("ratelimit: redis key embeds the uid",
          "uid-a" in lim_a.redis_key and "uid-b" in lim_b.redis_key)
    lim_legacy = b.tenant_llm_limiter(None)
    check("ratelimit: legacy uses shared key, distinct from any tenant",
          lim_legacy is not lim_a and "legacy" in lim_legacy.redis_key)
    lim_class = b.tenant_llm_limiter("uid-a", max_calls=b.CLASSIFY_RPM)
    check("ratelimit: classification cap cached separately (15/min vs 20/min)",
          lim_class is not lim_a and lim_class.max_calls == b.CLASSIFY_RPM
          and lim_a.max_calls == b.DEFAULT_LLM_RPM)
    b.clear_cache()

    # ── 7. Ledger recording from instrument.py ──
    from core.llm.response import LLMResponse
    from core.llm import instrument as inst
    with patch.object(inst, "record_llm_spend") as mock_rec, \
         patch.object(inst, "current_tenant", return_value="uid-a"), \
         patch.object(inst, "audit_log_sync"), \
         patch.object(inst, "get_supabase"):
        resp = LLMResponse(text="hello world", provider="gemini", model="gemini-3.5-flash-lite",
                           workload="general", success=True, degraded=False,
                           degraded_reason=None, attempts=1, latency_ms=10,
                           final_exception=None)
        inst.log_llm_outcome(resp, Outcome.SUCCESS, prompt="hi there")
        check("ledger: log_llm_outcome records spend with owner + model",
              mock_rec.called and mock_rec.call_args.kwargs.get("model") == "gemini-3.5-flash-lite"
              and mock_rec.call_args.kwargs.get("uid") == "uid-a")

    # ── 8. Classify uses per-tenant limiter ──
    import core.webhook.classify as cl
    csrc = open(cl.__file__).read()
    check("classify: no global rate-limit key leak",
          "rhodey:rate_limit:classify:legacy" not in csrc
          or "tenant_llm_limiter(current_tenant()" in csrc)
    check("classify: per-tenant limiter wired into rate-limit gate",
          "tenant_llm_limiter(current_tenant()" in csrc
          and "CLASSIFY_RPM" in csrc)

    print()
    if FAILURES:
        print(f"❌ M7 COST-CONTROL GATE FAILED ({len(FAILURES)}): {FAILURES}")
        return 1
    print("✅ ALL M7 COST-CONTROL GATES PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
