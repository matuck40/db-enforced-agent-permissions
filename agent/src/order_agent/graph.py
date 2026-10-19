"""The LangGraph ordering agent.

    model -> validate -> tools -> model        (loop while the model calls tools)
                     \\-> model                 (every call refused)
    tools -> human                             (after request_human; ends the turn)

The model proposes, validation and the database decide. Every tool call gets exactly
one ToolMessage before the next model call: refusals from the validate node, accepted
calls from ToolNode.
"""

from __future__ import annotations

import uuid
from typing import Any

from langchain_core.messages import AIMessage, SystemMessage
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.prebuilt import ToolNode
from langgraph.types import Command, Send, interrupt

from order_agent.db import AgentDB
from order_agent.prompt import SYSTEM_PROMPT, TASK_ONLY_PROMPT
from order_agent.state import OrderState
from order_agent.tools import make_tools
from order_agent.validate import validate_tool_calls

# Pass with a thread id: {**RUN_CONFIG, "configurable": {"thread_id": ...}}
RUN_CONFIG: dict[str, Any] = {"recursion_limit": 25}


def _last_ai(state: OrderState) -> AIMessage:
    return state["messages"][-1]


def build_graph(model, db: AgentDB, guarded: bool = True) -> CompiledStateGraph:
    tools = make_tools(db)
    bound = model.bind_tools(tools)
    prompt = SYSTEM_PROMPT if guarded else TASK_ONLY_PROMPT

    def call_model(state: OrderState) -> dict:
        reply = bound.invoke([SystemMessage(prompt), *state["messages"]])
        # A provider may omit a call id; the reply for it needs one to match.
        for call in reply.tool_calls:
            if not call.get("id"):
                call["id"] = f"call_{uuid.uuid4().hex[:12]}"
        return {"messages": [reply]}

    def validate(state: OrderState) -> Command:
        """Answer refused calls here; send each accepted call to ToolNode on its own."""
        calls = _last_ai(state).tool_calls
        if guarded:
            menu_ids = {item.id for item in db.menu()}  # harness read, not a model action
            accepted, refusals, events = validate_tool_calls(calls, state, menu_ids)
        else:
            accepted, refusals, events = list(calls), [], []
        sends = [
            Send("tools", {"__type": "tool_call_with_context", "tool_call": c, "state": state})
            for c in accepted
        ]
        return Command(update={"messages": refusals, "events": events}, goto=sends or "model")

    def human(state: OrderState) -> dict:
        interrupt({"reason": state["handoff_reason"]})
        return {}

    def after_model(state: OrderState) -> str:
        return "validate" if _last_ai(state).tool_calls else END

    def after_tools(state: OrderState) -> str:
        return "human" if state.get("handoff_reason") else "model"

    graph = StateGraph(OrderState)
    graph.add_node("model", call_model)
    graph.add_node("validate", validate, destinations=("tools", "model"))
    graph.add_node("tools", ToolNode(tools))
    graph.add_node("human", human)
    graph.add_edge(START, "model")
    graph.add_conditional_edges("model", after_model, ["validate", END])
    graph.add_conditional_edges("tools", after_tools, ["human", "model"])
    graph.add_edge("human", END)
    return graph.compile(checkpointer=MemorySaver())
