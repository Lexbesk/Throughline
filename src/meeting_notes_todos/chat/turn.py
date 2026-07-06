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
from ..providers.base import LLMProvider, ToolCall, Usage
from .tools import TOOL_DEFS

_FROZEN = ("done", "cancelled", "deleted")

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
    # soft-deleted items are off the list/view, so the assistant neither sees nor
    # targets them; a proposal carrying such an id is dropped as unknown/stale
    items = [item for item in items if item.status != "deleted"]
    profile = (profile or "").strip() or None
    system_prompt = load_prompt(
        prompts.dir,
        prompts.chat,
        task_list=format_task_list(items),
        profile=profile or NO_PROFILE,
        today=(today or date.today()).isoformat(),
    )
    result = provider.chat(system_prompt=system_prompt, messages=messages, tools=TOOL_DEFS)

    by_id = {item.id: item for item in items}
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
