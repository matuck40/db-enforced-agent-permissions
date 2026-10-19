"""Optional Langfuse tracing: off unless asked for, real mode only, never the token."""

from __future__ import annotations

import builtins

import evals.run as run_module
import pytest
from evals.run import run_conversation
from langchain_core.callbacks import BaseCallbackHandler
from test_scoring import conv

from order_agent.config import Settings
from order_agent.db import AgentDB
from order_agent.fake import ScriptedChatModel
from order_agent.tracing import make_callbacks

LANGFUSE_ENV = {
    "LANGFUSE_PUBLIC_KEY": "pk-lf-fake",
    "LANGFUSE_SECRET_KEY": "sk-lf-fake",
    "LANGFUSE_HOST": "https://langfuse.invalid",
}
BASE = {"AGENT_DATABASE_URL": "postgresql://u:p@h/db"}


def test_no_env_no_callbacks():
    assert make_callbacks(Settings.from_env(BASE), "real") == []


def test_offline_mode_never_traces(monkeypatch):
    settings = Settings.from_env({**BASE, **LANGFUSE_ENV})
    assert settings.langfuse is True

    real_import = builtins.__import__

    def no_langfuse(name, *args, **kwargs):
        assert not name.startswith("langfuse"), "offline mode must not even import langfuse"
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_langfuse)
    assert make_callbacks(settings, "offline") == []


def test_env_without_package_raises(monkeypatch):
    settings = Settings.from_env({**BASE, **LANGFUSE_ENV})
    real_import = builtins.__import__

    def missing(name, *args, **kwargs):
        if name.startswith("langfuse"):
            raise ImportError("No module named 'langfuse'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", missing)
    with pytest.raises(RuntimeError, match="uv sync --extra tracing"):
        make_callbacks(settings, "real")


class Capture(BaseCallbackHandler):
    """Records everything a tracing handler would be given."""

    def __init__(self):
        self.seen: list[str] = []

    def on_chain_start(self, serialized, inputs, **kwargs):
        self.seen.append(repr((serialized, inputs, kwargs)))

    def on_chat_model_start(self, serialized, messages, **kwargs):
        self.seen.append(repr((serialized, messages, kwargs)))

    def on_llm_end(self, response, **kwargs):
        self.seen.append(repr((response, kwargs)))

    def on_tool_start(self, serialized, input_str, **kwargs):
        self.seen.append(repr((serialized, input_str, kwargs)))

    def on_tool_end(self, output, **kwargs):
        self.seen.append(repr((output, kwargs)))


def test_conversation_token_never_reaches_the_trace(monkeypatch, admin_conn, agent_url):
    token = "TOKEN-must-not-leak-0123456789abcdefghijklmnop"
    monkeypatch.setattr(run_module.secrets, "token_urlsafe", lambda n=32: token)
    handed: list[str] = []

    def factory(t: str) -> AgentDB:
        handed.append(t)
        return AgentDB.connect(agent_url, t)

    capture = Capture()
    model = ScriptedChatModel(
        script=[
            {"tool_calls": [{"name": "get_menu", "args": {}}]},
            {"content": "menu"},
        ]
    )
    tracing = {
        "callbacks": [capture],
        "run_name": "c1",
        "metadata": {"langfuse_session_id": "2026-10-19-tools"},
    }
    run_conversation(conv(), "tools", model, factory, admin_conn, tracing=tracing)
    assert handed == [token]  # the database got it
    assert capture.seen  # the handler really saw the run
    assert not any(token in s for s in capture.seen)


def test_real_run_attaches_one_trace_per_conversation(monkeypatch, admin_conn, agent_url):
    """run_real hands the tracing handler, name and metadata to every run (fake handler)."""
    seen_names: list[str] = []
    seen_meta: list[dict] = []

    class Spy(BaseCallbackHandler):
        def on_chain_start(self, serialized, inputs, *, metadata=None, name=None, **kwargs):
            if kwargs.get("parent_run_id") is None:
                seen_names.append(name)
                seen_meta.append(metadata or {})

    monkeypatch.setattr(
        run_module, "make_callbacks", lambda settings, mode, trace_seed=None: [Spy()]
    )
    monkeypatch.setattr(run_module, "flush", lambda handlers: None)
    settings = Settings.from_env({**BASE, **LANGFUSE_ENV})
    prices = {"models": {"m": {"input": 1.0, "cached_input": 0.5, "output": 2.0}}}
    conv_dir = run_module.ROOT / "evals" / "conversations"
    outcome = run_module.run_real(
        conv_dir,
        ["tools"],
        admin_conn,
        agent_url,
        lambda: ScriptedChatModel(script=[{"content": "ok"}] * 500),
        "m",
        prices,
        100.0,
        settings=settings,
        schema_sha="abc123",
    )
    ids = [c for c, _ in outcome.scores["tools"]]
    # One root run per customer turn, all named after their conversation, in order.
    assert list(dict.fromkeys(seen_names)) == ids
    assert all(m["langfuse_session_id"].endswith("-tools") for m in seen_meta)
    assert all(m["schema_sha"] == "abc123" and m["prompt_version"] for m in seen_meta)


def _fake_langfuse(monkeypatch):
    """A stand-in langfuse package: deterministic ids from a seed, handler keeps its context."""
    import hashlib
    import sys
    import types

    class Handler(BaseCallbackHandler):
        def __init__(self, trace_context=None):
            self.trace_context = trace_context

    class Langfuse:
        @staticmethod
        def create_trace_id(*, seed=None):
            return hashlib.sha256(seed.encode()).hexdigest()[:32]

    top = types.ModuleType("langfuse")
    top.Langfuse = Langfuse
    sub = types.ModuleType("langfuse.langchain")
    sub.CallbackHandler = Handler
    monkeypatch.setitem(sys.modules, "langfuse", top)
    monkeypatch.setitem(sys.modules, "langfuse.langchain", sub)


def test_trace_id_is_per_conversation(monkeypatch):
    _fake_langfuse(monkeypatch)
    settings = Settings.from_env({**BASE, **LANGFUSE_ENV})

    def trace_id(seed):
        return make_callbacks(settings, "real", trace_seed=seed)[0].trace_context["trace_id"]

    turn_1 = trace_id("2026-10-19-tools-c1")
    turn_2 = trace_id("2026-10-19-tools-c1")  # a later turn of the same conversation
    assert turn_1 == turn_2
    assert trace_id("2026-10-19-tools-c2") != turn_1
    assert trace_id("2026-10-19-baseline-c1") != turn_1
