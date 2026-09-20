"""Amazon Bedrock provider — the Converse API via boto3 (build plan §4.1).

Talks to ``bedrock-runtime`` through ``converse`` / ``converse_stream``: one wire
dialect for every Bedrock model, so completion, tool calling, and streaming all
come from the same shape. Selected by config like every other provider — the
model string routes here (``amazon.*``, or the ``us.`` cross-region inference
profile form).

Credentials. Local single-user mode uses boto3's default chain (``.env`` /
``~/.aws``). Hosted mode passes the requesting user's stored credentials
explicitly. AWS needs three values where the per-user key store (v4 M18) holds
one string per provider, so the triple is serialized into that single slot by
``pack_aws_credentials`` and unpacked at use — the store itself is unchanged.
Credential values are never logged, echoed, or put in an error message.

Like the local provider, Converse has no pydantic-native structured output:
``response_schema`` is accepted and ignored, the prompts ask for JSON, and the
pipeline parses/repairs (§4.1).
"""

from __future__ import annotations

import json
import os
from typing import Any

from .base import ChatResult, CompletionResult, LLMProvider, TextDelta, ToolCall, Usage

DEFAULT_REGION = "us-east-2"

_CRED_FIELDS = ("access_key_id", "secret_access_key", "region")


def pack_aws_credentials(access_key_id: str, secret_access_key: str, region: str) -> str:
    """The three AWS values as the one opaque string the key store encrypts.

    A blank region falls back to :data:`DEFAULT_REGION`; a missing key id or
    secret raises ``ValueError`` (the web layer turns that into a 400, exactly
    as an empty single-string key already does).
    """
    credentials = {
        "access_key_id": (access_key_id or "").strip(),
        "secret_access_key": (secret_access_key or "").strip(),
        "region": (region or "").strip() or DEFAULT_REGION,
    }
    missing = [field for field in _CRED_FIELDS if not credentials[field]]
    if missing:
        raise ValueError(f"missing AWS credential field(s): {', '.join(missing)}")
    return json.dumps(credentials)


def parse_aws_credentials(blob: str) -> dict[str, str]:
    """Inverse of :func:`pack_aws_credentials`.

    Raises ``ValueError`` on anything malformed. The message never quotes the
    blob — it holds a live secret.
    """
    try:
        credentials = json.loads(blob)
    except json.JSONDecodeError:
        raise ValueError("stored AWS credentials are not valid JSON") from None
    if not isinstance(credentials, dict) or any(
        not str(credentials.get(field, "")).strip() for field in _CRED_FIELDS
    ):
        raise ValueError("stored AWS credentials are missing a required field")
    return {field: str(credentials[field]).strip() for field in _CRED_FIELDS}


def _content(resp: dict) -> list[dict]:
    return ((resp.get("output") or {}).get("message") or {}).get("content") or []


def _join_text(resp: dict) -> str:
    return "".join(block["text"] for block in _content(resp) if "text" in block)


def _calls_of(resp: dict) -> list[ToolCall]:
    return [
        ToolCall(
            id=block["toolUse"].get("toolUseId", "") or "",
            name=block["toolUse"].get("name", ""),
            input=dict(block["toolUse"].get("input") or {}),
        )
        for block in _content(resp)
        if "toolUse" in block
    ]


def _usage_of(resp: dict) -> Usage:
    # Converse also reports totalTokens; Usage carries the two fields the usage
    # log records, and the total is their sum, so it is dropped here.
    usage = resp.get("usage") or {}
    return Usage(
        input_tokens=usage.get("inputTokens", 0) or 0,
        output_tokens=usage.get("outputTokens", 0) or 0,
    )


def _parse_input(fragments: str) -> dict:
    try:
        return json.loads(fragments or "{}")
    except json.JSONDecodeError:
        return {}


# Distinct handling for the three failures worth telling apart. Only
# ValidationException echoes the AWS text — it names the offending parameter
# (typically a model id with no on-demand throughput) and carries no secrets.
# Auth failures get our own wording, because AWS's can quote the calling
# principal or access key id, which must never reach a log or a response.
_FRIENDLY = {
    "ThrottlingException": "Bedrock throttled this request (rate or quota limit); retry shortly.",
    "AccessDeniedException": "Bedrock denied this request: the credentials lack permission for "
    "this model, or model access is not enabled for them in this region.",
}


def _friendly(exc: Exception) -> Exception | None:
    """Map a botocore ``ClientError`` to a clear error, or None to re-raise as-is.

    Read through the ``response["Error"]["Code"]`` shape ClientError exposes, so
    this module needs no botocore import of its own.
    """
    error = (getattr(exc, "response", None) or {}).get("Error") or {}
    code = error.get("Code", "")
    if code == "ValidationException":
        return RuntimeError(f"Bedrock rejected the request: {error.get('Message', '')}".strip())
    if code in _FRIENDLY:
        return RuntimeError(_FRIENDLY[code])
    if code:
        return RuntimeError(f"Bedrock call failed ({code}).")
    return None


def _resolve_region(region: str | None) -> str:
    """Config region, else the environment boto3 would have read, else the default."""
    return (
        region
        or os.environ.get("AWS_REGION")
        or os.environ.get("AWS_DEFAULT_REGION")
        or DEFAULT_REGION
    )


class BedrockProvider(LLMProvider):
    """Calls Bedrock's Converse API through boto3.

    ``credentials`` (the unpacked triple) is passed when the request runs on a
    user's stored AWS credentials; when it is None boto3's default chain applies
    (local single-user mode). A pre-built ``client`` can be injected (tests).
    """

    def __init__(
        self,
        model: str,
        *,
        region: str | None = None,
        max_tokens: int = 1024,
        temperature: float | None = None,
        credentials: dict[str, str] | None = None,
        client: Any = None,
    ) -> None:
        self._model = model
        self._max_tokens = max_tokens
        self._temperature = temperature
        if client is not None:
            self._client = client
        else:  # lazy import so boto3 is only needed when this provider is used
            import boto3

            if credentials:  # hosted: this user's own credentials, never the env
                self._client = boto3.client(
                    "bedrock-runtime",
                    region_name=credentials["region"],
                    aws_access_key_id=credentials["access_key_id"],
                    aws_secret_access_key=credentials["secret_access_key"],
                )
            else:
                self._client = boto3.client(
                    "bedrock-runtime", region_name=_resolve_region(region)
                )

    def _call(self, fn, **kwargs):
        try:
            return fn(**kwargs)
        except Exception as exc:
            friendly = _friendly(exc)
            if friendly is None:
                raise  # not a ClientError — leave it exactly as it came
            raise friendly from exc

    def _converse_kwargs(
        self, system_prompt: str, messages: list[dict], tools: list[dict] | None, max_tokens
    ) -> dict[str, Any]:
        inference: dict[str, Any] = {"maxTokens": max_tokens or self._max_tokens}
        if self._temperature is not None:  # omitted → the model's own default
            inference["temperature"] = self._temperature
        kwargs: dict[str, Any] = {
            "modelId": self._model,
            # Converse content is always a block list; the app's history is prose
            # turns (advisory-first: no tool results ever enter it), so each one
            # becomes a single text block.
            "messages": [
                {"role": m["role"], "content": [{"text": m["content"]}]} for m in messages
            ],
            "system": [{"text": system_prompt}],  # a separate param, not a message
            "inferenceConfig": inference,
        }
        if tools:
            kwargs["toolConfig"] = {
                "tools": [
                    {
                        "toolSpec": {
                            "name": tool["name"],
                            "description": tool.get("description", ""),
                            "inputSchema": {"json": tool.get("input_schema", {"type": "object"})},
                        }
                    }
                    for tool in tools
                ]
            }
        return kwargs

    def complete(
        self,
        *,
        system_prompt: str,
        user_content: str,
        max_tokens: int | None = None,
        response_schema: Any = None,
    ) -> CompletionResult:
        # response_schema is intentionally unused: Converse has no native
        # structured output, so the pipeline parses/repairs the text (§4.1).
        resp = self._call(
            self._client.converse,
            **self._converse_kwargs(
                system_prompt, [{"role": "user", "content": user_content}], None, max_tokens
            ),
        )
        return CompletionResult(
            text=_join_text(resp), usage=_usage_of(resp), raw=resp, parsed=None
        )

    def chat(
        self,
        *,
        system_prompt: str,
        messages: list[dict],
        tools: list[dict] | None = None,
        max_tokens: int | None = None,
    ) -> ChatResult:
        """Chat turn via Converse ``toolConfig`` (v2 §4.8); tool calls are not executed."""
        resp = self._call(
            self._client.converse,
            **self._converse_kwargs(system_prompt, messages, tools, max_tokens),
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
        """Stream via ``converse_stream``. Text arrives as ``contentBlockDelta``
        chunks; a tool call opens with its name on ``contentBlockStart`` and its
        input follows as JSON-string fragments, so calls are reassembled by block
        index and delivered whole at the end — proposals never render half-formed.
        Usage arrives last, on the ``metadata`` event."""
        resp = self._call(
            self._client.converse_stream,
            **self._converse_kwargs(system_prompt, messages, tools, max_tokens),
        )
        text = ""
        usage = Usage()
        frags: dict[int, dict] = {}  # content-block index -> {id, name, input}
        for event in resp.get("stream") or []:
            if "contentBlockStart" in event:
                block = event["contentBlockStart"]
                start = (block.get("start") or {}).get("toolUse") or {}
                if start:
                    frags[block.get("contentBlockIndex", 0)] = {
                        "id": start.get("toolUseId", "") or "",
                        "name": start.get("name", ""),
                        "input": "",
                    }
            elif "contentBlockDelta" in event:
                block = event["contentBlockDelta"]
                delta = block.get("delta") or {}
                if delta.get("text"):
                    text += delta["text"]
                    yield TextDelta(delta["text"])
                elif "toolUse" in delta:
                    frag = frags.setdefault(
                        block.get("contentBlockIndex", 0), {"id": "", "name": "", "input": ""}
                    )
                    frag["input"] += delta["toolUse"].get("input", "") or ""
            elif "metadata" in event:
                usage = _usage_of(event["metadata"])
        calls = [
            ToolCall(id=f["id"], name=f["name"], input=_parse_input(f["input"]))
            for _, f in sorted(frags.items())
            if f["name"]
        ]
        yield ChatResult(text=text, tool_calls=calls, usage=usage, raw=None)
