"""LocalProvider tests (key-free; config-only switch + a fake OpenAI client)."""

from __future__ import annotations

from meeting_notes_todos.config import LLMConfig
from meeting_notes_todos.providers import LocalProvider, build_provider


class _FakeUsage:
    prompt_tokens = 12
    completion_tokens = 8


class _FakeMessage:
    def __init__(self, content: str) -> None:
        self.content = content


class _FakeChoice:
    def __init__(self, content: str) -> None:
        self.message = _FakeMessage(content)


class _FakeResponse:
    def __init__(self, content: str) -> None:
        self.choices = [_FakeChoice(content)]
        self.usage = _FakeUsage()


class _FakeCompletions:
    def __init__(self) -> None:
        self.last_kwargs: dict | None = None

    def create(self, **kwargs):
        self.last_kwargs = kwargs
        return _FakeResponse('{"items": []}')


class _FakeChat:
    def __init__(self) -> None:
        self.completions = _FakeCompletions()


class _FakeOpenAI:
    def __init__(self) -> None:
        self.chat = _FakeChat()


def test_local_complete_returns_text_and_usage():
    client = _FakeOpenAI()
    provider = LocalProvider(model="llama3.1", base_url="http://x/v1", client=client)

    result = provider.complete(system_prompt="sys", user_content="hi")

    assert result.text == '{"items": []}'
    assert result.parsed is None  # no native structured output — pipeline parses text
    assert result.usage.input_tokens == 12 and result.usage.output_tokens == 8
    kwargs = client.chat.completions.last_kwargs
    assert kwargs["model"] == "llama3.1"
    assert kwargs["messages"] == [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "hi"},
    ]


def test_switching_provider_to_local_is_config_only():
    provider = build_provider(
        LLMConfig(provider="local", model="llama3.1", base_url="http://localhost:11434/v1")
    )
    assert isinstance(provider, LocalProvider)


def test_openai_provider_is_the_openai_compatible_subclass():
    from meeting_notes_todos.providers import OpenAIProvider

    provider = build_provider(LLMConfig(provider="openai", model="gpt-4o-mini"))
    assert isinstance(provider, OpenAIProvider)  # same client path, OpenAI defaults
    assert isinstance(provider, LocalProvider)


# --- chat() via OpenAI-compatible function calling (v2 M8) --------------------


class _FakeFunction:
    def __init__(self, name: str, arguments: str) -> None:
        self.name = name
        self.arguments = arguments


class _FakeToolCall:
    def __init__(self, tc_id: str, name: str, arguments: str) -> None:
        self.id = tc_id
        self.function = _FakeFunction(name, arguments)


def test_local_chat_maps_tools_and_parses_function_calls():
    client = _FakeOpenAI()
    resp = _FakeResponse("On it.")
    resp.choices[0].message.tool_calls = [
        _FakeToolCall("c1", "propose_complete", '{"id": "a"}'),
        _FakeToolCall("c2", "propose_delete", "{not json"),  # malformed args -> {}
    ]
    client.chat.completions.create = lambda **kwargs: (
        setattr(client.chat.completions, "last_kwargs", kwargs) or resp
    )
    provider = LocalProvider(model="llama3.1", base_url="http://x/v1", client=client)

    tools = [{"name": "propose_complete", "description": "d",
              "input_schema": {"type": "object", "properties": {"id": {"type": "string"}}}}]
    result = provider.chat(
        system_prompt="sys", messages=[{"role": "user", "content": "done with a"}], tools=tools
    )

    assert result.text == "On it."
    assert [(c.name, c.input) for c in result.tool_calls] == [
        ("propose_complete", {"id": "a"}),
        ("propose_delete", {}),
    ]
    sent = client.chat.completions.last_kwargs["tools"][0]
    assert sent["type"] == "function"
    assert sent["function"]["name"] == "propose_complete"
    assert sent["function"]["parameters"]["properties"]["id"] == {"type": "string"}
    assert client.chat.completions.last_kwargs["messages"][0] == {
        "role": "system", "content": "sys"
    }
