"""A scripted chat model: deterministic, offline, zero tokens.

The script is a list of plain dicts consumed in order, one per model call:

    {"content": str, "tool_calls": [{"name": str, "args": dict}], "usage": {...}}
    {"json": dict, "usage": {...}}   # consumed by with_structured_output (strict-JSON baseline)
"""

from __future__ import annotations

from typing import Any

from langchain_core.callbacks import CallbackManager
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult, LLMResult
from langchain_core.runnables import RunnableConfig, RunnableLambda
from pydantic import Field, PrivateAttr

_ZERO_USAGE = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}


class ScriptExhausted(Exception):
    """The model was called more times than the script has entries."""


class ScriptedChatModel(BaseChatModel):
    script: list[dict[str, Any]]
    calls: list[list[BaseMessage]] = Field(default_factory=list)
    _cursor: int = PrivateAttr(default=0)
    _tool_calls_seen: int = PrivateAttr(default=0)

    @property
    def _llm_type(self) -> str:
        return "scripted-fake"

    @property
    def consumed(self) -> int:
        """How many script entries the model has used so far."""
        return self._cursor

    def _next(self, messages: list[BaseMessage], *, structured: bool) -> dict[str, Any]:
        self.calls.append(list(messages))
        index = self._cursor
        if index >= len(self.script):
            raise ScriptExhausted(f"script exhausted: it had {len(self.script)} entries")
        entry = self.script[index]
        if structured != ("json" in entry):
            want = "a 'json' entry" if structured else "a non-'json' entry"
            raise ValueError(f"script misaligned: entry {index} should be {want}, got {entry!r}")
        self._cursor += 1
        return entry

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        entry = self._next(messages, structured=False)
        tool_calls = []
        for call in entry.get("tool_calls", []):
            tool_calls.append(
                {
                    "name": call["name"],
                    "args": call.get("args", {}),
                    "id": f"call_{self._tool_calls_seen}",
                    "type": "tool_call",
                }
            )
            self._tool_calls_seen += 1
        message = AIMessage(
            content=entry.get("content", ""),
            tool_calls=tool_calls,
            usage_metadata=dict(entry.get("usage", _ZERO_USAGE)),
        )
        return ChatResult(generations=[ChatGeneration(message=message)])

    def bind_tools(self, tools: Any, **kwargs: Any) -> ScriptedChatModel:
        # Tools are irrelevant to a script; returning self shares cursor and calls.
        return self

    def with_structured_output(self, schema: Any, **kwargs: Any) -> RunnableLambda:
        def run(value: Any, config: RunnableConfig) -> dict[str, Any]:
            messages = self._convert_input(value).to_messages()
            # A real chat model reports every call, structured ones included, to its
            # callbacks (the model's own and the run's); the fake does the same, with usage.
            manager = CallbackManager.configure(
                inheritable_callbacks=config.get("callbacks"), local_callbacks=self.callbacks
            )
            run_manager = manager.on_chat_model_start({}, [messages])[0]
            try:
                entry = self._next(messages, structured=True)
            except Exception as exc:
                run_manager.on_llm_error(exc)
                raise
            message = AIMessage(
                content=str(entry["json"]),
                usage_metadata=dict(entry.get("usage", _ZERO_USAGE)),
            )
            run_manager.on_llm_end(LLMResult(generations=[[ChatGeneration(message=message)]]))
            return entry["json"]

        return RunnableLambda(run)
