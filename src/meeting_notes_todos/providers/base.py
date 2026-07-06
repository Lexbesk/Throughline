"""The single internal LLM interface every pipeline stage calls (build plan §4.1).

Conceptually ``complete(system_prompt, user_content, response_schema?) -> result``.
Extraction, reconcile, and merge only ever call this interface, so swapping the
model (API <-> local) is a config edit, not a code change.

v2 (M8) adds a sibling ``chat()`` for the assistant: it takes a message history
plus tool definitions and returns a response that may contain both text and
tool-call blocks (v2 plan §4.8). In advisory-first mode the tools are never
executed by the provider or the app — each tool call is parsed into a *staged
proposal* for the user to approve.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any


@dataclass
class Usage:
    """Token usage for one completion (build plan §4.4: log token usage per run)."""

    input_tokens: int = 0
    output_tokens: int = 0

    def __add__(self, other: "Usage") -> "Usage":
        return Usage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
        )


@dataclass
class CompletionResult:
    text: str
    usage: Usage
    raw: Any = None  # provider-native response object, kept for debugging
    parsed: Any = None  # validated object when the provider used native structured output


@dataclass
class ToolCall:
    """One tool invocation the model requested (staged as a proposal, not executed)."""

    id: str
    name: str
    input: dict


@dataclass
class ChatResult:
    """One chat turn: the model's prose plus any tool calls it emitted (v2 §4.8)."""

    text: str  # the conversational message (and the model's stated reasoning)
    tool_calls: list[ToolCall]
    usage: Usage
    raw: Any = None


class LLMProvider(ABC):
    """Base class for provider implementations (Anthropic now; Local/OpenAI later)."""

    @abstractmethod
    def complete(
        self,
        *,
        system_prompt: str,
        user_content: str,
        max_tokens: int | None = None,
        response_schema: Any = None,
    ) -> CompletionResult:
        """Return a completion for the given system + user content.

        When ``response_schema`` (a pydantic model class) is given, providers that
        support native structured output use it and populate
        ``CompletionResult.parsed`` with the validated instance; others ignore it
        and return text for the caller to parse. ``text`` always holds the raw
        model output.
        """
        raise NotImplementedError

    def chat(
        self,
        *,
        system_prompt: str,
        messages: list[dict],
        tools: list[dict] | None = None,
        max_tokens: int | None = None,
    ) -> ChatResult:
        """Multi-turn chat with optional tool definitions (v2 §4.8).

        ``messages`` is a plain ``[{"role": "user"|"assistant", "content": str}]``
        history ending with the new user message. ``tools`` uses the Anthropic
        shape (``name`` / ``description`` / ``input_schema``); other providers
        translate it. Not abstract so v1-only providers keep working — the chat
        endpoint surfaces this error as "provider does not support chat".
        """
        raise NotImplementedError("this provider does not support chat with tools")
