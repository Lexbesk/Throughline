"""One chat turn (v2 §4.4–§4.7): assemble context, call the provider with tools,
and parse the response into a message plus validated, staged proposals.

The chat is stateful where extraction is not: every turn re-injects the *live*
task list (§4.6) so the model never reasons about a stale snapshot. Every id a
tool call references is validated against that list (§4.7) — hallucinated or
stale ids are dropped and reported, never applied. Nothing here writes anything;
accepted proposals go through ``apply.apply_chat_ops``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from ..config import PromptsConfig
from ..models import ActionItem
from ..profile import NO_PROFILE, SECTION_CAP, section_body
from ..prompts import load_prompt
from ..providers.base import ChatResult, LLMProvider, TextDelta, ToolCall, Usage
from .tools import TOOL_DEFS

_FROZEN = ("done", "cancelled", "deleted")

# streaming attempts before giving up, retried only before any text has streamed
# (a dropped connection on a flaky network, common at the connect/first-token stage)
_STREAM_ATTEMPTS = 3

_OP_BY_TOOL = {
    "propose_new": "new",
    "propose_update": "update",
    "propose_delete": "delete",
    "propose_merge": "merge",
    "reprioritize": "reprioritize",
    "propose_complete": "complete",
    "propose_profile_update": "profile",
}

_UPDATE_FIELDS = ("title", "description", "owner", "due_date_text", "project")


@dataclass
class ChatTurn:
    message: str  # the model's prose — the conversation (and its stated reasoning)
    proposals: list[dict] = field(default_factory=list)  # UI-ready; each carries a
    # "target" of "todo" | "profile" (v3 §8) and, for todo ops, an "item" brief
    dropped: list[str] = field(default_factory=list)  # discarded tool calls, with reasons
    usage: Usage = field(default_factory=Usage)


def _prepare_turn(
    prompts: PromptsConfig,
    items: list[ActionItem],
    profile: str | None,
    today: date | None,
) -> tuple[str, str | None, dict[str, ActionItem]]:
    """Shared setup for the streaming and non-streaming turns: soft-deleted items
    are off the list (so they can't be seen or targeted), then build the system
    prompt and the id lookup."""
    items = [item for item in items if item.status != "deleted"]
    profile = (profile or "").strip() or None
    system_prompt = load_prompt(
        prompts.dir,
        prompts.chat,
        task_list=format_task_list(items),
        profile=profile or NO_PROFILE,
        today=(today or date.today()).isoformat(),
    )
    return system_prompt, profile, {item.id: item for item in items}


def _turn_from_result(result, profile: str | None, by_id: dict[str, ActionItem]) -> ChatTurn:
    """Parse a provider ChatResult into a ChatTurn (message + staged proposals).

    This is the same staging discipline for both paths — id validation, target
    routing, before/after for profile sections — unchanged from before."""
    proposals: list[dict] = []
    dropped: list[str] = []
    for call in result.tool_calls:
        proposal, reason = _stage(call, by_id)
        if proposal is not None:
            # every proposal is routed to one of two destinations (v3 §4.2/§8)
            proposal["target"] = "profile" if proposal["op"] == "profile" else "todo"
            if proposal["op"] == "profile":  # current section body → the card's before
                proposal["before"] = section_body(profile, proposal["section"])
            proposals.append(proposal)
        else:
            dropped.append(reason or f"tool call {call.name!r} could not be staged")
    return ChatTurn(
        message=result.text.strip(), proposals=proposals, dropped=dropped, usage=result.usage
    )


def run_chat_turn(
    *,
    provider: LLMProvider,
    prompts: PromptsConfig,
    items: list[ActionItem],
    messages: list[dict],
    profile: str | None = None,
    today: date | None = None,
) -> ChatTurn:
    """Run one turn: system prompt + live list (+ profile) + history → message + proposals."""
    system_prompt, profile, by_id = _prepare_turn(prompts, items, profile, today)
    result = provider.chat(system_prompt=system_prompt, messages=messages, tools=TOOL_DEFS)
    return _turn_from_result(result, profile, by_id)


def run_chat_turn_stream(
    *,
    provider: LLMProvider,
    prompts: PromptsConfig,
    items: list[ActionItem],
    messages: list[dict],
    profile: str | None = None,
    today: date | None = None,
):
    """Streaming turn: yield ``("text", chunk)`` as the message generates, then a
    single ``("done", ChatTurn)`` once the turn completes. Message text streams
    live; proposals are parsed from the final result and delivered whole in the
    'done' event — never half-formed.

    Resilience: a streamed request holds the connection open longer than a quick
    call, so a flaky network is more likely to drop it. When that happens
    *before any text has been shown*, it is safe to retry transparently (nothing
    reached the user yet, and the turn has no side effects) — so we start a fresh
    stream up to ``_STREAM_ATTEMPTS`` times. Once text has started streaming we
    can't cleanly retry, so a later failure surfaces as before.
    """
    system_prompt, profile, by_id = _prepare_turn(prompts, items, profile, today)
    attempt = 0
    while True:
        attempt += 1
        streamed_any = False
        result = None
        try:
            for event in provider.chat_stream(
                system_prompt=system_prompt, messages=messages, tools=TOOL_DEFS
            ):
                if isinstance(event, TextDelta):
                    streamed_any = True
                    yield ("text", event.text)
                elif isinstance(event, ChatResult):
                    result = event
            if result is None:  # a stream that yielded no terminal result
                raise RuntimeError("chat stream ended without a final result")
            break  # completed cleanly
        except Exception:
            if streamed_any or attempt >= _STREAM_ATTEMPTS:
                raise  # can't retry mid-message, or out of attempts
            # nothing shown yet → transparently start a fresh stream
    yield ("done", _turn_from_result(result, profile, by_id))


def format_task_list(items: list[ActionItem]) -> str:
    """The live list as injected into the system prompt — ids included so the
    operations have something to target (§4.5)."""
    if not items:
        return "(the list is empty)"
    lines = []
    for i, item in enumerate(items, 1):
        bits = [f"[{item.status}] {item.title}"]
        if item.owner:
            bits.append(f"owner: {item.owner}")
        if item.due_date:
            bits.append(f"due: {item.due_date.isoformat()}")
        elif item.due_date_text:
            bits.append(f'due phrase: "{item.due_date_text}"')
        if item.priority:
            bits.append(f"priority: {item.priority}")
        if item.description:
            bits.append(f"note: {item.description}")
        lines.append(f"{i}. " + "; ".join(bits) + f"  (id: {item.id})")
    return "\n".join(lines)


def _stage(call: ToolCall, by_id: dict[str, ActionItem]) -> tuple[dict | None, str | None]:
    """Validate one tool call into a staged proposal, or return a drop reason (§4.7)."""
    op = _OP_BY_TOOL.get(call.name)
    if op is None:
        return None, f"unknown tool {call.name!r}"
    data = call.input or {}

    if op == "new":
        title = _clean(data.get("title"))
        if not title:
            return None, "propose_new without a title"
        priority = data.get("priority")
        return {
            "op": "new",
            "title": title,
            "description": _clean(data.get("description")),
            "owner": _clean(data.get("owner")),
            "due_date_text": _clean(data.get("due_date_text")),
            "priority": priority if priority in ("high", "medium", "low") else None,
            "source_snippet": _clean(data.get("source_snippet")),  # miss-catching (v3 M12)
        }, None

    if op == "profile":
        section = _clean(data.get("section"))
        new_text = _clean(data.get("new_text"))
        if not section or not new_text:
            return None, "propose_profile_update needs a section name and new text"
        if len(new_text) > SECTION_CAP:
            return None, (f"profile section update too long ({len(new_text)} chars, "
                          f"cap {SECTION_CAP}) — sections stay tight")
        return {"op": "profile", "section": section, "new_text": new_text}, None

    if op == "merge":
        keep = by_id.get(data.get("keep_id") or "")
        absorb = by_id.get(data.get("absorb_id") or "")
        if keep is None or absorb is None:
            bad = data.get("keep_id") if keep is None else data.get("absorb_id")
            return None, f"merge references an unknown or stale id {bad!r}"
        if keep.id == absorb.id:
            return None, "merge with keep_id == absorb_id"
        for item in (keep, absorb):
            if item.status in _FROZEN:
                return None, f"merge touches a {item.status} item ({item.title}) — left untouched"
        return {
            "op": "merge",
            "id": keep.id,
            "absorb_id": absorb.id,
            "title": _clean(data.get("title")),
            "description": _clean(data.get("description")),
            "item": _brief(keep),
            "absorb": _brief(absorb),
        }, None

    # single-target ops: update / delete / reprioritize / complete
    item_id = data.get("id") or ""
    target = by_id.get(item_id)
    if target is None:
        return None, f"{call.name} references an unknown or stale id {item_id!r}"
    if target.status in _FROZEN:
        return None, f"{call.name} targets a {target.status} item ({target.title}) — left untouched"

    if op == "update":
        fields = {}
        for name in _UPDATE_FIELDS:
            value = _clean(data.get(name))
            if value is not None and value != getattr(target, name):
                fields[name] = value
        if not fields:
            return None, f"propose_update for {target.title!r} changes nothing"
        changes = [
            {"field": name, "before": getattr(target, name), "after": value}
            for name, value in fields.items()
        ]
        return {"op": "update", "id": target.id, **fields, "changes": changes,
                "item": _brief(target)}, None

    if op == "reprioritize":
        priority = data.get("priority")
        position = data.get("position")
        if priority is not None and priority not in ("high", "medium", "low", "none"):
            return None, f"reprioritize with invalid priority {priority!r}"
        if position is not None and (not isinstance(position, int) or position < 0):
            return None, f"reprioritize with invalid position {position!r}"
        if priority is None and position is None:
            return None, "reprioritize with neither priority nor position"
        return {"op": "reprioritize", "id": target.id, "priority": priority,
                "position": position, "item": _brief(target)}, None

    return {"op": op, "id": target.id, "item": _brief(target)}, None  # delete / complete


def _brief(item: ActionItem) -> dict:
    return {
        "id": item.id,
        "title": item.title,
        "status": item.status,
        "owner": item.owner,
        "due_date": item.due_date.isoformat() if item.due_date else None,
        "due_date_text": item.due_date_text,
        "priority": item.priority,
    }


def _clean(value) -> str | None:
    if value is None:
        return None
    value = str(value).strip()
    return value or None
