"""BedrockProvider tests (key-free; injected fake boto3 client, mirroring the
Anthropic/OpenAI provider tests), plus the AWS credential codec that lets the
single-string per-user key store hold a three-part AWS credential."""

from __future__ import annotations

import pytest
from botocore.exceptions import ClientError

from meeting_notes_todos.config import LLMConfig
from meeting_notes_todos.providers import (
    BedrockProvider,
    LocalProvider,
    build_provider,
    pack_aws_credentials,
    parse_aws_credentials,
    resolve_provider_name,
)

NOVA = "us.amazon.nova-2-lite-v1:0"

# --- fake bedrock-runtime client (converse + converse_stream) ------------------


def _response(text, tool_uses=(), tokens=(12, 8)):
    content = [{"text": text}] if text else []
    content += [
        {"toolUse": {"toolUseId": tid, "name": name, "input": args}}
        for tid, name, args in tool_uses
    ]
    return {
        "output": {"message": {"role": "assistant", "content": content}},
        "usage": {"inputTokens": tokens[0], "outputTokens": tokens[1],
                  "totalTokens": sum(tokens)},
        "stopReason": "end_turn",
    }


class _FakeBedrock:
    def __init__(self, response=None, stream=None, error=None):
        self.last_kwargs = None
        self.last_stream_kwargs = None
        self._response = response if response is not None else _response("A plain text reply.")
        self._stream = stream or []
        self._error = error

    def converse(self, **kwargs):
        self.last_kwargs = kwargs
        if self._error:
            raise self._error
        return self._response

    def converse_stream(self, **kwargs):
        self.last_stream_kwargs = kwargs
        if self._error:
            raise self._error
        return {"stream": iter(self._stream)}


def _client_error(code, message="boom"):
    return ClientError({"Error": {"Code": code, "Message": message}}, "Converse")


# --- routing: the model string picks the vendor --------------------------------


def test_bedrock_model_strings_route_to_bedrock():
    assert resolve_provider_name(LLMConfig(model=NOVA)) == "bedrock"
    assert resolve_provider_name(LLMConfig(model="amazon.nova-lite-v1:0")) == "bedrock"
    # the M14 switch scenario: provider field still says anthropic, model is Nova
    assert resolve_provider_name(LLMConfig(provider="anthropic", model=NOVA)) == "bedrock"
    # explicit local still wins, as for every other vendor prefix
    assert resolve_provider_name(
        LLMConfig(provider="local", model="amazon.nova-lite-v1:0", base_url="http://x/v1")
    ) == "local"
    # the Claude models Bedrock also hosts are NOT routed here: this app reaches
    # Claude through the Anthropic SDK
    assert resolve_provider_name(LLMConfig(model="claude-haiku-4-5")) == "anthropic"


def test_build_provider_builds_bedrock_from_the_model_string():
    provider = build_provider(LLMConfig(model=NOVA))
    assert isinstance(provider, BedrockProvider)
    assert not isinstance(provider, LocalProvider)


# --- credential codec: three AWS values in one stored string -------------------


def test_credentials_round_trip_through_a_single_string():
    blob = pack_aws_credentials("AKIAEXAMPLE1234", "secret-value", "us-east-2")
    assert parse_aws_credentials(blob) == {
        "access_key_id": "AKIAEXAMPLE1234",
        "secret_access_key": "secret-value",
        "region": "us-east-2",
    }


def test_blank_region_defaults_and_missing_fields_are_rejected():
    assert parse_aws_credentials(
        pack_aws_credentials("AKIA1", "s", "  ")
    )["region"] == "us-east-2"
    with pytest.raises(ValueError):
        pack_aws_credentials("", "secret", "us-east-2")
    with pytest.raises(ValueError):
        pack_aws_credentials("AKIA1", "", "us-east-2")


def test_malformed_stored_credentials_raise_without_quoting_the_blob():
    for blob in ('{"access_key_id": "AKIA1"}', "not json at all", '["a", "b"]'):
        with pytest.raises(ValueError) as exc:
            parse_aws_credentials(blob)
        assert blob not in str(exc.value)  # a live secret never reaches the message


def test_build_provider_passes_stored_credentials_to_the_client():
    blob = pack_aws_credentials("AKIAUSERA", "secret-A", "us-west-2")
    provider = build_provider(LLMConfig(model=NOVA), api_key=blob)
    client = provider._client
    assert client.meta.region_name == "us-west-2"  # the user's own region, not the config's
    # reaching into the signer proves the credentials got there (the Anthropic and
    # OpenAI key tests assert the same thing via client.api_key)
    assert client._request_signer._credentials.access_key == "AKIAUSERA"
    assert client._request_signer._credentials.secret_key == "secret-A"


def test_no_stored_credentials_falls_back_to_the_default_chain(monkeypatch):
    monkeypatch.setenv("AWS_REGION", "eu-central-1")
    provider = build_provider(LLMConfig(model=NOVA))  # local single-user mode
    assert provider._client.meta.region_name == "eu-central-1"
    # an explicit config region wins over the environment
    assert build_provider(
        LLMConfig(model=NOVA, region="us-east-2")
    )._client.meta.region_name == "us-east-2"


# --- PHASE 1: plain text completion --------------------------------------------


def test_complete_returns_text_and_maps_usage():
    client = _FakeBedrock()
    provider = BedrockProvider(model=NOVA, client=client)

    result = provider.complete(system_prompt="sys", user_content="hi")

    assert result.text == "A plain text reply."
    assert result.usage.input_tokens == 12 and result.usage.output_tokens == 8
    kwargs = client.last_kwargs
    assert kwargs["modelId"] == NOVA
    assert kwargs["system"] == [{"text": "sys"}]  # a separate param, not a message
    assert kwargs["messages"] == [{"role": "user", "content": [{"text": "hi"}]}]
    assert kwargs["inferenceConfig"] == {"maxTokens": 1024}


def test_max_tokens_and_temperature_flow_into_inference_config():
    client = _FakeBedrock()
    BedrockProvider(model=NOVA, client=client).complete(
        system_prompt="s", user_content="u", max_tokens=256
    )
    assert client.last_kwargs["inferenceConfig"] == {"maxTokens": 256}  # per-call override

    client2 = _FakeBedrock()
    BedrockProvider(model=NOVA, max_tokens=4096, temperature=0.3, client=client2).complete(
        system_prompt="s", user_content="u"
    )
    assert client2.last_kwargs["inferenceConfig"] == {"maxTokens": 4096, "temperature": 0.3}


def test_response_schema_is_ignored_so_the_pipeline_parses_the_text():
    # Converse has no native structured output (as with LocalProvider): the text
    # comes back for the pipeline's parse/repair loop.
    client = _FakeBedrock(response=_response('{"items": []}'))
    provider = BedrockProvider(model=NOVA, client=client)

    result = provider.complete(system_prompt="s", user_content="u", response_schema=object)

    assert result.text == '{"items": []}'
    assert result.parsed is None


# --- PHASE 2: tool calling through toolConfig ----------------------------------


def test_chat_translates_tools_and_history_and_parses_tool_use():
    client = _FakeBedrock(response=_response(
        "Those two are the same task.",
        tool_uses=[
            ("t1", "propose_merge", {"keep_id": "a", "absorb_id": "b"}),
            ("t2", "propose_complete", {"id": "a"}),  # parallel calls
        ],
    ))
    provider = BedrockProvider(model=NOVA, client=client)

    history = [
        {"role": "user", "content": "merge the Q3 items"},
        {"role": "assistant", "content": "I proposed a merge for your approval."},
        {"role": "user", "content": "and mark the cert done"},
    ]
    tools = [{"name": "propose_merge", "description": "d",
              "input_schema": {"type": "object", "properties": {"keep_id": {"type": "string"}}}}]
    result = provider.chat(system_prompt="sys", messages=history, tools=tools)

    # (a) internal defs -> Converse toolSpec (schema nested under inputSchema.json)
    assert client.last_kwargs["toolConfig"] == {"tools": [{"toolSpec": {
        "name": "propose_merge", "description": "d",
        "inputSchema": {"json": {"type": "object",
                                 "properties": {"keep_id": {"type": "string"}}}}}}]}
    # prose-only history -> one text block per turn, system prompt kept separate
    assert client.last_kwargs["messages"] == [
        {"role": "user", "content": [{"text": "merge the Q3 items"}]},
        {"role": "assistant", "content": [{"text": "I proposed a merge for your approval."}]},
        {"role": "user", "content": [{"text": "and mark the cert done"}]},
    ]
    # (b) Converse response -> internal text + ToolCalls
    assert result.text == "Those two are the same task."
    assert [(c.id, c.name, c.input) for c in result.tool_calls] == [
        ("t1", "propose_merge", {"keep_id": "a", "absorb_id": "b"}),
        ("t2", "propose_complete", {"id": "a"}),
    ]
    assert result.usage.input_tokens == 12 and result.usage.output_tokens == 8


def test_chat_omits_tool_config_when_there_are_no_tools():
    client = _FakeBedrock()
    BedrockProvider(model=NOVA, client=client).chat(
        system_prompt="s", messages=[{"role": "user", "content": "hi"}]
    )
    assert "toolConfig" not in client.last_kwargs


# --- PHASE 3: streaming ---------------------------------------------------------


def test_chat_stream_yields_text_then_a_final_result_with_whole_tool_calls():
    from meeting_notes_todos.providers import ChatResult, TextDelta

    stream = [
        {"messageStart": {"role": "assistant"}},
        {"contentBlockDelta": {"contentBlockIndex": 0, "delta": {"text": "Marking "}}},
        {"contentBlockDelta": {"contentBlockIndex": 0, "delta": {"text": "it done."}}},
        {"contentBlockStop": {"contentBlockIndex": 0}},
        {"contentBlockStart": {"contentBlockIndex": 1,
                               "start": {"toolUse": {"toolUseId": "t1",
                                                     "name": "propose_complete"}}}},
        # tool input streams as JSON-string fragments and must not render early
        {"contentBlockDelta": {"contentBlockIndex": 1, "delta": {"toolUse": {"input": '{"id":'}}}},
        {"contentBlockDelta": {"contentBlockIndex": 1, "delta": {"toolUse": {"input": ' "a"}'}}}},
        {"contentBlockStop": {"contentBlockIndex": 1}},
        {"messageStop": {"stopReason": "tool_use"}},
        {"metadata": {"usage": {"inputTokens": 9, "outputTokens": 4, "totalTokens": 13}}},
    ]
    provider = BedrockProvider(model=NOVA, client=_FakeBedrock(stream=stream))

    events = list(provider.chat_stream(
        system_prompt="s", messages=[{"role": "user", "content": "done with a"}]
    ))

    deltas = [e.text for e in events if isinstance(e, TextDelta)]
    assert deltas == ["Marking ", "it done."]  # text arrives incrementally
    final = events[-1]
    assert isinstance(final, ChatResult)
    assert final.text == "Marking it done."
    assert [(c.id, c.name, c.input) for c in final.tool_calls] == [
        ("t1", "propose_complete", {"id": "a"})
    ]
    assert final.usage.input_tokens == 9 and final.usage.output_tokens == 4


def test_stream_tool_input_that_never_parses_degrades_to_empty_args():
    stream = [
        {"contentBlockStart": {"contentBlockIndex": 0,
                               "start": {"toolUse": {"toolUseId": "t1", "name": "propose_delete"}}}},
        {"contentBlockDelta": {"contentBlockIndex": 0, "delta": {"toolUse": {"input": "{not json"}}}},
    ]
    provider = BedrockProvider(model=NOVA, client=_FakeBedrock(stream=stream))
    final = list(provider.chat_stream(system_prompt="s", messages=[]))[-1]
    assert [(c.name, c.input) for c in final.tool_calls] == [("propose_delete", {})]


# --- PHASE 4: error mapping ------------------------------------------------------


@pytest.mark.parametrize(
    "code, expected",
    [
        ("ThrottlingException", "throttled"),
        ("AccessDeniedException", "denied"),
        ("ValidationException", "rejected"),
    ],
)
def test_client_errors_surface_distinctly(code, expected):
    provider = BedrockProvider(model=NOVA, client=_FakeBedrock(error=_client_error(code)))
    with pytest.raises(RuntimeError) as exc:
        provider.complete(system_prompt="s", user_content="u")
    assert expected in str(exc.value).lower()


def test_validation_errors_explain_and_auth_errors_stay_quiet():
    # a wrong model id is the common ValidationException: the AWS text helps
    bad_model = _client_error(
        "ValidationException",
        "Invocation of model ID amazon.nova-2-lite-v1:0 with on-demand throughput isn't supported",
    )
    provider = BedrockProvider(model=NOVA, client=_FakeBedrock(error=bad_model))
    with pytest.raises(RuntimeError) as exc:
        provider.complete(system_prompt="s", user_content="u")
    assert "on-demand throughput" in str(exc.value)

    # AWS's own denial text can name the calling principal or access key id, so
    # only our wording is surfaced
    denied = _client_error(
        "AccessDeniedException", "User: arn:aws:iam::123456789012:user/throughline is not authorized"
    )
    provider = BedrockProvider(model=NOVA, client=_FakeBedrock(error=denied))
    with pytest.raises(RuntimeError) as exc:
        provider.complete(system_prompt="s", user_content="u")
    assert "arn:aws:iam" not in str(exc.value) and "123456789012" not in str(exc.value)


def test_non_client_errors_propagate_unchanged():
    provider = BedrockProvider(model=NOVA, client=_FakeBedrock(error=KeyboardInterrupt()))
    with pytest.raises(KeyboardInterrupt):
        provider.complete(system_prompt="s", user_content="u")
