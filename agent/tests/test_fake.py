import pytest
from langchain_core.messages import AIMessage, HumanMessage

from order_agent.fake import ScriptedChatModel, ScriptExhausted


def test_returns_script_in_order():
    model = ScriptedChatModel(script=[{"content": "one"}, {"content": "two"}])
    assert model.invoke([HumanMessage("a")]).content == "one"
    assert model.invoke([HumanMessage("b")]).content == "two"


def test_tool_calls_become_aimessage_tool_calls():
    model = ScriptedChatModel(
        script=[
            {"content": "", "tool_calls": [{"name": "a", "args": {"x": 1}}]},
            {"content": "", "tool_calls": [{"name": "b", "args": {}}, {"name": "c", "args": {}}]},
        ]
    )
    bound = model.bind_tools([])
    first = bound.invoke([HumanMessage("hi")])
    second = bound.invoke([HumanMessage("hi")])
    assert isinstance(first, AIMessage)
    assert first.tool_calls == [
        {"name": "a", "args": {"x": 1}, "id": "call_0", "type": "tool_call"}
    ]
    assert [c["id"] for c in second.tool_calls] == ["call_1", "call_2"]


def test_exhausted_script_raises():
    model = ScriptedChatModel(script=[{"content": "only"}])
    model.invoke([HumanMessage("a")])
    with pytest.raises(ScriptExhausted, match="1 entries"):
        model.invoke([HumanMessage("b")])


def test_records_received_messages():
    model = ScriptedChatModel(script=[{"content": "x"}])
    msgs = [HumanMessage("hello")]
    model.invoke(msgs)
    msgs.append(HumanMessage("later"))
    assert len(model.calls) == 1
    assert [m.content for m in model.calls[0]] == ["hello"]


def test_structured_output_returns_json_entry():
    model = ScriptedChatModel(script=[{"json": {"intent": "x"}}])
    out = model.with_structured_output({"type": "object"}, strict=True).invoke([HumanMessage("q")])
    assert out == {"intent": "x"}
    assert len(model.calls) == 1


def test_bound_models_share_cursor_and_calls():
    model = ScriptedChatModel(script=[{"content": "a"}, {"json": {"k": 1}}, {"content": "c"}])
    model.bind_tools([]).invoke([HumanMessage("1")])
    model.with_structured_output({}, strict=True).invoke([HumanMessage("2")])
    assert model.invoke([HumanMessage("3")]).content == "c"
    assert len(model.calls) == 3


def test_misaligned_entries_fail_loudly():
    model = ScriptedChatModel(script=[{"content": "a"}, {"json": {}}])
    with pytest.raises(ValueError, match="entry 0"):
        model.with_structured_output({}, strict=True).invoke([HumanMessage("x")])
    model2 = ScriptedChatModel(script=[{"json": {}}])
    with pytest.raises(ValueError, match="entry 0"):
        model2.invoke([HumanMessage("x")])


def test_usage_metadata_default_and_configured():
    model = ScriptedChatModel(
        script=[
            {"content": "a"},
            {"content": "b", "usage": {"input_tokens": 3, "output_tokens": 2, "total_tokens": 5}},
        ]
    )
    assert model.invoke([HumanMessage("x")]).usage_metadata == {
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
    }
    assert model.invoke([HumanMessage("x")]).usage_metadata["total_tokens"] == 5


def test_structured_output_fires_callbacks_with_usage():
    from langchain_core.callbacks import BaseCallbackHandler

    seen = []

    class Spy(BaseCallbackHandler):
        def on_llm_end(self, response, **kwargs):
            seen.append(response.generations[0][0].message.usage_metadata)

    usage = {"input_tokens": 7, "output_tokens": 3, "total_tokens": 10}
    model = ScriptedChatModel(script=[{"json": {"k": 1}, "usage": usage}])
    model.callbacks = [Spy()]
    out = model.with_structured_output({}, strict=True).invoke([HumanMessage("q")])
    assert out == {"k": 1}
    assert seen == [usage]
