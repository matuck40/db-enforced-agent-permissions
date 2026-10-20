"""The agent's tools over AgentDB.

The model proposes; the database decides. Tools never check ids, quantities or
permissions themselves: they pass the request on and report the database's answer.
Every call that reaches the database records exactly one Event.
"""

from __future__ import annotations

from typing import Annotated

from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.tools import BaseTool, InjectedToolCallId, tool
from langgraph.prebuilt import InjectedState
from langgraph.types import Command

from order_agent.db import AgentDB, DbRefusal
from order_agent.events import Event
from order_agent.state import merge_cart

# The label every chat order gets, in both configurations (tools and baseline).
# The harness sets it; the model never sees it as an argument.
CUSTOMER_LABEL = "Chat customer"


def _refusal_text(refusal: DbRefusal) -> str:
    # Only the SQLSTATE goes back to the model, never the server message.
    return f"refused by the database: {refusal.sqlstate}"


def _reply(call_id: str, text: str, **update) -> Command:
    return Command(update={"messages": [ToolMessage(text, tool_call_id=call_id)], **update})


def _last_ai_calls(messages: list) -> list[dict]:
    for message in reversed(messages):
        if isinstance(message, AIMessage):
            return list(message.tool_calls)
    return []


def make_tools(db: AgentDB) -> list[BaseTool]:
    def db_call(name: str, args: dict, call_id: str, fn):
        """Run fn against the database; reply with its text and one Event."""
        try:
            text, update = fn()
        except DbRefusal as refusal:
            event = Event(name, args, "database", "refused", refusal.sqlstate)
            return _reply(call_id, _refusal_text(refusal), events=[event])
        event = Event(name, args, "database", "ok", "")
        return _reply(call_id, text, events=[event], **update)

    @tool
    def get_menu(call_id: Annotated[str, InjectedToolCallId]) -> Command:
        """List the active menu items with their id, name and price."""

        def run():
            lines = [f"{i.id} | {i.name} | {i.price}" for i in db.menu()]
            return "\n".join(lines), {}

        return db_call("get_menu", {}, call_id, run)

    @tool
    def add_to_cart(
        menu_item_id: str,
        quantity: int,
        call_id: Annotated[str, InjectedToolCallId],
        cart: Annotated[dict[str, int], InjectedState("cart")],
    ) -> Command:
        """Put a menu item in the cart. Args: menu_item_id (menu item id), quantity (units).
        Sets the quantity for that item (it does not add to an earlier quantity)."""
        new = merge_cart(cart, {menu_item_id: quantity})
        return _reply(call_id, f"cart: {new}", cart={menu_item_id: quantity})

    @tool
    def remove_from_cart(
        menu_item_id: str,
        call_id: Annotated[str, InjectedToolCallId],
        cart: Annotated[dict[str, int], InjectedState("cart")],
    ) -> Command:
        """Remove a menu item from the cart. Args: menu_item_id (menu item id)."""
        new = merge_cart(cart, {menu_item_id: None})
        return _reply(call_id, f"cart: {new}", cart={menu_item_id: None})

    @tool
    def submit_order(
        call_id: Annotated[str, InjectedToolCallId],
        cart: Annotated[dict[str, int], InjectedState("cart")],
        order_ids: Annotated[list[str], InjectedState("order_ids")],
        messages: Annotated[list, InjectedState("messages")],
    ) -> Command:
        """Create the order from the cart, as a draft. It takes no arguments; the order
        label is set by the system, so do not ask the customer for a name.
        Call it alone in its own turn, after the cart is final.
        Returns the order id and the total computed by the database. The draft is not
        sent yet: once the customer confirms it, call
        update_order_status(order_id, "submitted")."""
        lines = [{"menu_item_id": k, "quantity": q} for k, q in cart.items()]
        args = {"customer_label": CUSTOMER_LABEL, "lines": lines}
        turn_calls = _last_ai_calls(messages)
        reason = None
        if not cart:
            reason = "cart is empty"
        elif order_ids:
            reason = "an order was already submitted"
        elif any(c["name"] != "submit_order" for c in turn_calls) or (
            sum(c["name"] == "submit_order" for c in turn_calls) > 1
        ):
            reason = "submit_order must be called alone in its own turn"
        if reason:
            event = Event("submit_order", args, "tool", "refused", reason)
            return _reply(call_id, f"refused: {reason}", events=[event])

        try:
            order_id = db.create_order(CUSTOMER_LABEL, lines)
        except DbRefusal as refusal:
            event = Event("submit_order", args, "database", "refused", refusal.sqlstate)
            return _reply(call_id, _refusal_text(refusal), events=[event])
        try:
            summary = db.order_summary(order_id)
        except DbRefusal as refusal:
            # The order exists even though reading it back was refused.
            event = Event("submit_order", args, "database", "refused", refusal.sqlstate)
            return _reply(call_id, _refusal_text(refusal), events=[event], order_ids=[order_id])
        event = Event("submit_order", args, "database", "ok", "")
        text = f"order {order_id} created, total {summary.total}"
        return _reply(call_id, text, events=[event], order_ids=[order_id])

    @tool
    def get_order_status(order_id: str, call_id: Annotated[str, InjectedToolCallId]) -> Command:
        """Get the status and total of an order. Args: order_id (order id)."""

        def run():
            s = db.order_summary(order_id)
            return f"order {order_id}: status {s.status}, total {s.total}", {}

        return db_call("get_order_status", {"order_id": order_id}, call_id, run)

    @tool
    def update_order_status(
        order_id: str, status: str, call_id: Annotated[str, InjectedToolCallId]
    ) -> Command:
        """Change the status of an order. Args: order_id (order id), status (new status)."""

        def run():
            db.update_order_status(order_id, status)
            return f"order {order_id} is now {status}", {}

        args = {"order_id": order_id, "status": status}
        return db_call("update_order_status", args, call_id, run)

    @tool
    def request_human(reason: str, call_id: Annotated[str, InjectedToolCallId]) -> Command:
        """Hand the conversation to a human. Args: reason (why a human is needed)."""
        return _reply(call_id, "a human will take over", handoff_reason=reason)

    return [
        get_menu,
        add_to_cart,
        remove_from_cart,
        submit_order,
        get_order_status,
        update_order_status,
        request_human,
    ]
