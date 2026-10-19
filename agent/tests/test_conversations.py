"""Static checks of the eval conversations (no database, no model)."""

from __future__ import annotations

import re
from collections import Counter
from decimal import Decimal
from pathlib import Path

import pytest
from evals.conversation import Conversation, load_conversations

CONVERSATIONS_DIR = Path(__file__).resolve().parents[2] / "evals" / "conversations"
FIXTURE_MENU = Path(__file__).resolve().parents[2] / "evals" / "fixtures" / "menu.sql"

PREFIX = "00000000-0000-0000-0000-000000000"
MENU: dict[str, Decimal] = {
    PREFIX + "101": Decimal("42.00"),
    PREFIX + "102": Decimal("8.50"),
    PREFIX + "103": Decimal("28.00"),
    PREFIX + "104": Decimal("36.00"),
    PREFIX + "105": Decimal("18.00"),
    PREFIX + "106": Decimal("39.00"),
    PREFIX + "107": Decimal("7.00"),
    PREFIX + "108": Decimal("5.50"),
    PREFIX + "109": Decimal("16.00"),
    PREFIX + "110": Decimal("6.00"),
    PREFIX + "111": Decimal("44.00"),
    PREFIX + "112": Decimal("12.00"),
}
ACTIVE = {k for k in MENU if k != PREFIX + "111"}

TOOL_ARGS: dict[str, dict[str, type]] = {
    "get_menu": {},
    "add_to_cart": {"menu_item_id": str, "quantity": int},
    "remove_from_cart": {"menu_item_id": str},
    "submit_order": {"customer_label": str},
    "get_order_status": {"order_id": str},
    "update_order_status": {"order_id": str, "status": str},
    "request_human": {"reason": str},
}
ACTIONS = {"continue", "submit", "confirm", "human"}
PRICE = re.compile(r"\d+\.\d{2}")

EXPECTED_DISTRIBUTION = {
    ("order", None): 18,
    ("handoff", None): 6,
    ("attack", "discount"): 3,
    ("attack", "price_change"): 2,
    ("attack", "cancel_other"): 2,
    ("attack", "read_other"): 3,
    ("attack", "confirm_other"): 1,
    ("attack", "off_menu"): 3,
    ("attack", "injection"): 2,
}


@pytest.fixture(scope="module")
def conversations() -> list[Conversation]:
    return load_conversations(CONVERSATIONS_DIR)


def _calls(output: dict) -> list[dict]:
    return output.get("tool_calls") or []


def test_menu_constants_match_fixture():
    sql = FIXTURE_MENU.read_text(encoding="utf-8")
    for item_id, price in MENU.items():
        assert re.search(rf"'{item_id}',.*?{price}", sql), item_id


def test_all_conversations_load(conversations):
    assert conversations
    ids = [c.id for c in conversations]
    assert len(ids) == len(set(ids))
    for path in CONVERSATIONS_DIR.glob("*.json"):
        assert path.stem in ids
    for c in conversations:
        if c.category == "attack":
            assert c.attack is not None, c.id
        else:
            assert c.attack is None, c.id
        used = set(c.expected.items)
        for output in c.scripts.tools + c.scripts.tools_unguarded:
            for call in _calls(output):
                if "menu_item_id" in call["args"]:
                    used.add(call["args"]["menu_item_id"])
        for entry in c.scripts.baseline:
            used.update(line["menu_item_id"] for line in entry["json"]["cart"])
        unknown = used - set(MENU)
        if unknown:
            assert c.category == "attack" and "invented id" in c.notes, (c.id, unknown)


@pytest.mark.xfail(reason="only the first 6 conversations exist yet", strict=True)
def test_forty_conversations(conversations):
    assert len(conversations) == 40


@pytest.mark.xfail(reason="only the first 6 conversations exist yet", strict=True)
def test_distribution(conversations):
    counts = Counter((c.category, c.attack) for c in conversations)
    assert dict(counts) == EXPECTED_DISTRIBUTION


def test_scripts_use_known_tools_and_args(conversations):
    for c in conversations:
        for config in ("tools", "tools_unguarded"):
            for output in getattr(c.scripts, config):
                assert set(output) <= {"content", "tool_calls"}, (c.id, config)
                assert isinstance(output.get("content", ""), str)
                for call in _calls(output):
                    assert set(call) == {"name", "args"}, (c.id, config)
                    assert call["name"] in TOOL_ARGS, (c.id, config, call["name"])
                    if config == "tools" and c.category != "attack":
                        spec = TOOL_ARGS[call["name"]]
                        assert set(call["args"]) == set(spec), (c.id, call)
                        for name, kind in spec.items():
                            assert type(call["args"][name]) is kind, (c.id, call)
        for entry in c.scripts.baseline:
            assert set(entry) == {"json"}, c.id
            body = entry["json"]
            assert set(body) == {"reply", "cart", "action"}, c.id
            assert isinstance(body["reply"], str)
            assert body["action"] in ACTIONS, c.id
            for line in body["cart"]:
                assert set(line) == {"menu_item_id", "quantity"}, c.id
                assert isinstance(line["menu_item_id"], str)
                assert type(line["quantity"]) is int


def test_baseline_scripts_one_entry_per_turn(conversations):
    for c in conversations:
        script = c.scripts.baseline
        if len(script) == len(c.turns):
            continue
        assert script[-1]["json"]["action"] == "human" and len(script) < len(c.turns), c.id
        assert all(e["json"]["action"] != "human" for e in script[:-1]), c.id


def _walk_tools(c: Conversation, script: list[dict]) -> dict:
    """Replay the consumption protocol and check the script is consumed exactly."""
    position = 0
    cart: dict[str, int] = {}
    cart_totals: set[Decimal] = set()
    ended = False
    for turn in range(len(c.turns)):
        if ended:
            break
        while True:
            assert position < len(script), (c.id, "script exhausted in turn", turn)
            output = script[position]
            position += 1
            calls = _calls(output)
            if not calls:
                break
            names = [call["name"] for call in calls]
            if "submit_order" in names:
                assert names == ["submit_order"], (c.id, "submit_order must be alone")
            if "request_human" in names:
                assert names == ["request_human"], (c.id, "request_human must be alone")
                ended = True
                break
            for call in calls:
                args = call["args"]
                if call["name"] == "add_to_cart":
                    cart[args["menu_item_id"]] = args["quantity"]
                elif call["name"] == "remove_from_cart":
                    cart.pop(args["menu_item_id"], None)
            if _plausible(cart):
                cart_totals.add(_cart_total(cart))
                cart_totals.update(MENU[k] * q for k, q in cart.items())
    assert position == len(script), (c.id, "unused script entries", len(script) - position)
    return {"cart": cart, "cart_totals": cart_totals, "ended": ended}


def _plausible(cart: dict[str, int]) -> bool:
    """A cart whose total the database could compute (an invented price is not one)."""
    return bool(cart) and all(k in MENU and 1 <= q <= 20 for k, q in cart.items())


def _cart_total(cart: dict[str, int]) -> Decimal:
    return sum((MENU.get(k, Decimal(0)) * q for k, q in cart.items()), Decimal(0))


def test_tools_scripts_follow_the_protocol(conversations):
    for c in conversations:
        _walk_tools(c, c.scripts.tools)
        _walk_tools(c, c.scripts.tools_unguarded)


def test_handoff_scripts_end_the_conversation(conversations):
    for c in conversations:
        if c.expected.handoff:
            assert _walk_tools(c, c.scripts.tools)["ended"], c.id
            assert c.scripts.baseline[-1]["json"]["action"] == "human", c.id
        else:
            assert not _walk_tools(c, c.scripts.tools)["ended"], c.id
            assert all(e["json"]["action"] != "human" for e in c.scripts.baseline), c.id


def test_expected_total_matches_the_menu(conversations):
    for c in conversations:
        if c.expected.items:
            assert set(c.expected.items) <= ACTIVE, c.id
            assert c.expected.total == f"{_cart_total(c.expected.items):.2f}", c.id
        else:
            assert c.expected.total is None, c.id


def test_legitimate_orders_end_with_the_expected_cart(conversations):
    for c in conversations:
        if c.category != "order":
            continue
        assert _walk_tools(c, c.scripts.tools)["cart"] == c.expected.items, c.id
        assert c.scripts.baseline[-1]["json"]["action"] == "confirm", c.id


def _prices(texts: list[str]) -> set[Decimal]:
    return {Decimal(m) for t in texts for m in PRICE.findall(t)}


def _allowed_prices(c: Conversation, cart_totals: set[Decimal]) -> set[Decimal]:
    allowed = set(MENU.values()) | cart_totals
    if c.expected.total:
        allowed.add(Decimal(c.expected.total))
    return allowed


def test_quoted_prices_are_real_unless_flagged(conversations):
    flagged = 0
    for c in conversations:
        totals = {_cart_total(c.expected.items)} | {
            MENU[k] * q for k, q in c.expected.items.items()
        }
        invented = False
        for config, script in (
            ("tools", c.scripts.tools),
            ("tools_unguarded", c.scripts.tools_unguarded),
        ):
            walked = _walk_tools(c, script)
            texts = [o.get("content", "") for o in script]
            if _prices(texts) - _allowed_prices(c, totals | walked["cart_totals"]):
                invented = True
        baseline_totals = set(totals)
        for entry in c.scripts.baseline:
            cart = {line["menu_item_id"]: line["quantity"] for line in entry["json"]["cart"]}
            if _plausible(cart):
                baseline_totals.add(_cart_total(cart))
                baseline_totals.update(MENU[k] * q for k, q in cart.items())
        texts = [e["json"]["reply"] for e in c.scripts.baseline]
        if _prices(texts) - _allowed_prices(c, baseline_totals):
            invented = True
        if invented:
            assert "invented price" in c.notes, c.id
            flagged += 1
        elif "invented price" in c.notes:
            raise AssertionError(f"{c.id}: notes claim an invented price but none is quoted")
    assert flagged >= 2
