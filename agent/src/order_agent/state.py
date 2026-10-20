"""Graph state shared by the model node, the tools and the validation node."""

from __future__ import annotations

import operator
from typing import Annotated

from langgraph.graph import MessagesState

from order_agent.events import Event


def merge_cart(old: dict[str, int] | None, delta: dict[str, int | None]) -> dict[str, int]:
    """Apply a cart delta: a key is set to its value, or removed when the value is None.

    Tools return deltas so parallel calls in one step do not overwrite each other.
    """
    merged = dict(old or {})
    for key, value in delta.items():
        if value is None:
            merged.pop(key, None)
        else:
            merged[key] = value
    return merged


class OrderState(MessagesState):
    cart: Annotated[dict[str, int], merge_cart]
    order_ids: Annotated[list[str], operator.add]  # orders created in this conversation
    events: Annotated[list[Event], operator.add]
    handoff_reason: str | None
