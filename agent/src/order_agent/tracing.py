"""Optional Langfuse tracing for real eval runs.

Off unless all three LANGFUSE_* variables are set (``Settings.langfuse``) and the mode is
real. The Langfuse SDK reads the keys and the host from those same environment variables;
nothing is passed here. The package is an optional extra (``uv sync --extra tracing``), so
it is imported only when a handler is actually built.
"""

from __future__ import annotations

from langchain_core.callbacks import BaseCallbackHandler

from order_agent.config import Settings


def make_callbacks(
    settings: Settings, mode: str, trace_seed: str | None = None
) -> list[BaseCallbackHandler]:
    """The tracing handlers for ``mode`` ("offline" or "real"); empty when tracing is off.

    With ``trace_seed`` the handler is pinned to the trace id derived from that seed, so every
    graph run it sees (one per customer turn) lands in the same trace. Build one handler per
    conversation and configuration, with a seed that does not contain the conversation token.
    """
    if mode != "real" or not settings.langfuse:
        return []
    try:
        from langfuse import Langfuse
        from langfuse.langchain import CallbackHandler
    except ImportError as exc:
        raise RuntimeError(
            "LANGFUSE_* variables are set but the langfuse package is not installed; "
            "run `uv sync --extra tracing`, or unset them to run without tracing"
        ) from exc
    if trace_seed is None:
        return [CallbackHandler()]
    trace_id = Langfuse.create_trace_id(seed=trace_seed)
    return [CallbackHandler(trace_context={"trace_id": trace_id})]


def flush(handlers: list[BaseCallbackHandler]) -> None:
    """Send buffered traces before the process exits; a no-op without handlers."""
    if not handlers:
        return
    from langfuse import get_client

    get_client().flush()
