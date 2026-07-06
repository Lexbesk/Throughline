"""Local / OpenAI-compatible provider (build plan §4.1).

Talks to any OpenAI-compatible chat-completions endpoint — a local Ollama or vLLM
server, or the OpenAI API. It has no native structured-output mode, so it prompts
for JSON (the prompt files already require it) and returns text; the pipeline parses
and repairs. Selected by config (``provider = "local"``), so switching is config-only.

For the v2 chat assistant, ``chat()`` maps our Anthropic-shaped tool definitions
onto OpenAI-compatible function calling (v2 §4.8). If a local model's tool calling
is too unreliable, the documented fallback (v2 §4.4) is a single structured
response — one JSON object ``{"message": ..., "proposals": [...]}`` parsed with
the same repair discipline as extraction; the chat module is the one place that
would change.
"""

from __future__ import annotations

import json
from typing import Any

from .base import ChatResult, CompletionResult, LLMProvider, ToolCall, Usage


class LocalProvider(LLMProvider):
    # Local/compatible endpoints expect the classic parameter name; OpenAI proper
    # overrides this with "max_completion_tokens" (the current API's name).
    _max_tokens_param = "max_tokens"

    def __init__(
        self,
        model: str,
        *,
        base_url: str,
        api_key: str = "not-needed",
        max_tokens: int = 1024,
        temperature: float | None = None,
        client: Any = None,
    ) -> None:
        self._model = model
        self._max_tokens = max_tokens
        self._temperature = temperature
        if client is not None:
            self._client = client
        else:  # lazy import so the OpenAI SDK is only needed when this provider is used
            from openai import OpenAI

            self._client = OpenAI(base_url=base_url, api_key=api_key)

    def complete(
        self,
        *,
        system_prompt: str,
        user_content: str,
        max_tokens: int | None = None,
        response_schema: Any = None,
    ) -> CompletionResult:
        # response_schema is intentionally not used for native structured output:
        # local endpoints prompt for JSON and the pipeline parses/repairs (§4.1).
        kwargs: dict[str, Any] = {
            "model": self._model,
            self._max_tokens_param: max_tokens or self._max_tokens,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
        }
        if self._temperature is not None:
            kwargs["temperature"] = self._temperature

        resp = self._client.chat.completions.create(**kwargs)
        text = resp.choices[0].message.content or ""
        return CompletionResult(text=text, usage=_usage_of(resp), raw=resp, parsed=None)

    def chat(
        self,
        *,
        system_prompt: str,
        messages: list[dict],
        tools: list[dict] | None = None,
        max_tokens: int | None = None,
    ) -> ChatResult:
        """Chat turn via OpenAI-compatible function calling (v2 §4.8).

        History translation: the app's internal history is plain prose turns
        ({"role": "user"|"assistant", "content": str}) — by advisory-first design
        it never contains executed tool calls or tool results — so the mapping is
        role-preserving with the system prompt prepended. Tool calls exist only
        in responses, parsed below into internal ToolCall objects.
        """
        kwargs: dict[str, Any] = {
            "model": self._model,
            self._max_tokens_param: max_tokens or self._max_tokens,
            "messages": [{"role": "system", "content": system_prompt}, *messages],
        }
        if tools:
            kwargs["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": tool["name"],
                        "description": tool.get("description", ""),
                        "parameters": tool.get("input_schema", {"type": "object"}),
                    },
                }
                for tool in tools
            ]
        if self._temperature is not None:
            kwargs["temperature"] = self._temperature

        resp = self._client.chat.completions.create(**kwargs)
        message = resp.choices[0].message
        calls: list[ToolCall] = []
        for tc in getattr(message, "tool_calls", None) or []:
            fn = getattr(tc, "function", None)
            if fn is None:
                continue
            try:
                args = json.loads(fn.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            calls.append(ToolCall(id=getattr(tc, "id", "") or "", name=fn.name, input=args))
        return ChatResult(
            text=message.content or "", tool_calls=calls, usage=_usage_of(resp), raw=resp
        )


def _usage_of(resp: Any) -> Usage:
    usage = getattr(resp, "usage", None)
    return Usage(
        input_tokens=getattr(usage, "prompt_tokens", 0) or 0,
        output_tokens=getattr(usage, "completion_tokens", 0) or 0,
    )
