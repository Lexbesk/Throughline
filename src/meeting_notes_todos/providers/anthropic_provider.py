"""Anthropic Claude provider — the default provider (build plan §4.1)."""

from __future__ import annotations

from typing import Any

import anthropic

from .base import ChatResult, CompletionResult, LLMProvider, ToolCall, Usage


def _join_text(resp: Any) -> str:
    return "".join(
        block.text
        for block in getattr(resp, "content", None) or []
        if getattr(block, "type", None) == "text"
    )


def _usage_of(resp: Any) -> Usage:
    usage = getattr(resp, "usage", None)
    return Usage(
        input_tokens=getattr(usage, "input_tokens", 0) or 0,
        output_tokens=getattr(usage, "output_tokens", 0) or 0,
    )


class AnthropicProvider(LLMProvider):
    """Calls the Claude API via the official Anthropic SDK.

    The SDK reads ``ANTHROPIC_API_KEY`` from the environment, so no key is passed
    here. A pre-built ``client`` can be injected (used by tests).
    """

    def __init__(
        self,
        model: str,
        *,
        max_tokens: int = 1024,
        temperature: float | None = None,
        client: anthropic.Anthropic | None = None,
    ) -> None:
        self._model = model
        self._max_tokens = max_tokens
        self._temperature = temperature
        self._client = client or anthropic.Anthropic()

    def complete(
        self,
        *,
        system_prompt: str,
        user_content: str,
        max_tokens: int | None = None,
        response_schema: Any = None,
    ) -> CompletionResult:
        kwargs: dict[str, Any] = {
            "model": self._model,
            "max_tokens": max_tokens or self._max_tokens,
            "system": system_prompt,
            "messages": [{"role": "user", "content": user_content}],
        }
        if self._temperature is not None:
            kwargs["temperature"] = self._temperature

        # Prefer the provider's native structured-output mode when a schema is
        # requested (build plan §4.3): messages.parse sanitizes the schema and
        # validates the response into the pydantic model for us.
        if response_schema is not None and hasattr(self._client.messages, "parse"):
            resp = self._client.messages.parse(output_format=response_schema, **kwargs)
            return CompletionResult(
                text=_join_text(resp),
                usage=_usage_of(resp),
                raw=resp,
                parsed=getattr(resp, "parsed_output", None),
            )

        resp = self._client.messages.create(**kwargs)
        return CompletionResult(text=_join_text(resp), usage=_usage_of(resp), raw=resp)

    def chat(
        self,
        *,
        system_prompt: str,
        messages: list[dict],
        tools: list[dict] | None = None,
        max_tokens: int | None = None,
    ) -> ChatResult:
        """Chat turn via native SDK tool use (v2 §4.8); tool calls are not executed."""
        kwargs: dict[str, Any] = {
            "model": self._model,
            "max_tokens": max_tokens or self._max_tokens,
            "system": system_prompt,
            "messages": messages,
        }
        if tools:
            kwargs["tools"] = tools
        if self._temperature is not None:
            kwargs["temperature"] = self._temperature

        resp = self._client.messages.create(**kwargs)
        calls = [
            ToolCall(
                id=getattr(block, "id", "") or "",
                name=block.name,
                input=dict(block.input or {}),
            )
            for block in (getattr(resp, "content", None) or [])
            if getattr(block, "type", None) == "tool_use"
        ]
        return ChatResult(text=_join_text(resp), tool_calls=calls, usage=_usage_of(resp), raw=resp)
