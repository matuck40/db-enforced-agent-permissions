"""Runtime settings, read from environment variables only."""

import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

PROVIDERS = ("openai", "anthropic")


@dataclass(frozen=True)
class Settings:
    database_url: str
    provider: Literal["openai", "anthropic"] = "openai"
    model: str | None = None
    langfuse: bool = False
    reasoning_effort: str | None = None

    @classmethod
    def from_env(cls, env: Mapping[str, str] = os.environ) -> "Settings":
        database_url = env.get("AGENT_DATABASE_URL")
        if not database_url:
            raise ValueError("AGENT_DATABASE_URL is required")
        provider = env.get("AGENT_PROVIDER") or "openai"
        if provider not in PROVIDERS:
            raise ValueError(f"AGENT_PROVIDER must be one of {PROVIDERS}")
        langfuse = all(
            env.get(k) for k in ("LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY", "LANGFUSE_HOST")
        )
        return cls(
            database_url=database_url,
            provider=provider,  # type: ignore[arg-type]
            model=env.get("AGENT_MODEL") or None,
            langfuse=bool(langfuse),
            reasoning_effort=env.get("AGENT_REASONING_EFFORT") or None,
        )
