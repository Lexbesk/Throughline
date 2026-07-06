"""OpenAIProvider tests (key-free; injected fake client, mirrors the Anthropic
provider tests) and the centralized model-string routing."""

from __future__ import annotations

import pytest

from meeting_notes_todos.config import LLMConfig
from meeting_notes_todos.providers import (
    AnthropicProvider,
    LocalProvider,
    OpenAIProvider,
    build_provider,
    resolve_provider_name,
)

# --- fake OpenAI client (chat.completions.create + .parse) ---------------------


class _FakeUsage:
    prompt_tokens = 12
    completion_tokens = 8


class _FakeMessage:
    def __init__(self, content, parsed=None):
        self.content = content
        self.parsed = parsed


class _FakeChoice:
    def __init__(self, message):
        self.message = message


class _FakeResponse:
    def __init__(self, content, parsed=None):
        self.choices = [_FakeChoice(_FakeMessage(content, parsed))]
        self.usage = _FakeUsage()


class _FakeCompletions:
    def __init__(self, parsed_sentinel=None):
        self.last_create_kwargs = None
        self.last_parse_kwargs = None
        self._parsed = parsed_sentinel

    def create(self, **kwargs):
        self.last_create_kwargs = kwargs
        return _FakeResponse("A plain text reply.")

    def parse(self, **kwargs):
        self.last_parse_kwargs = kwargs
        return _FakeResponse('{"items": []}', parsed=self._parsed)


class _FakeOpenAI:
    def __init__(self, parsed_sentinel=None):
        self.chat = type("Chat", (), {})()
        self.chat.completions = _FakeCompletions(parsed_sentinel)


# --- routing: the model string picks the provider ------------------------------


def test_model_string_routes_to_the_right_vendor():
    assert resolve_provider_name(LLMConfig(model="claude-haiku-4-5")) == "anthropic"
    assert resolve_provider_name(LLMConfig(model="gpt-4.1-mini")) == "openai"
    # the M14 switch scenario: provider field still says anthropic, model is GPT
    assert resolve_provider_name(LLMConfig(provider="anthropic", model="gpt-5.1")) == "openai"
    # explicit local always wins — e.g. Ollama serving a gpt-oss model
    assert resolve_provider_name(
        LLMConfig(provider="local", model="gpt-oss-20b", base_url="http://localhost:11434/v1")
    ) == "local"


def test_build_provider_routes_by_model_string():
    assert isinstance(build_provider(LLMConfig(model="claude-haiku-4-5")), AnthropicProvider)
    gpt = build_provider(LLMConfig(model="gpt-4.1-mini"))
    assert isinstance(gpt, OpenAIProvider)
    local = build_provider(
        LLMConfig(provider="local", model="gpt-oss-20b", base_url="http://x/v1")
    )
    assert isinstance(local, LocalProvider) and not isinstance(local, OpenAIProvider)


def test_unroutable_config_still_raises():
    with pytest.raises(ValueError):
        build_provider(LLMConfig(provider="nope", model="llama3.1"))


# --- PHASE 1: plain text completion ---------------------------------------------


def test_complete_returns_text_and_maps_usage():
    client = _FakeOpenAI()
    provider = OpenAIProvider(model="gpt-4.1-mini", client=client)

    result = provider.complete(system_prompt="sys", user_content="hi")

    assert result.text == "A plain text reply."
    assert result.usage.input_tokens == 12 and result.usage.output_tokens == 8
    kwargs = client.chat.completions.last_create_kwargs
    assert kwargs["model"] == "gpt-4.1-mini"
    # current API: max_completion_tokens, not the deprecated max_tokens
    assert kwargs["max_completion_tokens"] == 1024 and "max_tokens" not in kwargs
    assert kwargs["messages"] == [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "hi"},
    ]


# --- PHASE 2: native structured output -------------------------------------------


def test_response_schema_uses_native_parse():
    sentinel = object()
    client = _FakeOpenAI(parsed_sentinel=sentinel)
    provider = OpenAIProvider(model="gpt-4.1-mini", client=client)

    schema = object  # any class; the SDK receives it as response_format
    result = provider.complete(system_prompt="s", user_content="u", response_schema=schema)

    assert result.parsed is sentinel  # validated instance flows to the pipeline
    kwargs = client.chat.completions.last_parse_kwargs
    assert kwargs["response_format"] is schema
    assert kwargs["max_completion_tokens"] == 1024
    assert client.chat.completions.last_create_kwargs is None  # create() not used


# --- PHASE 3: tool calling through the inherited translation ---------------------


class _FakeFunction:
    def __init__(self, name, arguments):
        self.name = name
        self.arguments = arguments


class _FakeToolCall:
    def __init__(self, tc_id, name, arguments):
        self.id = tc_id
        self.function = _FakeFunction(name, arguments)


def test_chat_translates_tools_and_history_and_uses_current_token_param():
    client = _FakeOpenAI()
    resp = _FakeResponse("Those two are the same task.")
    resp.choices[0].message.tool_calls = [
        _FakeToolCall("c1", "propose_merge", '{"keep_id": "a", "absorb_id": "b"}'),
        _FakeToolCall("c2", "propose_complete", '{"id": "a"}'),  # parallel calls
    ]
    client.chat.completions.create = lambda **kwargs: (
        setattr(client.chat.completions, "last_create_kwargs", kwargs) or resp
    )
    provider = OpenAIProvider(model="gpt-5.1", client=client)

    history = [
        {"role": "user", "content": "merge the Q3 items"},
        {"role": "assistant", "content": "I proposed a merge for your approval."},
        {"role": "user", "content": "and mark the cert done"},
    ]
    tools = [{"name": "propose_merge", "description": "d",
              "input_schema": {"type": "object", "properties": {"keep_id": {"type": "string"}}}}]
    result = provider.chat(system_prompt="sys", messages=history, tools=tools)

    # (a) internal defs -> OpenAI function schema
    sent = client.chat.completions.last_create_kwargs
    assert sent["tools"][0] == {"type": "function", "function": {
        "name": "propose_merge", "description": "d",
        "parameters": {"type": "object", "properties": {"keep_id": {"type": "string"}}}}}
    # history is prose-only by design; provider prepends the system prompt
    assert sent["messages"] == [{"role": "system", "content": "sys"}, *history]
    assert sent["max_completion_tokens"] == 1024
    # (b) OpenAI response -> internal text + ToolCalls (JSON-string args, multiple)
    assert result.text == "Those two are the same task."
    assert [(c.name, c.input) for c in result.tool_calls] == [
        ("propose_merge", {"keep_id": "a", "absorb_id": "b"}),
        ("propose_complete", {"id": "a"}),
    ]
