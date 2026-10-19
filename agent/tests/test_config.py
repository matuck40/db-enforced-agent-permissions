import dataclasses

import pytest

from order_agent.config import Settings

BASE = {"AGENT_DATABASE_URL": "postgresql://u:p@h/db"}


def test_default_provider_is_openai():
    s = Settings.from_env(BASE)
    assert s.provider == "openai"
    assert s.model is None
    assert s.reasoning_effort is None
    assert s.langfuse is False


def test_unknown_provider_rejected():
    with pytest.raises(ValueError, match="AGENT_PROVIDER"):
        Settings.from_env({**BASE, "AGENT_PROVIDER": "nope"})


def test_database_url_required():
    with pytest.raises(ValueError, match="AGENT_DATABASE_URL"):
        Settings.from_env({})


def test_model_and_effort_read():
    s = Settings.from_env(
        {**BASE, "AGENT_PROVIDER": "anthropic", "AGENT_MODEL": "m", "AGENT_REASONING_EFFORT": "low"}
    )
    assert (s.provider, s.model, s.reasoning_effort) == ("anthropic", "m", "low")


def test_langfuse_needs_all_three_vars():
    full = {
        "LANGFUSE_PUBLIC_KEY": "pk-lf-fake",
        "LANGFUSE_SECRET_KEY": "sk-lf-fake",
        "LANGFUSE_HOST": "http://localhost:3000",
    }
    assert Settings.from_env({**BASE, **full}).langfuse is True
    for missing in full:
        partial = {k: v for k, v in full.items() if k != missing}
        assert Settings.from_env({**BASE, **partial}).langfuse is False
    assert Settings.from_env({**BASE, **full, "LANGFUSE_HOST": ""}).langfuse is False


def test_settings_frozen():
    s = Settings.from_env(BASE)
    with pytest.raises(dataclasses.FrozenInstanceError):
        s.model = "x"  # type: ignore[misc]
