"""List prices for the Claude models the gallery benchmarks, so an analysis can report its cost.

Prices change. Update `PRICES` together with each entry's `as_of` date when you re-check the page.
"""

from dataclasses import dataclass
from datetime import date

PRICING_URL = "https://platform.claude.com/docs/en/about-claude/pricing"

_TOKENS_PER_MILLION = 1_000_000


@dataclass(frozen=True, slots=True)
class ModelPrice:
    """What one model charges per million tokens, and where and when that was read."""

    input_usd_per_mtok: float
    output_usd_per_mtok: float
    source_url: str
    as_of: date


_AS_OF = date(2026, 10, 20)

# Standard-tier list prices, with no cache discount: the analyzer doesn't use prompt caching.
# Haiku 5.5 charges $0.50 / $2.50 for prompts over 100K tokens, which an image resized to 1024 px
# can't reach. The Sonnet 5, Opus 5 and Opus 4.8 rows exist because they're the models a
# server-side refusal fallback may answer with, and `usage` should price the model that answered.
PRICES: dict[str, ModelPrice] = {
    "claude-haiku-5-5": ModelPrice(0.10, 0.50, PRICING_URL, _AS_OF),
    "claude-sonnet-5-5": ModelPrice(2.00, 10.00, PRICING_URL, _AS_OF),
    "claude-opus-5-5": ModelPrice(4.00, 20.00, PRICING_URL, _AS_OF),
    "claude-sonnet-5": ModelPrice(2.00, 10.00, PRICING_URL, _AS_OF),
    "claude-opus-5": ModelPrice(5.00, 25.00, PRICING_URL, _AS_OF),
    "claude-opus-4-8": ModelPrice(5.00, 25.00, PRICING_URL, _AS_OF),
}


def cost_usd(model: str, input_tokens: int, output_tokens: int) -> float | None:
    """Cost of one request in US dollars, or None when `model` has no entry in `PRICES`."""
    price = PRICES.get(model)
    if price is None:
        return None
    return (
        input_tokens * price.input_usd_per_mtok + output_tokens * price.output_usd_per_mtok
    ) / _TOKENS_PER_MILLION
