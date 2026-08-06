"""budget.py — M6 per-tenant LLM credits (cost controls v2).

The product model (per your direction, 2026-08-06): each user is allocated a
MONTHLY credit (USD) that the OPERATOR owns — it lives on `users` as an
editable column (`users.monthly_credit_usd`), not in code. The cycle resets
on the user's signup day-of-month (anniversary billing: joined the 14th →
credit refreshes every 14th). Spend is read from the `llm_spend` ledger
(db/85) — one row per LLM outcome, written by instrument.py.

    - resolve_monthly_credit(uid)   users.monthly_credit_usd → env → default
    - cycle_start_utc(uid)          most recent signup-day boundary ≤ now
    - cycle_spend_usd(uid)          SUM(llm_spend) since cycle start
    - credit_remaining(uid)         max(0, credit − spent)
    - credit_warning(uid)           True when ≤ WARN_THRESHOLD of credit left
    - credit_exhausted(uid)         True when remaining ≤ 0 → hard block

Semantics:
  - No tenant context (legacy single-user / pre-db/78): no credit cap and
    the shared rate-limiter key — identical to pre-M6 behaviour.
  - Soft warn: the fallback entry gate logs a WARNING when the user is in
    the warning zone (≤20% of credit left) but keeps serving. At exactly 0
    it degrades to safe-hold until the next cycle — you can never spend
    past the credit you set.
  - Fail-open on ledger errors (documented): a dead ledger must not brick
    the product; the rate limiter still protects bursts. This is "heavily
    reduced risk", not absolute immunity (AGENTS.md standard).
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from calendar import monthrange

from core.services.db import get_tenant, get_supabase
from core.lib.audit_logger import audit_log_sync
from core.lib.rate_limiter import SlidingWindowLimiter
from core.llm.cost import estimate_cost_usd

# ── Credit defaults ─────────────────────────────────────────────────────

# Fallback when a user row has NO monthly_credit_usd set (NULL). The product
# form is the users table — this only covers unset/legacy rows.
DEFAULT_MONTHLY_CREDIT_USD = 5.0

# Soft-warn zone: warn (but keep serving) while this fraction of the credit
# remains; hard block at exactly 0.
WARN_THRESHOLD = 0.20

# Per-tenant LLM calls/min cap (Redis-backed sliding window).
DEFAULT_LLM_RPM = 20

# Classification keeps its own per-tenant cap (was 15/min GLOBAL before M6)
# so classification behaviour is unchanged — just tenant-scoped.
CLASSIFY_RPM = 15

# Legacy shared key used when no tenant context is active.
_LEGACY_LIMITER_KEY = "rhodey:rate_limit:llm:legacy"

# ── Small TTL cache for the users row (credit + cycle day). The row is
#    edited by an operator — near-immutable at runtime — so a 60s cache
#    avoids one DB read per LLM call. Spend is NEVER cached.
_user_row_cache: dict[str, tuple[float, dict | None]] = {}
_USER_ROW_TTL_S = 60.0


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


# ── Cycle math ──────────────────────────────────────────────────────────

def _parse_dt(value) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None


def _cycle_day_for(created_at: datetime | None, cycle_day: int | None) -> int:
    if cycle_day and 1 <= cycle_day <= 31:
        return cycle_day
    if created_at:
        return created_at.day
    return _now_utc().day


def cycle_start_utc(uid: str | None) -> datetime:
    """Most recent signup-day boundary ≤ now (UTC), clamped to month length.

    Anniversary billing: a user who joined on the 14th refreshes on the 14th
    of every month. Day 29-31 clamps to the shorter month's last day.
    """
    created_at, row = _user_row(uid)
    day = _cycle_day_for(_parse_dt(created_at), (row or {}).get("credit_cycle_day"))
    now = _now_utc()

    def _make(y: int, m: int, d: int) -> datetime:
        last = monthrange(y, m)[1]
        return datetime(y, m, min(d, last), tzinfo=timezone.utc)

    cand = _make(now.year, now.month, day)
    if cand <= now:
        return cand
    # Boundary fell later this month than we are → cycle started last month.
    prev = now.replace(day=1) - timedelta(days=1)
    return _make(prev.year, prev.month, day)


# ── User row access (cached) ────────────────────────────────────────────

def _user_row(uid: str | None) -> tuple[str | None, dict | None]:
    """(created_at, row) for a user, cached 60s. Fail-open → (None, None).

    Uses the RAW client with an explicit `.eq("id", uid)` — the uid IS the
    scope, so this is safe with a tenant context (runtime) or without one
    (admin endpoint). The tenant facade would fail CLOSED without a context
    and silently drop the admin credit overlay.
    """
    if not uid:
        return None, None
    now = _now_utc().timestamp()
    cached = _user_row_cache.get(uid)
    if cached and cached[0] > now:
        return cached[1].get("created_at"), cached[1]
    try:
        res = (
            get_supabase()
            .table("users")
            .select("monthly_credit_usd, credit_cycle_day, created_at")
            .eq("id", uid)
            .limit(1)
            .maybe_single()
            .execute()
        )
        row = res.data if isinstance(res.data, dict) else {}
        _user_row_cache[uid] = (now + _USER_ROW_TTL_S, row)
        return row.get("created_at"), row
    except Exception as e:
        audit_log_sync("llm_budget", "WARNING", f"users row lookup failed: {e}")
        return None, None


# ── Credit resolution ───────────────────────────────────────────────────

def resolve_monthly_credit(uid: str | None = None) -> float:
    """Per-user monthly credit: users.monthly_credit_usd → env → default.

    The credit is whatever the operator set in the users table (NULL = fall
    back). The env var only applies in legacy/no-uid mode so it can never
    clobber a per-user row.
    """
    if uid:
        _, row = _user_row(uid)
        try:
            val = (row or {}).get("monthly_credit_usd")
            if val is not None and float(val) > 0:
                return float(val)
        except Exception:
            pass
    try:
        env_credit = os.getenv("LLM_MONTHLY_CREDIT_USD")
        if env_credit:
            return float(env_credit)
    except Exception:
        pass
    return DEFAULT_MONTHLY_CREDIT_USD


# ── Ledger reads / writes ───────────────────────────────────────────────

def cycle_spend_usd(uid: str) -> float:
    """Sum est_cost_usd of this user's llm_spend rows since cycle start.

    Raw client with an explicit owner filter — same reasoning as _user_row:
    the uid IS the scope, safe with or without a tenant context.
    """
    start = cycle_start_utc(uid)
    res = (
        get_supabase()
        .table("llm_spend")
        .select("est_cost_usd")
        .eq("owner_id", uid)
        .gte("ts", start.isoformat())
        .execute()
    )
    total = 0.0
    for row in res.data or []:
        try:
            total += float(row.get("est_cost_usd") or 0.0)
        except Exception:
            pass
    return total


def credit_remaining(uid: str | None) -> float:
    """max(0, credit − spent this cycle). Legacy (no uid): unlimited."""
    if not uid:
        return float("inf")
    try:
        return max(0.0, resolve_monthly_credit(uid) - cycle_spend_usd(uid))
    except Exception as e:
        audit_log_sync("llm_budget", "WARNING", f"credit remaining failed, allowing: {e}")
        return float("inf")


def credit_warning(uid: str | None) -> bool:
    """True when the user is in the soft-warn zone (≤20% left, not at 0)."""
    if not uid:
        return False
    try:
        credit = resolve_monthly_credit(uid)
        remaining = credit_remaining(uid)
        return 0 < remaining <= WARN_THRESHOLD * credit
    except Exception:
        return False


def credit_exhausted(uid: str | None) -> bool:
    """True when the user's monthly credit is spent → hard block."""
    if not uid:
        return False
    try:
        return credit_remaining(uid) <= 0.0
    except Exception as e:
        audit_log_sync("llm_budget", "WARNING", f"credit check failed, allowing: {e}")
        return False


def record_llm_spend(
    uid: str | None,
    model: str,
    provider: str | None,
    workload: str | None,
    input_tokens: int,
    output_tokens: int,
    outcome: str,
) -> None:
    """Append one row to the llm_spend ledger (owner-scoped insert)."""
    if not uid:
        return  # legacy mode: no tenant, no per-user ledger
    try:
        cost = estimate_cost_usd(model, input_tokens, output_tokens)
        get_supabase().table("llm_spend").insert({
            "owner_id": uid,  # raw client: stamp explicitly (uid IS the scope)
            "model": model,
            "provider": provider,
            "workload": workload,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "est_cost_usd": cost,
            "outcome": outcome,
        }).execute()
    except Exception as e:
        audit_log_sync("llm_budget", "WARNING", f"llm_spend record failed: {e}")


# ── Per-tenant rate limiter ─────────────────────────────────────────────

_tenant_limiters: dict[str, SlidingWindowLimiter] = {}


def tenant_llm_limiter(uid: str | None = None, max_calls: int = DEFAULT_LLM_RPM) -> SlidingWindowLimiter:
    """A per-tenant sliding-window limiter (cached per uid + cap)."""
    cache_key = f"{uid or _LEGACY_LIMITER_KEY}:{max_calls}"
    if cache_key not in _tenant_limiters:
        _tenant_limiters[cache_key] = SlidingWindowLimiter(
            max_calls=max_calls,
            per_seconds=60,
            redis_key=f"rhodey:rate_limit:llm:{uid or _LEGACY_LIMITER_KEY}",
        )
    return _tenant_limiters[cache_key]


def clear_cache() -> None:
    """Drop cached limiters + user rows (tests / config changes)."""
    _tenant_limiters.clear()
    _user_row_cache.clear()


def current_tenant() -> str | None:
    """The active tenant id from the contextvar, if any (M1)."""
    return get_tenant()
