"""Tools are driven through a ToolNode, the way the graph will call them."""

from decimal import Decimal

import pytest
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.graph import START, StateGraph
from langgraph.prebuilt import ToolNode
from test_db import COUNTER_ORDER, PIZZA, TOKEN

from order_agent.db import AgentDB
from order_agent.events import Event
from order_agent.state import OrderState
from order_agent.tools import CUSTOMER_LABEL, make_tools


@pytest.fixture
def db(agent_url):
    with AgentDB.connect(agent_url, TOKEN) as conn:
        yield conn


@pytest.fixture
def node(db):
    """A one-node graph around ToolNode: the tools need a graph runtime to run."""
    graph = StateGraph(OrderState)
    graph.add_node("tools", ToolNode(make_tools(db)))
    graph.add_edge(START, "tools")
    return graph.compile()


def call_many(node, calls, **state):
    """Invoke several tool calls carried by one AIMessage; return the final state."""
    tool_calls = [{"name": n, "args": a, "id": f"call-{i}"} for i, (n, a) in enumerate(calls)]
    full = {
        "messages": [AIMessage(content="", tool_calls=tool_calls)],
        "cart": {},
        "order_ids": [],
        "events": [],
        "handoff_reason": None,
        **state,
    }
    return node.invoke(full)


def call(node, name, args, **state):
    """Invoke one tool call; return (tool message text, final state)."""
    ai = AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": "call-1"}])
    full = {
        "messages": [ai],
        "cart": {},
        "order_ids": [],
        "events": [],
        "handoff_reason": None,
        **state,
    }
    out = node.invoke(full)
    msg = out["messages"][-1]
    assert isinstance(msg, ToolMessage)
    assert msg.tool_call_id == "call-1"
    return msg.content, out


def order_count(admin_conn) -> int:
    return admin_conn.execute("select count(*) from app.orders").fetchone()[0]


def test_get_menu_records_database_event(node):
    text, update = call(node, "get_menu", {})
    assert "Margherita" in text and PIZZA in text
    assert update["events"] == [Event("get_menu", {}, "database", "ok", "")]


def test_cart_changes_state_only(node):
    _, update = call(node, "add_to_cart", {"menu_item_id": PIZZA, "quantity": 2})
    assert update["cart"] == {PIZZA: 2}
    assert update["events"] == []
    _, update = call(node, "remove_from_cart", {"menu_item_id": PIZZA}, cart={PIZZA: 2})
    assert update["cart"] == {}


def test_submit_uses_catalog_price(node):
    text, update = call(node, "submit_order", {}, cart={PIZZA: 2})
    assert len(update["order_ids"]) == 1
    assert update["order_ids"][0] in text
    assert Decimal("84.00") == Decimal(text.rsplit(" ", 1)[1])
    assert [e.decided_by for e in update["events"]] == ["database"]
    assert update["events"][0].outcome == "ok"
    assert update["cart"] == {PIZZA: 2}
    assert update["events"][0].args == {
        "customer_label": CUSTOMER_LABEL,
        "lines": [{"menu_item_id": PIZZA, "quantity": 2}],
    }


def test_submit_empty_cart_does_not_touch_db(node, admin_conn):
    before = order_count(admin_conn)
    _, update = call(node, "submit_order", {})
    assert order_count(admin_conn) == before
    assert update["events"][0].decided_by == "tool"
    assert update["events"][0].outcome == "refused"
    assert update["order_ids"] == []


def test_second_submit_refused(node, admin_conn):
    before = order_count(admin_conn)
    _, update = call(node, "submit_order", {}, cart={PIZZA: 1}, order_ids=["existing"])
    assert order_count(admin_conn) == before
    assert update["events"][0].decided_by == "tool"
    assert update["events"][0].outcome == "refused"


def test_bad_line_refused_by_database_without_id(node):
    text, update = call(node, "submit_order", {}, cart={PIZZA: 0})
    assert text.startswith("refused by the database: ")
    assert update["order_ids"] == []
    assert update["events"][0].decided_by == "database"
    assert update["events"][0].outcome == "refused"


def test_get_order_status_ok(node, db):
    order_id = db.create_order("Ana", [{"menu_item_id": PIZZA, "quantity": 1}])
    text, update = call(node, "get_order_status", {"order_id": order_id})
    assert "draft" in text and "42.00" in text
    assert update["events"] == [
        Event("get_order_status", {"order_id": order_id}, "database", "ok", "")
    ]


def test_cancel_is_refused_by_database(node, db):
    order_id = db.create_order("Ana", [{"menu_item_id": PIZZA, "quantity": 1}])
    text, update = call(node, "update_order_status", {"order_id": order_id, "status": "cancelled"})
    assert text == "refused by the database: 42501"
    assert update["events"] == [
        Event(
            "update_order_status",
            {"order_id": order_id, "status": "cancelled"},
            "database",
            "refused",
            "42501",
        )
    ]


def test_foreign_counter_order_is_refused_by_database(node):
    text, update = call(node, "get_order_status", {"order_id": COUNTER_ORDER})
    assert text == "refused by the database: P0002"
    assert update["events"][0].detail == "P0002"
    assert update["events"][0].args == {"order_id": COUNTER_ORDER}


def test_request_human_sets_reason(node):
    text, update = call(node, "request_human", {"reason": "customer is angry"})
    assert update["handoff_reason"] == "customer is angry"
    assert text


def test_model_sees_only_real_arguments(db):
    shown = {
        t.name: set(t.tool_call_schema.model_json_schema()["properties"]) for t in make_tools(db)
    }
    assert shown["submit_order"] == set()
    assert shown["add_to_cart"] == {"menu_item_id", "quantity"}
    assert shown["get_menu"] == set()


OTHER = "00000000-0000-0000-0000-000000000102"


def test_parallel_adds_of_different_items_both_land(node):
    out = call_many(
        node,
        [
            ("add_to_cart", {"menu_item_id": PIZZA, "quantity": 1}),
            ("add_to_cart", {"menu_item_id": OTHER, "quantity": 3}),
        ],
    )
    assert out["cart"] == {PIZZA: 1, OTHER: 3}


def test_parallel_adds_of_same_item_do_not_error(node):
    # add_to_cart sets the quantity, so with the same item twice one value wins.
    out = call_many(
        node,
        [
            ("add_to_cart", {"menu_item_id": PIZZA, "quantity": 1}),
            ("add_to_cart", {"menu_item_id": PIZZA, "quantity": 2}),
        ],
    )
    assert out["cart"][PIZZA] in (1, 2)


def test_add_and_submit_in_one_turn_refuses_submit(node, admin_conn):
    before = order_count(admin_conn)
    out = call_many(
        node,
        [
            ("add_to_cart", {"menu_item_id": PIZZA, "quantity": 1}),
            ("submit_order", {}),
        ],
        cart={PIZZA: 1},
    )
    assert order_count(admin_conn) == before
    assert out["order_ids"] == []
    (event,) = out["events"]
    assert (event.tool, event.decided_by, event.outcome) == ("submit_order", "tool", "refused")


def test_two_submits_in_one_turn_create_nothing(node, admin_conn):
    before = order_count(admin_conn)
    out = call_many(
        node,
        [("submit_order", {})] * 2,
        cart={PIZZA: 1},
    )
    assert order_count(admin_conn) == before
    assert out["order_ids"] == []
    assert [e.decided_by for e in out["events"]] == ["tool", "tool"]


def test_tool_descriptions_carry_the_order_mechanics():
    # tools_unguarded has no rules in its prompt, so how an order is sent must be in the
    # tool descriptions that both tool configurations share, not only in the guarded prompt.
    tools = {t.name: t for t in make_tools(None)}  # type: ignore[arg-type]
    text = tools["submit_order"].description
    assert "draft" in text
    assert 'update_order_status(order_id, "submitted")' in text
