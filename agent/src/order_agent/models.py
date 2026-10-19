"""Build the real chat model for the configured provider."""

from langchain_core.language_models.chat_models import BaseChatModel

from order_agent.config import Settings


def make_model(settings: Settings) -> BaseChatModel:
    if not settings.model:
        raise ValueError("AGENT_MODEL is required in real mode")
    if settings.provider == "anthropic":
        from langchain_anthropic import ChatAnthropic

        return ChatAnthropic(model=settings.model, temperature=0, max_tokens=800)
    from langchain_openai import ChatOpenAI

    kwargs = {}
    if settings.reasoning_effort:
        kwargs["reasoning_effort"] = settings.reasoning_effort
    return ChatOpenAI(model=settings.model, temperature=0, max_tokens=800, store=False, **kwargs)
