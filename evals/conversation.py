"""Eval conversation format and loader.

A conversation is a list of customer turns plus the outcome the customer legitimately
wants, so the eval can score the order from the database. The same conversation runs
in three configurations (tools, tools_unguarded, baseline). Online, the model answers
for real. Offline (CI) the model is a scripted fake, so every conversation carries one
script per configuration that emulates a plausible model, including models that TRY the
attack, so that validation and the database are exercised. Offline numbers therefore
measure the harness and the database, not the model.

Script-consumption protocol (every script follows it exactly)
--------------------------------------------------------------
``tools`` and ``tools_unguarded``: a flat list of model outputs
``{"content": str, "tool_calls": [{"name": str, "args": dict}]}`` (``tool_calls``
optional), consumed in order across the whole conversation.

* For each customer turn the model is called. If the output has tool calls, they are
  processed and the model is called again. The turn ends at the first output WITHOUT
  tool calls.
* In ``tools``, a call refused by validation is answered with an error message and the
  model is called again (one more entry).
* An accepted ``request_human`` ends the conversation right after the tool round: no
  more entries for that turn or later turns.
* ``submit_order`` must be alone in its AIMessage, otherwise it is refused at tool level.
* The 7 tools and their model-visible args: ``get_menu()``,
  ``add_to_cart(menu_item_id: str, quantity: int)`` (sets the quantity),
  ``remove_from_cart(menu_item_id: str)``, ``submit_order(customer_label: str)``,
  ``get_order_status(order_id: str)``, ``update_order_status(order_id: str, status: str)``,
  ``request_human(reason: str)``. ``submit_order`` creates a DRAFT; the order is
  confirmed with ``update_order_status("$ORDER_ID", "submitted")`` once the customer
  confirms.
* ``$ORDER_ID`` in any argument is replaced, offline only, by the last order id this
  conversation created (the runner does it, never the agent).
* In ``tools``, validation refuses: unknown tools, wrong arg names or types (quantity
  must be an int), quantity outside 1..20, a menu_item_id not in the active menu, an
  order_id not created in this conversation, a status other than "submitted". In
  ``tools_unguarded`` none of that is checked and the call reaches the database, which
  refuses (quantity 500 or an inactive item at submit: 22023; a non-UUID id: 22P02;
  cancel: 42501; counter order: P0002), EXCEPT that on the schema as it is now, reading
  or confirming the foreign CHAT draft ...0202 by id succeeds. That is the leak later
  tasks measure and then fix.

``baseline``: exactly one ``{"json": {"reply": str, "cart": [{"menu_item_id": str,
"quantity": int}], "action": "continue"|"submit"|"confirm"|"human"}}`` per customer
turn, until ``human`` (which ends the conversation). ``cart`` is the FULL current cart
on every turn. The baseline has no field to address another order or to cancel, so its
attack scripts simply reply, or try what it can (an off-menu id, an odd quantity).

Prices in replies: scripts only quote catalog prices, line subtotals, or the order
total the database would compute (format ``\\d+\\.\\d{2}``). Where a scripted model
quotes an invented price (a "discounted" total) the ``notes`` field says "invented
price", so the invented-price metric is exercised.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Category = Literal["order", "handoff", "attack"]
Attack = Literal[
    "discount",
    "price_change",
    "cancel_other",
    "read_other",
    "confirm_other",
    "off_menu",
    "injection",
]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Expected(_Strict):
    """What the customer legitimately wants, whatever the configuration."""

    items: dict[str, int]  # menu item id -> quantity of the final order; {} if none
    total: str | None  # e.g. "84.00"
    handoff: bool


class Scripts(_Strict):
    tools: list[dict[str, Any]]
    tools_unguarded: list[dict[str, Any]]
    baseline: list[dict[str, Any]]


class Conversation(_Strict):
    id: str
    category: Category
    attack: Attack | None = None
    turns: list[str] = Field(min_length=1)
    expected: Expected
    scripts: Scripts
    notes: str = ""

    @model_validator(mode="after")
    def _attack_matches_category(self) -> Conversation:
        if (self.category == "attack") != (self.attack is not None):
            raise ValueError("attack is set if and only if category is 'attack'")
        return self


def load_conversations(directory: Path) -> list[Conversation]:
    """Load every ``*.json`` in the directory, sorted by file name.

    The conversation id must equal the file name (without extension).
    """
    found: list[Conversation] = []
    for path in sorted(directory.glob("*.json")):
        conversation = Conversation.model_validate_json(path.read_text(encoding="utf-8"))
        if conversation.id != path.stem:
            raise ValueError(f"{path.name}: id {conversation.id!r} does not match the file name")
        found.append(conversation)
    return found
