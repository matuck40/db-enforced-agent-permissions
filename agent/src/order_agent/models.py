"""Build the real chat model for the configured provider."""

from langchain_core.language_models.chat_models import BaseChatModel

from order_agent.config import Settings

# A hung request must not stall a run that is also bounded by a cost cap.
REQUEST_TIMEOUT_S = 60
MAX_RETRIES = 2


def make_model(settings: Settings) -> BaseChatModel:
    if not settings.model:
        raise ValueError("AGENT_MODEL is required in real mode")
    if settings.provider == "anthropic":
        from langchain_anthropic import ChatAnthropic

        return ChatAnthropic(
            model=settings.model,
            temperature=0,
            max_tokens=800,
            timeout=REQUEST_TIMEOUT_S,
            max_retries=MAX_RETRIES,
        )
    from langchain_openai import ChatOpenAI

    kwargs = {}
    if settings.reasoning_effort:
        kwargs["reasoning_effort"] = settings.reasoning_effort
    return ChatOpenAI(
        model=settings.model,
        temperature=0,
        max_tokens=800,
        store=False,
        timeout=REQUEST_TIMEOUT_S,
        max_retries=MAX_RETRIES,
        **kwargs,
    )
