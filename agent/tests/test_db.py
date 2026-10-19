from decimal import Decimal

import pytest

from order_agent.db import AgentDB, DbRefusal, OrderSummary, UnsafeRole

PIZZA = "00000000-0000-0000-0000-000000000101"
COUNTER_ORDER = "00000000-0000-0000-0000-000000000201"
FOREIGN_DRAFT = "00000000-0000-0000-0000-000000000202"
# 43 characters, like secrets.token_urlsafe(32); the database requires at least 32.
TOKEN = "test-conversation-token-0123456789abcdefghi"
OTHER_TOKEN = "other-conversation-token-0123456789abcdefg"


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


def test_foreign_chat_order_invisible_after_fix(db):
    # The chat draft ...0202 has no conversation hash, so no conversation can reach it.
    with pytest.raises(DbRefusal) as exc:
        db.order_summary(FOREIGN_DRAFT)
    assert exc.value.sqlstate == "P0002"
    with pytest.raises(DbRefusal) as exc:
        db.update_order_status(FOREIGN_DRAFT, "submitted")
    assert exc.value.sqlstate == "P0002"


def test_token_is_not_left_on_the_connection(db):
    order_id = db.create_order("Ana", [{"menu_item_id": PIZZA}])
    db.order_summary(order_id)
    with pytest.raises(DbRefusal):
        db.order_summary(COUNTER_ORDER)
    # A fresh transaction on the same connection, without the per-transaction set_config.
    value = db._conn.execute("select current_setting('app.conversation', true)").fetchone()[0]
    db._conn.rollback()
    assert value in (None, "")


def test_conversations_cannot_read_each_other(agent_url):
    with AgentDB.connect(agent_url, TOKEN) as one, AgentDB.connect(agent_url, OTHER_TOKEN) as two:
        mine = one.create_order("Ana", [{"menu_item_id": PIZZA}])
        theirs = two.create_order("Bea", [{"menu_item_id": PIZZA}])
        for db, order_id in ((one, theirs), (two, mine)):
            with pytest.raises(DbRefusal) as exc:
                db.order_summary(order_id)
            assert exc.value.sqlstate == "P0002"
            with pytest.raises(DbRefusal) as exc:
                db.update_order_status(order_id, "submitted")
            assert exc.value.sqlstate == "P0002"
        assert one.order_summary(mine).status == "draft"
        assert two.order_summary(theirs).status == "draft"


def test_short_token_cannot_create_an_order(agent_url):
    with AgentDB.connect(agent_url, "too-short") as db, pytest.raises(DbRefusal) as exc:
        db.create_order("Ana", [{"menu_item_id": PIZZA}])
    assert exc.value.sqlstate == "42501"
    assert "too-short" not in str(exc.value)
