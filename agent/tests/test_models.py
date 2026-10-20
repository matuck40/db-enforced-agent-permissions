import pytest
from langchain_anthropic import ChatAnthropic
from langchain_openai import ChatOpenAI

from order_agent.config import Settings
from order_agent.models import make_model


def settings(**kw):
    base = {
        "database_url": "postgresql://u:p@h/db",
        "provider": "openai",
        "model": "fake-model",
        "langfuse": False,
    }
    return Settings(**{**base, **kw})


def test_make_model_openai_type(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "fake-key-for-tests")
    m = make_model(settings())
    assert isinstance(m, ChatOpenAI)
    assert m.temperature == 0
    assert m.max_tokens == 800
    assert m.store is False
    assert m.request_timeout == 60
    assert m.max_retries == 2
    assert m.reasoning_effort is None


def test_make_model_openai_reasoning_effort(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "fake-key-for-tests")
    m = make_model(settings(reasoning_effort="low"))
    assert m.reasoning_effort == "low"


def test_make_model_anthropic_type(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-key-for-tests")
    m = make_model(settings(provider="anthropic"))
    assert isinstance(m, ChatAnthropic)
    assert m.temperature == 0
    assert m.max_tokens == 800
    assert m.default_request_timeout == 60
    assert m.max_retries == 2


def test_make_model_requires_model():
    with pytest.raises(ValueError, match="AGENT_MODEL is required in real mode"):
        make_model(settings(model=None))
