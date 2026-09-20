"""Streaming path tests (provider chat_stream + the SSE chat endpoint).

Key-free: fake streaming clients for Anthropic and OpenAI, and a fake provider
whose chat_stream yields several text deltas then a ChatResult with tool calls.
Verifies: text streams incrementally, tool calls arrive whole at the end, and
the SSE endpoint emits text events followed by a single done event whose
proposals are routed correctly.
"""

from __future__ import annotations

import json

from fastapi.testclient import TestClient

import pytest

from meeting_notes_todos.chat import run_chat_turn_stream
from meeting_notes_todos.config import PromptsConfig
from meeting_notes_todos.providers import (
    AnthropicProvider,
    ChatResult,
    LLMProvider,
    OpenAIProvider,
    TextDelta,
    ToolCall,
    Usage,
)
from meeting_notes_todos.store import MarkdownStore
from meeting_notes_todos.web.app import app, get_provider, get_store


# --- Anthropic streaming: iterate events for text, final message for tools -----


class _AnthTextDelta:
    type = "content_block_delta"

    def __init__(self, text):
        self.delta = type("D", (), {"type": "text_delta", "text": text})()


class _AnthFinalBlockText:
    type = "text"

    def __init__(self, text):
        self.text = text


class _AnthFinalBlockTool:
    type = "tool_use"

    def __init__(self, id, name, input):
        self.id, self.name, self.input = id, name, input


class _AnthFinalMessage:
    def __init__(self, content):
        self.content = content
        self.usage = type("U", (), {"input_tokens": 9, "output_tokens": 4})()


class _AnthStreamCtx:
    def __init__(self, events, final):
        self._events, self._final = events, final

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def __iter__(self):
        return iter(self._events)

    def get_final_message(self):
        return self._final


class _AnthMessages:
    def __init__(self, events, final):
        self._events, self._final = events, final
        self.last_kwargs = None

    def stream(self, **kwargs):
        self.last_kwargs = kwargs
        return _AnthStreamCtx(self._events, self._final)


class _AnthClient:
    def __init__(self, events, final):
        self.messages = _AnthMessages(events, final)


def test_anthropic_chat_stream_yields_text_then_result():
    events = [_AnthTextDelta("Hello"), _AnthTextDelta(" there")]
    final = _AnthFinalMessage([
        _AnthFinalBlockText("Hello there"),
        _AnthFinalBlockTool("t1", "propose_complete", {"id": "a"}),
    ])
    provider = AnthropicProvider(model="m", client=_AnthClient(events, final))

    out = list(provider.chat_stream(system_prompt="s", messages=[{"role": "user", "content": "hi"}],
                                    tools=[{"name": "propose_complete"}]))
    deltas = [e.text for e in out if isinstance(e, TextDelta)]
    result = out[-1]
    assert deltas == ["Hello", " there"]  # streamed incrementally
    assert isinstance(result, ChatResult)
    assert result.text == "Hello there"
    assert [(c.name, c.input) for c in result.tool_calls] == [("propose_complete", {"id": "a"})]
    assert result.usage.input_tokens == 9


# --- OpenAI streaming: content deltas + fragmented tool-call arguments ----------


def _oai_chunk(content=None, tool_frags=None, usage=None):
    delta = type("Delta", (), {"content": content, "tool_calls": tool_frags})()
    choice = type("Choice", (), {"delta": delta})()
    return type("Chunk", (), {"choices": [] if usage else [choice], "usage": usage})()


def _oai_tool_frag(index, id=None, name=None, arguments=None):
    fn = type("Fn", (), {"name": name, "arguments": arguments})() if (name or arguments) else None
    return type("TC", (), {"index": index, "id": id, "function": fn})()


class _OaiCompletions:
    def __init__(self, chunks):
        self._chunks = chunks
        self.last_kwargs = None

    def create(self, **kwargs):
        self.last_kwargs = kwargs
        return iter(self._chunks)


class _OaiClient:
    def __init__(self, chunks):
        self.chat = type("Chat", (), {"completions": _OaiCompletions(chunks)})()


def test_openai_chat_stream_reassembles_fragmented_tool_calls():
    usage = type("U", (), {"prompt_tokens": 12, "completion_tokens": 8})()
    chunks = [
        _oai_chunk(content="Those "),
        _oai_chunk(content="two match."),
        _oai_chunk(tool_frags=[_oai_tool_frag(0, id="c1", name="propose_merge")]),
        _oai_chunk(tool_frags=[_oai_tool_frag(0, arguments='{"keep_id":"a",')]),
        _oai_chunk(tool_frags=[_oai_tool_frag(0, arguments='"absorb_id":"b"}')]),
        _oai_chunk(usage=usage),  # final usage-only chunk
    ]
    provider = OpenAIProvider(model="gpt-4.1-mini", client=_OaiClient(chunks))

    out = list(provider.chat_stream(system_prompt="s", messages=[{"role": "user", "content": "hi"}],
                                    tools=[{"name": "propose_merge"}]))
    deltas = [e.text for e in out if isinstance(e, TextDelta)]
    result = out[-1]
    assert deltas == ["Those ", "two match."]
    assert result.text == "Those two match."
    # the arguments streamed in three fragments and were reassembled into one call
    assert [(c.name, c.input) for c in result.tool_calls] == [
        ("propose_merge", {"keep_id": "a", "absorb_id": "b"})
    ]
    assert result.usage.output_tokens == 8
    assert provider._client.chat.completions.last_kwargs["stream"] is True


# --- the SSE endpoint: text events, then one done event with routed proposals ---


class _FakeStreamProvider(LLMProvider):
    """chat_stream yields several deltas then a ChatResult with tool calls."""

    def complete(self, *, system_prompt, user_content, max_tokens=None, response_schema=None):
        raise AssertionError("streaming path must not call complete()")

    def chat_stream(self, *, system_prompt, messages, tools=None, max_tokens=None):
        for chunk in ["Let ", "me ", "think… "]:
            yield TextDelta(chunk)
        yield ChatResult(
            text="Let me think… here's a thought.",
            tool_calls=[
                ToolCall(id="t1", name="propose_new", input={"title": "Book the venue"}),
                ToolCall(id="t2", name="propose_profile_update",
                         input={"section": "Current focus", "new_text": "Planning the event."}),
            ],
            usage=Usage(20, 9),
        )


def _parse_sse(raw: str) -> list[dict]:
    events = []
    for frame in raw.split("\n\n"):
        line = next((l for l in frame.splitlines() if l.startswith("data:")), None)
        if line:
            events.append(json.loads(line[5:].strip()))
    return events


def test_chat_stream_endpoint_streams_text_then_routed_proposals(tmp_path):
    store = MarkdownStore(tmp_path / "todos.md")
    app.dependency_overrides[get_store] = lambda: store
    app.dependency_overrides[get_provider] = lambda: _FakeStreamProvider()
    client = TestClient(app)
    try:
        resp = client.post("/api/chat/stream", json={"messages": [
            {"role": "user", "content": "help me plan an event"}]})
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        events = _parse_sse(resp.text)

        texts = [e["text"] for e in events if e["type"] == "text"]
        assert texts == ["Let ", "me ", "think… "]  # streamed as separate events

        done = [e for e in events if e["type"] == "done"]
        assert len(done) == 1  # exactly one terminal event
        d = done[0]
        assert d["message"] == "Let me think… here's a thought."
        # proposals arrive whole in the done event, routed to their targets
        assert [(p["op"], p["target"]) for p in d["proposals"]] == [
            ("new", "todo"), ("profile", "profile")
        ]
        assert d["usage"] == {"input_tokens": 20, "output_tokens": 9}
        # the text events all precede the done event (cards never render mid-stream)
        assert [e["type"] for e in events].index("done") == len(texts)
    finally:
        app.dependency_overrides.clear()


def test_chat_stream_endpoint_validates_empty_message(tmp_path):
    store = MarkdownStore(tmp_path / "todos.md")
    app.dependency_overrides[get_store] = lambda: store
    app.dependency_overrides[get_provider] = lambda: _FakeStreamProvider()
    try:
        assert TestClient(app).post("/api/chat/stream", json={"messages": []}).status_code == 400
    finally:
        app.dependency_overrides.clear()


# --- resilience: transparent retry when the stream drops before any text -------

PROMPTS = PromptsConfig()  # reads prompts/ from the repo root (pytest cwd)
_MSGS = [{"role": "user", "content": "hi"}]


class _FlakyThenOkProvider(LLMProvider):
    """Fails before any text on the first attempt, then succeeds — the common
    'dropped connection at the connect/first-token stage' on a flaky network."""

    def __init__(self):
        self.calls = 0

    def complete(self, *, system_prompt, user_content, max_tokens=None, response_schema=None):
        raise AssertionError

    def chat_stream(self, *, system_prompt, messages, tools=None, max_tokens=None):
        self.calls += 1
        if self.calls == 1:
            raise TimeoutError("dropped connection before first token")
        yield TextDelta("Hi ")
        yield TextDelta("there")
        yield ChatResult(text="Hi there", tool_calls=[
            ToolCall(id="t1", name="propose_new", input={"title": "Say hi back"})], usage=Usage(3, 2))


def test_stream_retries_transparently_before_first_token():
    p = _FlakyThenOkProvider()
    events = list(run_chat_turn_stream(provider=p, prompts=PROMPTS, items=[], messages=_MSGS))
    assert p.calls == 2  # retried once, invisibly
    assert [e[1] for e in events if e[0] == "text"] == ["Hi ", "there"]
    done = next(e[1] for e in events if e[0] == "done")
    assert done.message == "Hi there"
    assert [(x["op"], x["target"]) for x in done.proposals] == [("new", "todo")]


class _FailMidStreamProvider(LLMProvider):
    """Yields some text, then drops — can't be retried without duplicating output."""

    def __init__(self):
        self.calls = 0

    def complete(self, *, system_prompt, user_content, max_tokens=None, response_schema=None):
        raise AssertionError

    def chat_stream(self, *, system_prompt, messages, tools=None, max_tokens=None):
        self.calls += 1
        yield TextDelta("partial…")
        raise TimeoutError("dropped mid-message")


def test_stream_does_not_retry_once_text_has_started():
    p = _FailMidStreamProvider()
    got = []
    with pytest.raises(Exception):
        for ev in run_chat_turn_stream(provider=p, prompts=PROMPTS, items=[], messages=_MSGS):
            got.append(ev)
    assert p.calls == 1  # no retry once the user has seen text
    assert [e[1] for e in got if e[0] == "text"] == ["partial…"]
