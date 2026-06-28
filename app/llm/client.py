"""Provider-agnostic LLM + Vision wrapper.

Selects between Anthropic (default) and OpenAI based on LLM_PROVIDER env var.
Import get_llm() for tool-calling agents; get_vision_llm() for OCR tasks.
"""
from __future__ import annotations

from langchain_core.language_models import BaseChatModel

from app.config import Settings, get_settings


def get_llm(model: str | None = None, settings: Settings | None = None) -> BaseChatModel:
    """Return a chat model for the configured provider."""
    cfg = settings or get_settings()
    m = model or cfg.LLM_MODEL
    if cfg.LLM_PROVIDER == "anthropic":
        from langchain_anthropic import ChatAnthropic

        return ChatAnthropic(
            model=m,
            api_key=cfg.ANTHROPIC_API_KEY,
            max_tokens=2048,
        )
    else:
        from langchain_openai import ChatOpenAI

        return ChatOpenAI(model=m, api_key=cfg.OPENAI_API_KEY)


def get_vision_llm(model: str | None = None, settings: Settings | None = None) -> BaseChatModel:
    """Return a vision-capable chat model for OCR / image analysis."""
    cfg = settings or get_settings()
    m = model or cfg.VISION_MODEL
    if cfg.LLM_PROVIDER == "anthropic":
        from langchain_anthropic import ChatAnthropic

        return ChatAnthropic(
            model=m,
            api_key=cfg.ANTHROPIC_API_KEY,
            max_tokens=1024,
        )
    else:
        from langchain_openai import ChatOpenAI

        return ChatOpenAI(model=m, api_key=cfg.OPENAI_API_KEY)
