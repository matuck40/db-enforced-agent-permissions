"""Graph state shared by the model node, the tools and the validation node."""

from __future__ import annotations

import operator
from typing import Annotated

from langgraph.graph import MessagesState

from order_agent.events import Event


class OrderState(MessagesState):
    cart: dict[str, int]
    order_ids: list[str]  # orders created in this conversation
    events: Annotated[list[Event], operator.add]
    handoff_reason: str | None
