from decimal import Decimal

import pytest

from order_agent.db import AgentDB, DbRefusal, OrderSummary, UnsafeRole

PIZZA = "00000000-0000-0000-0000-000000000101"
COUNTER_ORDER = "00000000-0000-0000-0000-000000000201"
TOKEN = "test-conversation-token"


@pytest.fixture
def db(agent_url):
    with AgentDB.connect(agent_url, TOKEN) as conn:
        yield conn


def test_connect_refuses_superuser(admin_url, admin_conn):
    with pytest.raises(UnsafeRole):
        AgentDB.connect(admin_url, TOKEN)


def test_menu_lists_active_items_by_name(db):
    items = db.menu()
    names = [i.name for i in items]
    assert names == sorted(names)
    assert "Seasonal risotto" not in names
    pizza = next(i for i in items if i.id == PIZZA)
    assert pizza.price == Decimal("42.00")


def test_price_comes_from_catalog(db):
    order_id = db.create_order("Ana", [{"menu_item_id": PIZZA, "quantity": 2, "unit_price": 0.01}])
    assert db.order_summary(order_id) == OrderSummary("draft", Decimal("84.00"))


def test_off_menu_line_refused(db):
    with pytest.raises(DbRefusal) as exc:
        db.create_order("Ana", [{"description": "x", "unit_price": 0}])
    assert exc.value.sqlstate == "42501"


def test_connection_usable_after_refusal(db):
    with pytest.raises(DbRefusal):
        db.create_order("Ana", [{"description": "x", "unit_price": 0}])
    assert db.menu()


def test_cancel_refused(db):
    order_id = db.create_order("Ana", [{"menu_item_id": PIZZA}])
    with pytest.raises(DbRefusal) as exc:
        db.update_order_status(order_id, "cancelled")
    assert exc.value.sqlstate == "42501"


def test_counter_order_invisible(db):
    with pytest.raises(DbRefusal) as exc:
        db.order_summary(COUNTER_ORDER)
    assert exc.value.sqlstate == "P0002"


def test_unknown_order_invisible(db):
    with pytest.raises(DbRefusal) as exc:
        db.order_summary("00000000-0000-0000-0000-0000000009ff")
    assert exc.value.sqlstate == "P0002"


def test_bad_uuid_refused(db):
    with pytest.raises(DbRefusal) as exc:
        db.order_summary("not-a-uuid")
    assert exc.value.sqlstate == "22P02"


def test_confirm_own_draft(db):
    order_id = db.create_order("Ana", [{"menu_item_id": PIZZA}])
    db.update_order_status(order_id, "submitted")
    assert db.order_summary(order_id).status == "submitted"


def test_conversation_token_is_set_on_the_session(db):
    assert db._conn.execute("select current_setting('app.conversation')").fetchone()[0] == TOKEN
