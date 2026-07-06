"""OpenAI GPT provider — single-user, local, behind the same LLMProvider interface.

Reuses the LocalProvider's OpenAI-compatible client path (M5): the wire dialect
is identical, so text completion and tool calling are inherited. What differs:

- endpoint + auth: api.openai.com with ``OPENAI_API_KEY`` from the environment
  (via ``.env``, exactly like ``ANTHROPIC_API_KEY``);
- token limit: current GPT models take ``max_completion_tokens`` (``max_tokens``
  is the deprecated legacy name that local endpoints still expect);
- structured output: unlike local endpoints, OpenAI has a native strict-schema
  mode — ``chat.completions.parse`` (GA in openai>=2) takes the pydantic class
  and returns a validated instance, mirroring the Anthropic provider's
  ``messages.parse`` path. The pipeline's parse→validate→one-repair-retry loop
  stays in place as the safety net.

Verified against the installed SDK (openai 2.44.0): ``chat.completions.parse``
is GA; usage fields are ``prompt_tokens``/``completion_tokens``; tools use the
``{"type": "function", "function": {...}}`` shape with JSON-string arguments.
"""

from __future__ import annotations

import os
from typing import Any

from .base import CompletionResult
from .local_provider import LocalProvider, _usage_of


class OpenAIProvider(LocalProvider):
    _max_tokens_param = "max_completion_tokens"  # current API name (max_tokens is legacy)

    def __init__(
        self,
        model: str,
        *,
        max_tokens: int = 1024,
        temperature: float | None = None,
        client: Any = None,
    ) -> None:
        if client is None:  # lazy import, as in LocalProvider
            from openai import OpenAI

            # placeholder avoids a constructor-time error when the key is absent;
            # a request without a real key fails with OpenAI's own 401 message
            client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY") or "not-set")
        super().__init__(
            model=model,
            base_url="https://api.openai.com/v1",
            max_tokens=max_tokens,
            temperature=temperature,
            client=client,
        )

    def complete(
        self,
        *,
        system_prompt: str,
        user_content: str,
        max_tokens: int | None = None,
        response_schema: Any = None,
    ) -> CompletionResult:
        # Native structured output when a schema is requested (mirrors the
        # Anthropic provider): the SDK converts the pydantic model to a strict
        # JSON schema and validates the response into ``message.parsed``.
        if response_schema is not None:
            kwargs: dict[str, Any] = {
                "model": self._model,
                self._max_tokens_param: max_tokens or self._max_tokens,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_content},
                ],
                "response_format": response_schema,
            }
            if self._temperature is not None:
                kwargs["temperature"] = self._temperature
            resp = self._client.chat.completions.parse(**kwargs)
            message = resp.choices[0].message
            return CompletionResult(
                text=message.content or "",
                usage=_usage_of(resp),
                raw=resp,
                parsed=getattr(message, "parsed", None),
            )
        return super().complete(
            system_prompt=system_prompt, user_content=user_content, max_tokens=max_tokens
        )
