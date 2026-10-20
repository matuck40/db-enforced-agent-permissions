"""Cost from token usage, the price table and the real-mode cap (no paid call anywhere)."""

from __future__ import annotations

import re

import pytest
from evals.cost import (
    PRICES_PATH,
    CostCapReached,
    PriceError,
    UsageHandler,
    cost_usd,
    load_prices,
    model_prices,
)
from langchain_core.messages import HumanMessage

from order_agent.fake import ScriptedChatModel

PRICES = {"models": {"m": {"input": 1.0, "cached_input": 0.5, "output": 2.0}}}


def test_cost_from_usage():
    usage = {"input_tokens": 1000, "cached_input_tokens": 400, "output_tokens": 500}
    # 600 fresh * 1.0 + 400 cached * 0.5 + 500 out * 2.0 = 1800 per million
    assert cost_usd(usage, "m", PRICES) == pytest.approx(0.0018)
    assert cost_usd({"input_tokens": 10, "output_tokens": 0}, "m", PRICES) == pytest.approx(1e-5)


def test_unknown_model_price_fails_fast():
    with pytest.raises(PriceError, match="no price for model 'other'"):
        model_prices("other", PRICES)
    with pytest.raises(PriceError):
        cost_usd({"input_tokens": 1}, "other", PRICES)


def test_empty_price_fails_fast():
    empty = {"models": {"m": {"input": "", "cached_input": 0.5, "output": 2.0}}}
    with pytest.raises(PriceError, match="'input'.*empty"):
        model_prices("m", empty)
    missing = {"models": {"m": {"input": 1.0, "output": 2.0}}}
    with pytest.raises(PriceError, match="'cached_input'"):
        model_prices("m", missing)


def test_shipped_prices_are_complete_and_say_where_they_came_from():
    # The numbers are copied by hand from the provider's page; the file must say when and where.
    text = PRICES_PATH.read_text(encoding="utf-8")
    assert re.search(r"^# Checked on \(YYYY-MM-DD\): \d{4}-\d{2}-\d{2}$", text, re.MULTILINE)
    assert re.search(r"^# Source URL: https://\S+", text, re.MULTILINE)
    prices = load_prices(PRICES_PATH)
    assert prices["models"]
    for name in prices["models"]:
        assert all(v > 0 for v in model_prices(name, prices).values()), name


def test_handler_sums_input_cached_and_output():
    usage = {
        "input_tokens": 100,
        "output_tokens": 20,
        "total_tokens": 120,
        "input_token_details": {"cache_read": 60},
    }
    model = ScriptedChatModel(script=[{"content": "a", "usage": usage}, {"content": "b"}])
    handler = UsageHandler()
    model.callbacks = [handler]
    model.invoke([HumanMessage("x")])
    model.invoke([HumanMessage("x")])
    assert handler.usage == {"input_tokens": 100, "cached_input_tokens": 60, "output_tokens": 20}


def test_handler_raises_once_over_the_cap():
    usage = {"input_tokens": 1_000_000, "output_tokens": 0, "total_tokens": 1_000_000}
    model = ScriptedChatModel(script=[{"content": "a", "usage": usage}] * 3)
    handler = UsageHandler("m", PRICES, max_usd=0.5)
    model.callbacks = [handler]
    with pytest.raises(CostCapReached):
        model.invoke([HumanMessage("x")])
    assert handler.total_usd == pytest.approx(1.0)


def test_handler_counts_calls_without_usage():
    model = ScriptedChatModel(
        script=[
            {"content": "a", "usage": {"input_tokens": 1, "output_tokens": 0, "total_tokens": 1}}
        ]
    )

    class NoUsage(ScriptedChatModel):
        def _generate(self, *args, **kwargs):
            result = super()._generate(*args, **kwargs)
            result.generations[0].message.usage_metadata = None
            return result

    silent = NoUsage(script=[{"content": "a"}, {"content": "b"}])
    handler = UsageHandler()
    silent.callbacks = [handler]
    silent.invoke([HumanMessage("x")])
    silent.invoke([HumanMessage("x")])
    assert (handler.calls, handler.calls_without_usage) == (0, 2)
    model.callbacks = [handler]
    model.invoke([HumanMessage("x")])
    assert (handler.calls, handler.calls_without_usage) == (1, 2)
