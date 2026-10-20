"""The ordering graph, driven by a scripted model against the real test database."""

from decimal import Decimal

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from test_db import PIZZA, TOKEN

from order_agent.db import AgentDB
from order_agent.fake import ScriptedChatModel
from order_agent.graph import RUN_CONFIG, build_graph
from order_agent.prompt import PROMPT_VERSION, SYSTEM_PROMPT, TASK_ONLY_PROMPT

OFF_MENU = "00000000-0000-0000-0000-000000000111"


@pytest.fixture
def db(agent_url):
    with AgentDB.connect(agent_url, TOKEN) as conn:
        yield conn


def config(thread: str) -> dict:
    return {**RUN_CONFIG, "configurable": {"thread_id": thread}}


def start(text: str = "hi") -> dict:
    return {"messages": [HumanMessage(text)], "cart": {}, "order_ids": [], "events": []}


def step(name: str, **args) -> dict:
    return {"name": name, "args": args}


def tool_messages(state) -> list[ToolMessage]:
    return [m for m in state["messages"] if isinstance(m, ToolMessage)]


class MarkerModel(ScriptedChatModel):
    """Test helper: replaces "$ORDER_ID" in scripted args with the id of the order that
    the last submit_order reply reported. Production never rewrites model arguments."""

    def _generate(self, messages, *args, **kwargs):
        result = super()._generate(messages, *args, **kwargs)
        order_id = None
        for m in messages:
            if (
                isinstance(m, ToolMessage)
                and m.content.startswith("order ")
                and " created" in m.content
            ):
                order_id = m.content.split()[1]
        for call in result.generations[0].message.tool_calls:
            call["args"] = {
                k: (order_id if v == "$ORDER_ID" else v) for k, v in call["args"].items()
            }
        return result


def test_happy_path_creates_and_confirms_order(db, admin_conn):
    model = MarkerModel(
        script=[
            {"tool_calls": [step("get_menu")]},
            {"tool_calls": [step("add_to_cart", menu_item_id=PIZZA, quantity=2)]},
            {"tool_calls": [step("submit_order")]},
            {"tool_calls": [step("update_order_status", order_id="$ORDER_ID", status="submitted")]},
            {"tool_calls": [step("get_order_status", order_id="$ORDER_ID")]},
            {"content": "Your order is in."},
        ]
    )
    state = build_graph(model, db).invoke(start("two pizzas"), config("happy"))
    assert len(state["order_ids"]) == 1
    row = admin_conn.execute(
        "select status, total from app.orders where id = %s", (state["order_ids"][-1],)
    ).fetchone()
    assert row == ("submitted", Decimal("84.00"))
    assert all(e.outcome == "ok" for e in state["events"])
    assert "status submitted" in tool_messages(state)[-1].content


def test_request_human_interrupts(db):
    model = ScriptedChatModel(
        script=[{"tool_calls": [step("request_human", reason="allergy question")]}]
    )
    graph = build_graph(model, db)
    out = graph.invoke(start("I am allergic to nuts"), config("human"))
    assert out["__interrupt__"][0].value == {"reason": "allergy question"}


def test_validation_refusal_goes_back_to_model(db):
    model = ScriptedChatModel(
        script=[
            {"tool_calls": [step("add_to_cart", menu_item_id="not-an-id", quantity=1)]},
            {"content": "Sorry, that is not on the menu."},
        ]
    )
    state = build_graph(model, db).invoke(start(), config("refusal"))
    assert [(e.decided_by, e.outcome) for e in state["events"]] == [("validation", "refused")]
    assert state["cart"] == {}
    assert tool_messages(state)[0].content.startswith("refused by validation")
    assert state["messages"][-1].content == "Sorry, that is not on the menu."


def test_unguarded_reaches_database(db):
    # Quantity 500 is stopped by validation when guarded; unguarded, the database decides.
    script = [
        {"tool_calls": [step("add_to_cart", menu_item_id=PIZZA, quantity=500)]},
        {"tool_calls": [step("submit_order")]},
        {"content": "ok"},
    ]
    state = build_graph(ScriptedChatModel(script=script), db, guarded=False).invoke(
        start(), config("unguarded")
    )
    assert [(e.decided_by, e.outcome, e.detail) for e in state["events"]] == [
        ("database", "refused", "22023")
    ]
    guarded = build_graph(ScriptedChatModel(script=script[:1] + [{"content": "x"}]), db)
    state = guarded.invoke(start(), config("guarded-500"))
    assert [(e.decided_by, e.outcome) for e in state["events"]] == [("validation", "refused")]


def test_mixed_calls_are_all_answered_and_only_accepted_run(db):
    model = ScriptedChatModel(
        script=[
            {
                "tool_calls": [
                    step("add_to_cart", menu_item_id="bogus", quantity=1),
                    step("add_to_cart", menu_item_id=PIZZA, quantity=1),
                ]
            },
            {"content": "added one"},
        ]
    )
    state = build_graph(model, db).invoke(start(), config("mixed"))
    answered = {m.tool_call_id: m.content for m in tool_messages(state)}
    assert set(answered) == {"call_0", "call_1"}
    assert answered["call_0"].startswith("refused by validation")
    assert state["cart"] == {PIZZA: 1}
    assert [e.decided_by for e in state["events"]] == ["validation"]


def test_call_without_id_does_not_crash(db):
    class NoIds(ScriptedChatModel):
        def _generate(self, *args, **kwargs):
            result = super()._generate(*args, **kwargs)
            msg = result.generations[0].message
            msg.tool_calls = [{**c, "id": None} for c in msg.tool_calls]
            return result

    model = NoIds(
        script=[
            {"tool_calls": [step("add_to_cart", menu_item_id="bogus", quantity=1)]},
            {"content": "ok"},
        ]
    )
    state = build_graph(model, db).invoke(start(), config("noid"))
    msgs = tool_messages(state)
    stored = next(m for m in state["messages"] if isinstance(m, AIMessage))
    assert stored.tool_calls[0]["id"]
    assert [m.tool_call_id for m in msgs] == [stored.tool_calls[0]["id"]]


def test_system_prompt_is_prepended_but_not_stored(db):
    for guarded, prompt in ((True, SYSTEM_PROMPT), (False, TASK_ONLY_PROMPT)):
        model = ScriptedChatModel(script=[{"content": "hello"}])
        state = build_graph(model, db, guarded=guarded).invoke(start(), config(f"p{guarded}"))
        first = model.calls[0][0]
        assert isinstance(first, SystemMessage) and first.content == prompt
        assert not any(isinstance(m, SystemMessage) for m in state["messages"])
        assert TOKEN not in prompt
        assert "postgresql://" not in prompt and "password" not in prompt.lower()


def test_prompt_version_is_stable():
    import hashlib

    assert PROMPT_VERSION == hashlib.sha256(SYSTEM_PROMPT.encode()).hexdigest()[:12]
    assert len(PROMPT_VERSION) == 12
    assert TASK_ONLY_PROMPT != SYSTEM_PROMPT


def test_handoff_in_parallel_calls_ends_the_turn(db):
    model = ScriptedChatModel(
        script=[
            {
                "tool_calls": [
                    step("add_to_cart", menu_item_id=PIZZA, quantity=1),
                    step("request_human", reason="allergy"),
                ]
            }
        ]
    )
    graph = build_graph(model, db)
    out = graph.invoke(start(), config("parallel-handoff"))
    assert out["__interrupt__"][0].value == {"reason": "allergy"}
    assert len(model.calls) == 1
    assert out["cart"] == {PIZZA: 1}


def test_resumed_handoff_clears_reason(db):
    from langgraph.types import Command

    model = ScriptedChatModel(
        script=[
            {"tool_calls": [step("request_human", reason="complaint")]},
            {"tool_calls": [step("get_menu")]},
            {"content": "back"},
        ]
    )
    graph = build_graph(model, db)
    cfg = config("resume")
    graph.invoke(start(), cfg)
    state = graph.invoke(Command(resume="handled"), cfg)
    assert state["handoff_reason"] is None


def test_checkpointer_serializes_events_without_warning(db, caplog):
    """Event objects in the state round-trip through MemorySaver without a serde warning."""
    from langgraph.checkpoint.serde import jsonplus

    from order_agent.events import Event

    # The library warns once per type and process; forget earlier warnings to see this run's.
    jsonplus._warned_unregistered_types.clear()
    model = ScriptedChatModel(
        script=[
            {"tool_calls": [step("add_to_cart", menu_item_id=OFF_MENU, quantity=1)]},
            {"content": "sorry"},
        ]
    )
    graph = build_graph(model, db)
    cfg = config("serde-warning")
    with caplog.at_level("WARNING"):
        graph.invoke(start("pizza"), cfg)
        values = graph.get_state(cfg).values
    assert values["events"], "the refused call should have left an audit event"
    assert all(isinstance(e, Event) for e in values["events"])
    assert [r.getMessage() for r in caplog.records if "checkpoint" in r.name] == []
