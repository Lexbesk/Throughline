"""Anthropic Claude provider — the default provider (build plan §4.1)."""

from __future__ import annotations

from typing import Any

import anthropic

from .base import ChatResult, CompletionResult, LLMProvider, TextDelta, ToolCall, Usage


def _join_text(resp: Any) -> str:
    return "".join(
        block.text
        for block in getattr(resp, "content", None) or []
        if getattr(block, "type", None) == "text"
    )


def _calls_of(resp: Any) -> list[ToolCall]:
    return [
        ToolCall(id=getattr(block, "id", "") or "", name=block.name, input=dict(block.input or {}))
        for block in (getattr(resp, "content", None) or [])
        if getattr(block, "type", None) == "tool_use"
    ]


def _usage_of(resp: Any) -> Usage:
    usage = getattr(resp, "usage", None)
    return Usage(
        input_tokens=getattr(usage, "input_tokens", 0) or 0,
        output_tokens=getattr(usage, "output_tokens", 0) or 0,
    )


class AnthropicProvider(LLMProvider):
    """Calls the Claude API via the official Anthropic SDK.

    ``api_key`` is passed explicitly when supplied (v4 M18: the requesting
    user's key); when it is None the SDK reads ``ANTHROPIC_API_KEY`` from the
    environment (local single-user mode). A pre-built ``client`` can be injected
    (used by tests).
    """

    def __init__(
        self,
        model: str,
        *,
        max_tokens: int = 1024,
        temperature: float | None = None,
        api_key: str | None = None,
        client: anthropic.Anthropic | None = None,
    ) -> None:
        self._model = model
        self._max_tokens = max_tokens
        self._temperature = temperature
        if client is None:
            client = anthropic.Anthropic(api_key=api_key) if api_key else anthropic.Anthropic()
        self._client = client

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

    def _chat_kwargs(
        self, system_prompt: str, messages: list[dict], tools: list[dict] | None, max_tokens
    ) -> dict[str, Any]:
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
        return kwargs

    def chat(
        self,
        *,
        system_prompt: str,
        messages: list[dict],
        tools: list[dict] | None = None,
        max_tokens: int | None = None,
    ) -> ChatResult:
        """Chat turn via native SDK tool use (v2 §4.8); tool calls are not executed."""
        resp = self._client.messages.create(
            **self._chat_kwargs(system_prompt, messages, tools, max_tokens)
        )
        return ChatResult(
            text=_join_text(resp), tool_calls=_calls_of(resp), usage=_usage_of(resp), raw=resp
        )

    def chat_stream(
        self,
        *,
        system_prompt: str,
        messages: list[dict],
        tools: list[dict] | None = None,
        max_tokens: int | None = None,
    ):
        """Stream the message text via the SDK's ``messages.stream``; tool calls
        (which never stream to the user) are read whole from the final message."""
        kwargs = self._chat_kwargs(system_prompt, messages, tools, max_tokens)
        with self._client.messages.stream(**kwargs) as stream:
            for event in stream:
                if getattr(event, "type", None) == "content_block_delta":
                    delta = getattr(event, "delta", None)
                    if getattr(delta, "type", None) == "text_delta" and delta.text:
                        yield TextDelta(delta.text)
            final = stream.get_final_message()
        yield ChatResult(
            text=_join_text(final), tool_calls=_calls_of(final), usage=_usage_of(final), raw=final
        )
