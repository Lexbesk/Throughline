"""Provider wiring tests (key-free, via injected fake clients)."""

from __future__ import annotations

import pytest

from meeting_notes_todos.config import LLMConfig
from meeting_notes_todos.providers import build_provider
from meeting_notes_todos.providers.anthropic_provider import AnthropicProvider


class _FakeTextBlock:
    type = "text"

    def __init__(self, text: str) -> None:
        self.text = text


class _FakeUsage:
    input_tokens = 11
    output_tokens = 7


class _FakeResponse:
    def __init__(self, text: str) -> None:
        self.content = [_FakeTextBlock(text)]
        self.usage = _FakeUsage()


class _FakeMessages:
    def __init__(self) -> None:
        self.last_kwargs: dict | None = None

    def create(self, **kwargs):
        self.last_kwargs = kwargs
        return _FakeResponse("Hello! The pipeline is wired up.")


class _FakeClient:
    def __init__(self) -> None:
        self.messages = _FakeMessages()


def test_complete_round_trip_returns_text_and_usage():
    client = _FakeClient()
    provider = AnthropicProvider(model="claude-haiku-4-5", client=client)

    result = provider.complete(system_prompt="sys", user_content="hi")

    assert result.text == "Hello! The pipeline is wired up."
    assert result.usage.input_tokens == 11
    assert result.usage.output_tokens == 7
    assert client.messages.last_kwargs["model"] == "claude-haiku-4-5"
    assert client.messages.last_kwargs["system"] == "sys"
    assert client.messages.last_kwargs["messages"] == [{"role": "user", "content": "hi"}]


def test_temperature_is_only_sent_when_set():
    client = _FakeClient()
    AnthropicProvider(model="m", client=client).complete(system_prompt="s", user_content="u")
    assert "temperature" not in client.messages.last_kwargs

    client2 = _FakeClient()
    AnthropicProvider(model="m", temperature=0.3, client=client2).complete(
        system_prompt="s", user_content="u"
    )
    assert client2.messages.last_kwargs["temperature"] == 0.3


def test_factory_builds_anthropic_provider_from_config():
    provider = build_provider(LLMConfig(provider="anthropic", model="claude-haiku-4-5"))
    assert isinstance(provider, AnthropicProvider)


def test_factory_rejects_unknown_provider():
    # a model matching no vendor prefix falls back to the provider field
    with pytest.raises(ValueError):
        build_provider(LLMConfig(provider="nope", model="llama3.1"))


# --- Native structured output (M1) ------------------------------------------


class _FakeParseResponse:
    def __init__(self, parsed) -> None:
        self.parsed_output = parsed
        self.content = [_FakeTextBlock('{"items": []}')]
        self.usage = _FakeUsage()


class _FakeParseMessages:
    def __init__(self) -> None:
        self.last_kwargs: dict | None = None

    def parse(self, **kwargs):
        self.last_kwargs = kwargs
        return _FakeParseResponse(parsed="PARSED")


class _FakeParseClient:
    def __init__(self) -> None:
        self.messages = _FakeParseMessages()


def test_complete_uses_native_structured_output_when_schema_given():
    client = _FakeParseClient()
    provider = AnthropicProvider(model="m", client=client)

    sentinel_schema = object
    result = provider.complete(
        system_prompt="s", user_content="u", response_schema=sentinel_schema
    )

    assert result.parsed == "PARSED"
    assert client.messages.last_kwargs["output_format"] is sentinel_schema
    assert client.messages.last_kwargs["model"] == "m"


# --- chat() with tool use (v2 M8) --------------------------------------------


class _FakeToolUseBlock:
    type = "tool_use"

    def __init__(self, block_id: str, name: str, input: dict) -> None:
        self.id = block_id
        self.name = name
        self.input = input


def test_chat_parses_text_and_tool_use_blocks():
    client = _FakeClient()
    resp = _FakeResponse("These two are the same task.")
    resp.content.append(_FakeToolUseBlock("t1", "propose_merge", {"keep_id": "a", "absorb_id": "b"}))
    client.messages.create = lambda **kwargs: (
        setattr(client.messages, "last_kwargs", kwargs) or resp
    )
    provider = AnthropicProvider(model="m", client=client)

    tools = [{"name": "propose_merge", "description": "d", "input_schema": {"type": "object"}}]
    history = [{"role": "user", "content": "merge them"}]
    result = provider.chat(system_prompt="s", messages=history, tools=tools)

    assert result.text == "These two are the same task."
    assert [(c.id, c.name, c.input) for c in result.tool_calls] == [
        ("t1", "propose_merge", {"keep_id": "a", "absorb_id": "b"})
    ]
    assert result.usage.input_tokens == 11 and result.usage.output_tokens == 7
    assert client.messages.last_kwargs["tools"] is tools
    assert client.messages.last_kwargs["messages"] is history
    assert client.messages.last_kwargs["system"] == "s"
