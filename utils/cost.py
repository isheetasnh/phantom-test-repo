"""
Cost calculation for Claude token usage.

Applies the gateway margin on top of raw Anthropic prices so that
the amounts shown in the dashboard match what's actually deducted
from user credits.
"""

from utils.pricing import get_pricing

# LiteLLM gateway applies a flat margin to all LLM models.
# customer_price = base_cost * (1 + GATEWAY_MARGIN)
GATEWAY_MARGIN = 1.0


def compute_cost(
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_write_5m_tokens: int,
    cache_write_1h_tokens: int,
    cache_read_tokens: int,
) -> float:
    """Return the customer-facing total cost in USD after applying the gateway margin."""
    return sum(
        compute_cost_breakdown(
            model,
            input_tokens,
            output_tokens,
            cache_write_5m_tokens,
            cache_write_1h_tokens,
            cache_read_tokens,
        ).values()
    )


def compute_cost_breakdown(
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_write_5m_tokens: int,
    cache_write_1h_tokens: int,
    cache_read_tokens: int,
) -> dict:
    """Return per-category customer-facing costs in USD after applying the gateway margin."""
    pricing = get_pricing(model)
    m = 1 + GATEWAY_MARGIN
    return {
        "input": (input_tokens / 1_000_000) * pricing["input"] * m,
        "output": (output_tokens / 1_000_000) * pricing["output"] * m,
        "cache_write_5m": (cache_write_5m_tokens / 1_000_000)
        * pricing["cache_write_5m"]
        * m,
        "cache_write_1h": (cache_write_1h_tokens / 1_000_000)
        * pricing["cache_write_1h"]
        * m,
        "cache_read": (cache_read_tokens / 1_000_000) * pricing["cache_read"] * m,
    }
