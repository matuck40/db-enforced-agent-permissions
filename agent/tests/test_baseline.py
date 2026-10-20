"""The strict-JSON baseline (no tools), driven by a scripted model against the test database."""

from decimal import Decimal

import pytest
from langchain_core.messages import HumanMessage, SystemMessage
from test_db import COUNTER_ORDER, PIZZA, TOKEN

from order_agent.baseline import (
    BASELINE_PROMPT,
    BASELINE_PROMPT_VERSION,
    DEFAULT_REPLY,
    RESPONSE_SCHEMA,
    BaselineAgent,
)
from order_agent.db import AgentDB, DbRefusal
from order_agent.fake import ScriptedChatModel

OFF_MENU = "00000000-0000-0000-0000-000000000111"


@pytest.fixture
def db(agent_url):
    with AgentDB.connect(agent_url, TOKEN) as conn:
        yield conn


def reply(action="continue", cart=None, text="ok") -> dict:
    return {"json": {"reply": text, "cart": cart or [], "action": action}}


def line(item=PIZZA, qty=2) -> dict:
    return {"menu_item_id": item, "quantity": qty}


def test_schema_is_strict_and_has_no_price_or_order_id():
    assert RESPONSE_SCHEMA["additionalProperties"] is False
    assert set(RESPONSE_SCHEMA["required"]) == {"reply", "cart", "action"}
    assert "price" not in str(RESPONSE_SCHEMA) and "order_id" not in str(RESPONSE_SCHEMA)
    assert len(BASELINE_PROMPT_VERSION) == 12


def test_baseline_submit_prices_from_catalog(db, admin_conn):
    model = ScriptedChatModel(script=[reply("submit", [line()], "Submitting.")])
    out = BaselineAgent(model, db).turn("two pizzas, yes")
    assert out.reply == "Submitting."
    assert len(out.order_ids) == 1
    row = admin_conn.execute(
        "select status, total from app.orders where id = %s", (out.order_ids[0],)
    ).fetchone()
    assert row == ("draft", Decimal("84.00"))
    assert [(e.tool, e.decided_by, e.outcome) for e in out.events] == [("submit", "database", "ok")]
    first = model.calls[0]
    assert isinstance(first[0], SystemMessage) and first[0].content == BASELINE_PROMPT
    assert PIZZA in first[1].content and "42.00" in first[1].content
    assert first[2].content == "No order yet."
    assert isinstance(first[3], HumanMessage) and first[3].content == "two pizzas, yes"


def test_baseline_confirm_submits_own_order(db, admin_conn):
    model = ScriptedChatModel(
        script=[reply("submit", [line(qty=1)]), reply("confirm", [line(qty=1)], "Done.")]
    )
    agent = BaselineAgent(model, db)
    agent.turn("one pizza")
    out = agent.turn("yes")
    assert [(e.tool, e.outcome) for e in out.events] == [("confirm", "ok")]
    status = admin_conn.execute(
        "select status from app.orders where id = %s", (out.order_ids[0],)
    ).fetchone()[0]
    assert status == "submitted"
    # history: customer turns and previous replies only, after system prompt and menu
    second = model.calls[1]
    assert [type(m).__name__ for m in second] == [
        "SystemMessage",
        "HumanMessage",
        "HumanMessage",
        "HumanMessage",
        "AIMessage",
        "HumanMessage",
    ]


def test_baseline_confirm_without_order_refused_by_tool(db):
    out = BaselineAgent(ScriptedChatModel(script=[reply("confirm")]), db).turn("yes")
    assert [(e.tool, e.decided_by, e.outcome) for e in out.events] == [
        ("confirm", "tool", "refused")
    ]
    assert out.order_ids == []


def test_baseline_second_submit_and_empty_cart_refused_without_db(db):
    model = ScriptedChatModel(
        script=[reply("submit"), reply("submit", [line(qty=1)]), reply("submit", [line(qty=1)])]
    )
    agent = BaselineAgent(model, db)
    empty = agent.turn("order")
    assert [(e.decided_by, e.outcome) for e in empty.events] == [("tool", "refused")]
    agent.turn("pizza")
    again = agent.turn("again")
    assert [(e.decided_by, e.outcome) for e in again.events] == [("tool", "refused")]
    assert len(again.order_ids) == 1


def test_baseline_off_menu_id_refused_by_database(db):
    model = ScriptedChatModel(script=[reply("submit", [line(OFF_MENU, 1)])])
    out = BaselineAgent(model, db).turn("the secret dish")
    assert out.order_ids == []
    (event,) = out.events
    assert (event.tool, event.decided_by, event.outcome) == ("submit", "database", "refused")
    assert event.detail != ""


@pytest.mark.parametrize(
    "bad",
    [
        {"reply": "x", "cart": [], "action": "refund"},
        {"reply": "x", "cart": [], "action": "continue", "order_id": COUNTER_ORDER},
        {"reply": "x", "cart": []},
        {"reply": 1, "cart": [], "action": "continue"},
        {"reply": "x", "cart": [{"menu_item_id": PIZZA, "quantity": True}], "action": "submit"},
        {
            "reply": "x",
            "cart": [{"menu_item_id": PIZZA, "quantity": 1, "price": 1}],
            "action": "submit",
        },
        {"reply": "x", "cart": [{"menu_item_id": PIZZA}], "action": "submit"},
        {"reply": "x", "cart": "pizza", "action": "submit"},
    ],
)
def test_baseline_invalid_json_refused(db, bad):
    out = BaselineAgent(ScriptedChatModel(script=[{"json": bad}]), db).turn("hi")
    assert out.reply == DEFAULT_REPLY
    assert out.order_ids == [] and out.handoff is False
    (event,) = out.events
    assert (event.tool, event.decided_by, event.outcome) == ("response", "validation", "refused")
    assert PIZZA not in event.detail and COUNTER_ORDER not in event.detail


def test_baseline_human_action_sets_handoff(db):
    out = BaselineAgent(
        ScriptedChatModel(script=[reply("human", text="A person will help.")]), db
    ).turn("I want a refund")
    assert out.handoff is True
    assert out.reply == "A person will help."
    assert out.events == []


def status_of(model, call: int) -> str:
    return model.calls[call][2].content


def test_baseline_status_first_turn_and_after_success(db):
    model = ScriptedChatModel(script=[reply(), reply("submit", [line()]), reply()])
    agent = BaselineAgent(model, db)
    agent.turn("hi")
    agent.turn("two pizzas")
    agent.turn("thanks")
    assert status_of(model, 0) == "No order yet."
    assert "order created as a draft, database total 84.00." in status_of(model, 2)
    assert "Current order: draft, database total 84.00." in status_of(model, 2)
    # status is not stored in history: only customer and reply messages follow it
    assert [type(m).__name__ for m in model.calls[2][3:]] == [
        "HumanMessage",
        "AIMessage",
        "HumanMessage",
        "AIMessage",
        "HumanMessage",
    ]
    assert all("Last action" not in m.content for m in model.calls[2][3:])


def test_baseline_status_after_refusals(db):
    model = ScriptedChatModel(
        script=[reply("submit", [line(OFF_MENU, 1)]), reply("submit"), reply("confirm"), reply()]
    )
    agent = BaselineAgent(model, db)
    agent.turn("a")
    agent.turn("b")
    agent.turn("c")
    agent.turn("d")
    assert "Last action: submit - refused by the database (" in status_of(model, 1)
    assert "cart is empty" in status_of(model, 2)
    assert "confirm - refused: there is no order yet." in status_of(model, 3)


class SummaryRefusingDB:
    """Stand-in whose order_summary is always refused after the order was created."""

    def __init__(self, real):
        self._real = real

    def menu(self):
        return self._real.menu()

    def create_order(self, label, lines):
        return self._real.create_order(label, lines)

    def order_summary(self, order_id):
        raise DbRefusal("42501", "denied")


def test_baseline_summary_refusal_keeps_order_id(db):
    model = ScriptedChatModel(script=[reply("submit", [line(qty=1)]), reply("submit", [line()])])
    agent = BaselineAgent(model, SummaryRefusingDB(db))
    first = agent.turn("one pizza")
    assert len(first.order_ids) == 1
    assert [(e.tool, e.decided_by, e.outcome, e.detail) for e in first.events] == [
        ("submit", "database", "ok", ""),
        ("order_summary", "database", "refused", "42501"),
    ]
    second = agent.turn("again")
    assert len(second.order_ids) == 1
    assert second.events[0].decided_by == "tool"
    assert "unreadable (refused by the database, 42501)" in status_of(model, 1)
