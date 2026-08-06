"""cost.py — per-model token pricing and cost estimation (M6 cost controls).

The M6 daily budget is enforced in money (USD), because that is what a
shared-account operator actually cares about: a runaway tenant should burn
a bounded number of dollars, not just a bounded number of calls. Tokens are
converted to USD via this pricing table.

Pricing is an ESTIMATE (per 1K tokens), tuned to the models this project
actually calls (core/llm/constants.py). The exact per-model rates drift over
time — this table is the single place to correct them. Unknown models fall
back to a conservative mid-range rate so cost is never silently undercounted.
"""

# model -> (input_usd_per_1k, output_usd_per_1k)
MODEL_PRICING_USD_PER_1K: dict[str, tuple[float, float]] = {
    # Gemini Flash Lite — classification / interactive (cheapest)
    "gemini-3.5-flash-lite": (0.0001, 0.0004),
    # Gemini Flash — synthesis/briefing workhorse
    "gemini-3.6-flash": (0.0003, 0.0015),
    # Gemma fallback via Gemini SDK
    "gemma-4-31b-it": (0.0002, 0.0008),
    # OpenRouter free tier — $0
    "nvidia/nemotron-3-super-120b-a12b:free": (0.0, 0.0),
    # Embedding
    "gemini-embedding-2-preview": (0.00002, 0.00002),
}

# Conservative fallback for unknown models: never silently undercount.
_FALLBACK_IN_PER_1K = 0.0005
_FALLBACK_OUT_PER_1K = 0.002


def model_price_per_1k(model: str) -> tuple[float, float]:
    """(input_usd_per_1k, output_usd_per_1k) for a model name."""
    if model in MODEL_PRICING_USD_PER_1K:
        return MODEL_PRICING_USD_PER_1K[model]
    return (_FALLBACK_IN_PER_1K, _FALLBACK_OUT_PER_1K)


def estimate_cost_usd(model: str, input_tokens: int, output_tokens: int) -> float:
    """Estimated USD cost of a call: (tokens/1000) * per-1k price."""
    in_p, out_p = model_price_per_1k(model)
    return round((input_tokens / 1000.0) * in_p + (output_tokens / 1000.0) * out_p, 6)
