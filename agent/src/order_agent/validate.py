"""Rule-based validation of the model's tool calls (no database, no LLM).

This is a defence layer in front of the database. Every refusal is recorded as an
Event decided by 'validation', so the eval can tell which layer stopped an attack.
Reasons are short and never echo the raw argument values back to the model.
"""

from __future__ import annotations

from langchain_core.messages import ToolCall, ToolMessage

from order_agent.events import Event
from order_agent.state import OrderState

# Model-visible arguments of each tool (name -> type). A test keeps this equal to
# the schemas of make_tools(), so drift fails.
TOOL_ARGS: dict[str, dict[str, type]] = {
    "get_menu": {},
    "add_to_cart": {"menu_item_id": str, "quantity": int},
    "remove_from_cart": {"menu_item_id": str},
    "submit_order": {},
    "get_order_status": {"order_id": str},
    "update_order_status": {"order_id": str, "status": str},
    "request_human": {"reason": str},
}

MIN_QUANTITY, MAX_QUANTITY = 1, 20
ALLOWED_STATUS = "submitted"


def _check(call: ToolCall, order_ids: list[str], menu_ids: set[str]) -> str | None:
    """Return the reason to refuse the call, or None when it is acceptable."""
    spec = TOOL_ARGS.get(call["name"])
    if spec is None:
        return "unknown tool"
    args = call["args"]
    if not isinstance(args, dict):
        return "arguments must be an object"
    for name in spec:
        if name not in args:
            return f"missing argument: {name}"
    for name in args:
        if name not in spec:
            return f"unexpected argument: {name}"
    for name, expected in spec.items():
        value = args[name]
        # bool is a subclass of int; it is not a quantity.
        if type(value) is not expected:
            return f"argument {name} must be {expected.__name__}"

    if "quantity" in args and not MIN_QUANTITY <= args["quantity"] <= MAX_QUANTITY:
        return f"quantity must be between {MIN_QUANTITY} and {MAX_QUANTITY}"
    if "menu_item_id" in args and args["menu_item_id"] not in menu_ids:
        return "menu_item_id is not on the menu"
    if "order_id" in args and args["order_id"] not in order_ids:
        return "order_id was not created in this conversation"
    if "status" in args and args["status"] != ALLOWED_STATUS:
        return f"status must be {ALLOWED_STATUS}"
    return None


def validate_tool_calls(
    calls: list[ToolCall], state: OrderState, menu_ids: set[str]
) -> tuple[list[ToolCall], list[ToolMessage], list[Event]]:
    """Split calls into accepted ones (unchanged, in order) and refusals.

    Each refused call yields one ToolMessage and one Event.
    """
    order_ids = state.get("order_ids") or []
    accepted: list[ToolCall] = []
    messages: list[ToolMessage] = []
    events: list[Event] = []
    for call in calls:
        reason = _check(call, order_ids, menu_ids)
        if reason is None:
            accepted.append(call)
            continue
        messages.append(ToolMessage(f"refused by validation: {reason}", tool_call_id=call["id"]))
        events.append(Event(call["name"], call["args"], "validation", "refused", reason))
    return accepted, messages, events
