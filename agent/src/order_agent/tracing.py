"""Optional Langfuse tracing for real eval runs.

Off unless all three LANGFUSE_* variables are set (``Settings.langfuse``) and the mode is
real. The Langfuse SDK reads the keys and the host from those same environment variables;
nothing is passed here. The package is an optional extra (``uv sync --extra tracing``), so
it is imported only when a handler is actually built.
"""

from __future__ import annotations

from langchain_core.callbacks import BaseCallbackHandler

from order_agent.config import Settings


def make_callbacks(settings: Settings, mode: str) -> list[BaseCallbackHandler]:
    """The tracing handlers for ``mode`` ("offline" or "real"); empty when tracing is off."""
    if mode != "real" or not settings.langfuse:
        return []
    try:
        from langfuse.langchain import CallbackHandler
    except ImportError as exc:
        raise RuntimeError(
            "LANGFUSE_* variables are set but the langfuse package is not installed; "
            "run `uv sync --extra tracing`, or unset them to run without tracing"
        ) from exc
    return [CallbackHandler()]


def flush(handlers: list[BaseCallbackHandler]) -> None:
    """Send buffered traces before the process exits; a no-op without handlers."""
    if not handlers:
        return
    from langfuse import get_client

    get_client().flush()
