"""The agent's tools over AgentDB.

The model proposes; the database decides. Tools never check ids, quantities or
permissions themselves: they pass the request on and report the database's answer.
Every call that reaches the database records exactly one Event.
"""

from __future__ import annotations

from typing import Annotated

from langchain_core.messages import ToolMessage
from langchain_core.tools import BaseTool, InjectedToolCallId, tool
from langgraph.prebuilt import InjectedState
from langgraph.types import Command

from order_agent.db import AgentDB, DbRefusal
from order_agent.events import Event


def _refusal_text(refusal: DbRefusal) -> str:
    # Only the SQLSTATE goes back to the model, never the server message.
    return f"refused by the database: {refusal.sqlstate}"


def _reply(call_id: str, text: str, **update) -> Command:
    return Command(update={"messages": [ToolMessage(text, tool_call_id=call_id)], **update})


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
        """Put a menu item in the cart. Args: menu_item_id (menu item id), quantity (units)."""
        new = {**cart, menu_item_id: quantity}
        return _reply(call_id, f"cart: {new}", cart=new)

    @tool
    def remove_from_cart(
        menu_item_id: str,
        call_id: Annotated[str, InjectedToolCallId],
        cart: Annotated[dict[str, int], InjectedState("cart")],
    ) -> Command:
        """Remove a menu item from the cart. Args: menu_item_id (menu item id)."""
        new = {k: v for k, v in cart.items() if k != menu_item_id}
        return _reply(call_id, f"cart: {new}", cart=new)

    @tool
    def submit_order(
        customer_label: str,
        call_id: Annotated[str, InjectedToolCallId],
        cart: Annotated[dict[str, int], InjectedState("cart")],
        order_ids: Annotated[list[str], InjectedState("order_ids")],
    ) -> Command:
        """Create the order from the cart. Args: customer_label (name for the order).
        Returns the order id and the total computed by the database."""
        args = {"customer_label": customer_label}
        reason = "cart is empty" if not cart else "an order was already submitted"
        if not cart or order_ids:
            event = Event("submit_order", args, "tool", "refused", reason)
            return _reply(call_id, f"refused: {reason}", events=[event])

        def run():
            lines = [{"menu_item_id": k, "quantity": q} for k, q in cart.items()]
            order_id = db.create_order(customer_label, lines)
            summary = db.order_summary(order_id)
            text = f"order {order_id} created, total {summary.total}"
            return text, {"order_ids": [order_id], "cart": {}}

        return db_call("submit_order", args, call_id, run)

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
