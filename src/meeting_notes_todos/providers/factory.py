"""Provider selection (build plan §4.1, acceptance #8; model-string routing for GPT).

Switching providers/models is a config-only change because every caller goes
through :func:`build_provider`. Routing is centralized here and the **model
string picks the vendor**: ``claude-*`` → Anthropic, ``gpt-*`` → OpenAI. An
explicit local setup (``provider = "local"`` or a ``base_url``) always wins, so
locally served models with vendor-like names (e.g. Ollama's ``gpt-oss``) stay
on the local endpoint. When no rule matches, the ``provider`` field decides.
"""

from __future__ import annotations

import os

from ..config import LLMConfig
from .anthropic_provider import AnthropicProvider
from .base import LLMProvider
from .local_provider import LocalProvider
from .openai_provider import OpenAIProvider

_OLLAMA_BASE_URL = "http://localhost:11434/v1"


def resolve_provider_name(llm_config: LLMConfig) -> str:
    """The vendor behind this config: ``anthropic`` | ``openai`` | ``local``.

    Also used by the usage log so recorded provider labels stay truthful when
    the global model switch routes a turn to a different vendor.
    """
    if llm_config.provider == "local" or llm_config.base_url:
        return "local"  # explicit local endpoint wins over model-name prefixes
    model = llm_config.model.lower()
    if model.startswith("claude"):
        return "anthropic"
    if model.startswith("gpt-"):
        return "openai"
    if llm_config.provider in ("anthropic", "openai"):
        return llm_config.provider
    raise ValueError(f"Unknown provider: {llm_config.provider!r}")


def build_provider(llm_config: LLMConfig, api_key: str | None = None) -> LLMProvider:
    """Return the provider implementation for this config (see module docstring).

    ``api_key`` overrides the environment key when supplied — that is how a
    hosted request runs on the *requesting user's* stored key (v4 M18). When it
    is None each provider falls back to its env var (local single-user mode).
    """
    name = resolve_provider_name(llm_config)
    if name == "anthropic":
        return AnthropicProvider(
            model=llm_config.model,
            max_tokens=llm_config.max_tokens,
            temperature=llm_config.temperature,
            api_key=api_key,
        )
    if name == "openai":
        return OpenAIProvider(
            model=llm_config.model,
            max_tokens=llm_config.max_tokens,
            temperature=llm_config.temperature,
            api_key=api_key,
        )
    return LocalProvider(
        model=llm_config.model,
        base_url=llm_config.base_url or _OLLAMA_BASE_URL,
        api_key=api_key or os.environ.get("LOCAL_API_KEY", "not-needed"),
        max_tokens=llm_config.max_tokens,
        temperature=llm_config.temperature,
    )
