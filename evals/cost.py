"""Token usage, price table and the cost cap for the real-model eval mode (Ruling R17)."""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.outputs import LLMResult

PRICES_PATH = Path(__file__).resolve().parent / "prices.toml"
PRICE_FIELDS = ("input", "cached_input", "output")


class PriceError(ValueError):
    """The price table has no usable entry for the model."""


class CostCapReached(Exception):
    """The running cost went over ``EVAL_MAX_USD``; the current conversation stops here."""


def load_prices(path: Path = PRICES_PATH) -> dict[str, Any]:
    with path.open("rb") as handle:
        return tomllib.load(handle)


def model_prices(model: str, prices: dict[str, Any]) -> dict[str, float]:
    """The three prices (USD per million tokens) of ``model``, or a clear error.

    Called before any model call, so a missing or empty price never costs money.
    """
    entry = prices.get("models", {}).get(model)
    if not isinstance(entry, dict):
        raise PriceError(f"no price for model {model!r} in prices.toml; fill it in first")
    out: dict[str, float] = {}
    for name in PRICE_FIELDS:
        value = entry.get(name)
        if isinstance(value, bool) or not isinstance(value, int | float) or value < 0:
            raise PriceError(
                f"price {name!r} for model {model!r} in prices.toml is empty or not a number"
            )
        out[name] = float(value)
    return out


def cost_usd(usage: dict[str, int], model: str, prices: dict[str, Any]) -> float:
    """Cost of ``usage`` in USD.

    ``usage`` has ``input_tokens`` (all input, cached ones included, as LangChain reports
    it), ``cached_input_tokens`` (the cached part, default 0) and ``output_tokens``.
    """
    p = model_prices(model, prices)
    cached = usage.get("cached_input_tokens", 0)
    fresh = max(usage.get("input_tokens", 0) - cached, 0)
    return (
        fresh * p["input"]
        + cached * p["cached_input"]
        + usage.get("output_tokens", 0) * p["output"]
    ) / 1_000_000


class UsageHandler(BaseCallbackHandler):
    """Sums the usage of every chat-model result and enforces the cost cap.

    Attach it to the model itself (``model.callbacks = [handler]``): that covers the graph
    and the baseline, including the structured-output call. ``raise_error`` makes the
    ``CostCapReached`` raised after the call that crossed the cap reach the runner.
    """

    raise_error = True

    def __init__(
        self,
        model: str = "",
        prices: dict[str, Any] | None = None,
        max_usd: float | None = None,
    ):
        self.model = model
        self.prices = prices
        self.max_usd = max_usd
        self.usage = {"input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0}
        self.calls = 0

    @property
    def total_usd(self) -> float:
        if self.prices is None:
            return 0.0
        return cost_usd(self.usage, self.model, self.prices)

    @property
    def over_cap(self) -> bool:
        return self.max_usd is not None and self.total_usd > self.max_usd

    def on_llm_end(self, response: LLMResult, **kwargs: Any) -> None:
        for generations in response.generations:
            for generation in generations:
                meta = getattr(getattr(generation, "message", None), "usage_metadata", None)
                if not meta:
                    continue
                self.calls += 1
                self.usage["input_tokens"] += meta.get("input_tokens", 0)
                self.usage["output_tokens"] += meta.get("output_tokens", 0)
                details = meta.get("input_token_details") or {}
                self.usage["cached_input_tokens"] += details.get("cache_read", 0)
        if self.over_cap:
            raise CostCapReached(f"cost cap of {self.max_usd} USD reached ({self.total_usd:.4f})")
