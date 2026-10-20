"""Validation is pure: no database, no model."""

import pytest

from order_agent.events import Event
from order_agent.tools import make_tools
from order_agent.validate import TOOL_ARGS, validate_tool_calls

MENU = {"m-pizza", "m-soda"}


def call(name, args, id_="c1"):
    return {"name": name, "args": args, "id": id_, "type": "tool_call"}


def state(order_ids=()):
    return {"messages": [], "cart": {}, "order_ids": list(order_ids), "events": []}


def run(calls, order_ids=()):
    return validate_tool_calls(calls, state(order_ids), MENU)


def assert_refused(calls, order_ids=()):
    ok, msgs, events = run(calls, order_ids)
    assert ok == []
    assert len(msgs) == 1 and len(events) == 1
    reason = msgs[0].content
    assert reason.startswith("refused by validation: ")
    assert msgs[0].tool_call_id == calls[0]["id"]
    ev = events[0]
    assert ev == Event(
        calls[0]["name"],
        calls[0]["args"],
        "validation",
        "refused",
        reason.removeprefix("refused by validation: "),
    )
    return reason


def test_unknown_tool_refused():
    assert_refused([call("drop_tables", {})])


def test_quantity_string_refused():
    assert_refused([call("add_to_cart", {"menu_item_id": "m-pizza", "quantity": "2"})])


@pytest.mark.parametrize("q", [0, 21, -1])
def test_quantity_out_of_range_refused(q):
    assert_refused([call("add_to_cart", {"menu_item_id": "m-pizza", "quantity": q})])


@pytest.mark.parametrize("q", [True, 1.0])
def test_quantity_bool_or_float_refused(q):
    assert_refused([call("add_to_cart", {"menu_item_id": "m-pizza", "quantity": q})])


@pytest.mark.parametrize("name", ["add_to_cart", "remove_from_cart"])
def test_off_menu_id_refused(name):
    args = {"menu_item_id": "secret-item"}
    if name == "add_to_cart":
        args["quantity"] = 1
    reason = assert_refused([call(name, args)])
    assert "secret-item" not in reason


@pytest.mark.parametrize("name", ["get_order_status", "update_order_status"])
def test_order_id_from_other_conversation_refused(name):
    args = {"order_id": "other"}
    if name == "update_order_status":
        args["status"] = "submitted"
    assert_refused([call(name, args)], order_ids=["mine"])


def test_cancel_status_refused():
    assert_refused(
        [call("update_order_status", {"order_id": "mine", "status": "cancelled"})],
        order_ids=["mine"],
    )


def test_missing_and_extra_args_refused():
    assert_refused([call("add_to_cart", {"menu_item_id": "m-pizza"})])
    assert_refused([call("get_menu", {"x": 1})])
    assert_refused([call("request_human", {"reason": 5})])


def test_valid_calls_pass_through_unchanged():
    calls = [
        call("get_menu", {}, "a"),
        call("add_to_cart", {"menu_item_id": "m-pizza", "quantity": 20}, "b"),
        call("remove_from_cart", {"menu_item_id": "m-soda"}, "c"),
        call("submit_order", {}, "d"),
        call("get_order_status", {"order_id": "mine"}, "e"),
        call("update_order_status", {"order_id": "mine", "status": "submitted"}, "f"),
        call("request_human", {"reason": "help"}, "g"),
    ]
    ok, msgs, events = run(calls, order_ids=["mine"])
    assert ok == calls and msgs == [] and events == []


def test_mixed_calls_keep_order_and_split():
    good1 = call("get_menu", {}, "a")
    bad = call("nope", {}, "b")
    good2 = call("request_human", {"reason": "x"}, "c")
    ok, msgs, events = run([good1, bad, good2])
    assert ok == [good1, good2]
    assert [m.tool_call_id for m in msgs] == ["b"]
    assert len(events) == 1


def test_spec_matches_model_visible_tool_args():
    visible = {
        t.name: {k: v.annotation for k, v in t.tool_call_schema.model_fields.items()}
        for t in make_tools(None)
    }
    assert TOOL_ARGS == visible


@pytest.mark.parametrize("args", [{"customer_label": "Ana"}, {"x": 1}])
def test_submit_order_takes_no_arguments(args):
    reason = assert_refused([call("submit_order", args)])
    assert reason == f"refused by validation: unexpected argument: {next(iter(args))}"
