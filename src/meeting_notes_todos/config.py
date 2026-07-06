"""Configuration loading: ``config.toml`` + pydantic validation.

Secrets are not stored here — the Anthropic SDK reads ``ANTHROPIC_API_KEY`` from
the environment. This module holds only non-secret settings (provider, model,
generation params, store path, and prompt-file locations).
"""

from __future__ import annotations

import tomllib
from pathlib import Path

from pydantic import BaseModel, Field


class LLMConfig(BaseModel):
    provider: str = "anthropic"
    model: str = "claude-haiku-4-5"  # the startup model (the default tier's string)
    max_tokens: int = 1024
    temperature: float | None = None
    base_url: str | None = None  # for local/openai providers (OpenAI-compatible endpoint)
    # v3 M14: the global model switch — tier names the UI offers, mapped to concrete
    # model strings here so a changing lineup is a config edit, not code
    tiers: dict[str, str] = Field(
        default_factory=lambda: {
            "Haiku 4.5": "claude-haiku-4-5",
            "Sonnet 5": "claude-sonnet-5",
            "Opus 4.8": "claude-opus-4-8",
            "Fable 5": "claude-fable-5",
            "GPT 4.1 mini": "gpt-4.1-mini",
            "GPT 5.5": "gpt-5.5",
        }
    )

    def model_for_tier(self, tier: str | None) -> str:
        """Resolve a tier name to its model string; no/unknown tier → the startup model."""
        if tier and tier in self.tiers:
            return self.tiers[tier]
        return self.model


class StoreConfig(BaseModel):
    backend: str = "markdown"  # markdown (local files) | postgres (v4: DATABASE_URL)
    path: Path = Path("data/todos.md")  # markdown backend only


class PromptsConfig(BaseModel):
    dir: Path = Path("prompts")
    system: str = "system.md"
    extract: str = "extract.md"
    reconcile: str = "reconcile.md"
    chat: str = "assistant.md"  # the unified assistant prompt (v3 M12)


class UsageConfig(BaseModel):
    path: Path = Path("data/usage.jsonl")


class Config(BaseModel):
    llm: LLMConfig = Field(default_factory=LLMConfig)
    store: StoreConfig = Field(default_factory=StoreConfig)
    prompts: PromptsConfig = Field(default_factory=PromptsConfig)
    usage: UsageConfig = Field(default_factory=UsageConfig)


DEFAULT_CONFIG_PATH = Path("config.toml")


def load_config(path: str | Path = DEFAULT_CONFIG_PATH) -> Config:
    """Load and validate configuration from a TOML file.

    A missing file or missing sections fall back to the model defaults, so the
    app is runnable even before ``config.toml`` is customized.
    """
    path = Path(path)
    if path.exists():
        with path.open("rb") as f:
            data = tomllib.load(f)
    else:
        data = {}
    return Config(**data)
