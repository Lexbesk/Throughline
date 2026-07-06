"""Chat assistant tests (v2 M8; key-free, via a scripted fake provider).

Covers the advisory pipeline end to end: context assembly (live list + profile),
tool-call parsing and id validation (§4.7), and applying accepted ops (§4.3).
"""

from __future__ import annotations

from datetime import date, datetime, timezone

from meeting_notes_todos.chat import ChatOp, apply_chat_ops, run_chat_turn
from meeting_notes_todos.config import PromptsConfig
from meeting_notes_todos.models import ActionItem
from meeting_notes_todos.providers.base import ChatResult, LLMProvider, ToolCall, Usage

PROMPTS = PromptsConfig()  # reads prompts/ from the repo root (pytest cwd)
_TS = datetime(2026, 6, 28, 12, 0, 0, tzinfo=timezone.utc)
TODAY = date(2026, 7, 1)  # Wednesday; Friday = 2026-07-03


def _item(item_id: str = "a", title: str = "T", **overrides) -> ActionItem:
    base = dict(
        id=item_id,
        title=title,
        source_meeting_id="m",
        source_snippet="s",
        created_at=_TS,
        updated_at=_TS,
    )
    base.update(overrides)
    return ActionItem(**base)


class FakeChatProvider(LLMProvider):
    """Returns pre-scripted ChatResults in order; records each call."""

    def __init__(self, results: list[ChatResult]) -> None:
        self._results = list(results)
        self.calls: list[dict] = []

    def complete(self, *, system_prompt, user_content, max_tokens=None, response_schema=None):
        raise AssertionError("the chat path must not call complete()")

    def chat(self, *, system_prompt, messages, tools=None, max_tokens=None):
        self.calls.append({"system": system_prompt, "messages": messages, "tools": tools})
        return self._results.pop(0)


def _turn(provider, items, user="hello", profile=None):
    return run_chat_turn(
        provider=provider,
        prompts=PROMPTS,
        items=items,
        messages=[{"role": "user", "content": user}],
        profile=profile,
    )


def _result(text="", calls=()):
    return ChatResult(text=text, tool_calls=list(calls), usage=Usage(7, 3))


def test_assistant_prompt_keeps_slots_and_v3_sections():
    from pathlib import Path

    text = (Path("prompts") / "assistant.md").read_text(encoding="utf-8")
    for slot in ("{task_list}", "{profile}", "{today}"):
        assert slot in text, f"missing slot {slot}"
    assert "Inferred possibilities" in text  # broadened input (v3 §7.1)
    assert "Gap analysis" in text  # v3 §7.2
    assert "Two proposal targets" in text  # M13 routing guidance


# --- run_chat_turn: context assembly (§4.5) ----------------------------------


def test_pure_talk_turn_has_message_and_no_proposals():
    provider = FakeChatProvider([_result(text="Do the cert renewal first — it expires Monday.")])
    turn = _turn(provider, [_item("a", "Renew the cert")], user="what first?")

    assert turn.message == "Do the cert renewal first — it expires Monday."
    assert turn.proposals == [] and turn.dropped == []
    assert turn.usage.input_tokens == 7


def test_system_prompt_injects_live_list_ids_profile_and_today():
    items = [
        _item("id-one", "Renew the cert", owner="Marco", priority="high"),
        _item("id-two", "Draft the announcement", due_date=date(2026, 7, 8)),
    ]
    provider = FakeChatProvider([_result(text="ok")])
    run_chat_turn(
        provider=provider, prompts=PROMPTS, items=items,
        messages=[{"role": "user", "content": "hello"}],
        profile="Role: eval lead. Cares about the Q3 report.",
        today=date(2026, 7, 6),
    )

    system = provider.calls[0]["system"]
    assert "id: id-one" in system and "id: id-two" in system
    assert "Renew the cert" in system and "owner: Marco" in system
    assert "priority: high" in system and "due: 2026-07-08" in system
    assert "Role: eval lead" in system  # profile injected (§4.5)
    assert "Today is 2026-07-06" in system  # date context (v3 M12)
    assert provider.calls[0]["tools"], "tool definitions must be passed to the provider"


def test_empty_list_and_missing_profile_have_placeholders():
    provider = FakeChatProvider([_result(text="ok")])
    _turn(provider, [])
    system = provider.calls[0]["system"]
    assert "(the list is empty)" in system
    assert "(no profile provided)" in system


# --- run_chat_turn: staging + validation (§4.7) -------------------------------


def test_valid_tool_calls_become_staged_proposals():
    items = [
        _item("q1", "Send the Q3 eval report", owner="Priya"),
        _item("q2", "Get the Q3 report out", description="the exec summary"),
        _item("c1", "Renew the cert"),
    ]
    provider = FakeChatProvider(
        [
            _result(
                text="Those two Q3 items are the same task.",
                calls=[
                    ToolCall(id="t1", name="propose_merge",
                             input={"keep_id": "q1", "absorb_id": "q2"}),
                    ToolCall(id="t2", name="propose_complete", input={"id": "c1"}),
                    ToolCall(id="t3", name="reprioritize",
                             input={"id": "c1", "priority": "high", "position": 0}),
                    ToolCall(id="t4", name="propose_new",
                             input={"title": "Book the retro room", "due_date_text": "by Friday"}),
                    ToolCall(id="t5", name="propose_update",
                             input={"id": "q1", "owner": "Lei"}),
                ],
            )
        ]
    )
    turn = _turn(provider, items, user="clean up the list")

    assert turn.dropped == []
    ops = [p["op"] for p in turn.proposals]
    assert ops == ["merge", "complete", "reprioritize", "new", "update"]
    assert all(p["target"] == "todo" for p in turn.proposals)  # destination (v3 §8)
    merge = turn.proposals[0]
    assert merge["id"] == "q1" and merge["absorb_id"] == "q2"
    assert merge["item"]["title"] == "Send the Q3 eval report"
    assert merge["absorb"]["title"] == "Get the Q3 report out"
    update = turn.proposals[4]
    assert update["changes"] == [{"field": "owner", "before": "Priya", "after": "Lei"}]


def test_stale_ids_frozen_targets_and_bad_calls_are_dropped():
    items = [_item("a", "Open task"), _item("d", "Done task", status="done")]
    provider = FakeChatProvider(
        [
            _result(
                text="…",
                calls=[
                    ToolCall(id="t1", name="propose_complete", input={"id": "nope"}),  # stale id
                    ToolCall(id="t2", name="propose_update", input={"id": "d", "title": "X"}),  # frozen
                    ToolCall(id="t3", name="propose_merge", input={"keep_id": "a", "absorb_id": "a"}),
                    ToolCall(id="t4", name="propose_merge", input={"keep_id": "a", "absorb_id": "d"}),  # frozen
                    ToolCall(id="t5", name="run_shell_command", input={}),  # not a real tool
                    ToolCall(id="t6", name="propose_new", input={}),  # no title
                    ToolCall(id="t7", name="reprioritize", input={"id": "a"}),  # nothing to change
                ],
            )
        ]
    )
    turn = _turn(provider, items)

    assert turn.proposals == []  # none of these may reach the user as a card
    assert len(turn.dropped) == 7
    assert "'nope'" in turn.dropped[0]
    assert "done" in turn.dropped[1]
    assert "unknown tool" in turn.dropped[4]


def test_extracted_new_items_carry_their_source_snippet():
    # the unified surface (M12): pasted notes become propose_new calls whose
    # source_snippet preserves the miss-catching link back to the input text
    provider = FakeChatProvider(
        [_result(text="Two action items in these notes.", calls=[
            ToolCall(id="t1", name="propose_new",
                     input={"title": "Renew the staging cert", "owner": "Marco",
                            "due_date_text": "by Tuesday",
                            "source_snippet": "Marco offered to renew the staging cert by Tuesday."}),
        ])]
    )
    turn = _turn(provider, [], user="- Standup notes\n- Marco offered to renew…")

    (proposal,) = turn.proposals
    assert proposal["source_snippet"] == "Marco offered to renew the staging cert by Tuesday."

    result = apply_chat_ops([], [ChatOp(**{k: v for k, v in proposal.items() if k != "op"},
                                        op="new")], today=TODAY)
    (item,) = result.final
    assert item.source_snippet == "Marco offered to renew the staging cert by Tuesday."
    assert item.due_date == date(2026, 7, 7)  # resolved in code, as ever


def test_profile_section_update_is_staged_with_the_current_body_as_before():
    profile = "## Long-term goals\n\nShip the eval platform.\n\n## Current focus\n\nQ3 report."
    provider = FakeChatProvider(
        [_result(text="Noted — updating your focus.", calls=[
            ToolCall(id="t1", name="propose_profile_update",
                     input={"section": "Current focus",
                            "new_text": "Q3 shipped; now the v3 planning work."}),
        ])]
    )
    turn = _turn(provider, [_item()], user="the Q3 report is out the door", profile=profile)

    (proposal,) = turn.proposals
    assert proposal["op"] == "profile"
    assert proposal["target"] == "profile"  # routed to the profile, not the list (v3 §8)
    assert proposal["section"] == "Current focus"
    assert proposal["new_text"] == "Q3 shipped; now the v3 planning work."
    assert proposal["before"] == "Q3 report."  # only that section's current body


def test_one_turn_can_propose_to_both_targets():
    # M13 acceptance shape: a single turn yields a todo proposal AND a
    # profile-goal update, each routed to its destination
    provider = FakeChatProvider(
        [_result(text="A goal and a first step.", calls=[
            ToolCall(id="t1", name="propose_profile_update",
                     input={"section": "Long-term goals", "new_text": "Run a marathon next year."}),
            ToolCall(id="t2", name="propose_new",
                     input={"title": "Sign up for the Tuesday run club"}),
        ])]
    )
    turn = _turn(provider, [], user="I want to run a marathon next year; sign me up for the run club")

    assert [(p["op"], p["target"]) for p in turn.proposals] == [
        ("profile", "profile"), ("new", "todo")
    ]


def test_profile_update_needs_section_and_text_and_respects_the_cap():
    provider = FakeChatProvider(
        [_result(text="…", calls=[
            ToolCall(id="t1", name="propose_profile_update", input={"new_text": "x"}),
            ToolCall(id="t2", name="propose_profile_update", input={"section": "Current focus"}),
            ToolCall(id="t3", name="propose_profile_update",
                     input={"section": "Current focus", "new_text": "x" * 601}),
        ])]
    )
    turn = _turn(provider, [_item()])
    assert turn.proposals == []
    assert "needs a section name and new text" in turn.dropped[0]
    assert "needs a section name and new text" in turn.dropped[1]
    assert "too long" in turn.dropped[2]  # sections stay tight (§5)


# --- apply_chat_ops: the commit side of the advisory gate (§4.3) --------------


def test_apply_merge_collapses_the_q3_duplicates():
    items = [
        _item("q1", "Send the Q3 eval report", owner="Priya"),
        _item("q2", "Get the Q3 report out", description="the exec summary"),
        _item("c1", "Renew the cert"),
    ]
    result = apply_chat_ops(items, [ChatOp(op="merge", id="q1", absorb_id="q2")], today=TODAY)

    assert [i.id for i in result.final] == ["q1", "c1"]  # q2 folded in and removed
    merged = result.final[0]
    assert merged.owner == "Priya"
    assert merged.description == "the exec summary"  # filled from the absorbed duplicate
    assert merged.created_at == _TS  # identity preserved
    assert result.applied and "folded into" in result.applied[0]


def test_apply_new_update_delete_complete():
    items = [_item("a", "Renew the cert"), _item("b", "Old noise")]
    ops = [
        ChatOp(op="new", title="Book the retro room", due_date_text="by Friday", priority="high"),
        ChatOp(op="update", id="a", owner="Marco", due_date_text="by Friday"),
        ChatOp(op="delete", id="b"),
        ChatOp(op="complete", id="a"),
    ]
    result = apply_chat_ops(items, ops, today=TODAY)

    assert len(result.applied) == 4 and result.skipped == []
    by_title = {i.title: i for i in result.final}
    assert by_title["Old noise"].status == "deleted"  # soft delete: the row is kept
    new = by_title["Book the retro room"]
    assert new.due_date == date(2026, 7, 3)  # resolved in code, not by the model
    assert new.priority == "high" and new.status == "todo" and new.source_meeting_id == "chat"
    cert = by_title["Renew the cert"]
    assert cert.owner == "Marco" and cert.due_date == date(2026, 7, 3)
    assert cert.status == "done"  # the complete op, applied after the update


def test_apply_never_touches_frozen_items_and_validates_ids():
    items = [_item("d", "Shipped it", status="done"), _item("a", "Open")]
    ops = [
        ChatOp(op="update", id="d", title="Rewrite"),
        ChatOp(op="complete", id="d"),
        ChatOp(op="merge", id="a", absorb_id="d"),
        ChatOp(op="reprioritize", id="d", priority="high"),
        ChatOp(op="delete", id="d"),  # even soft delete never touches frozen items
        ChatOp(op="complete", id="zzz"),  # stale id at commit time
    ]
    result = apply_chat_ops(items, ops, today=TODAY)

    assert result.applied == [] and len(result.skipped) == 6
    assert [i.id for i in result.final] == ["d", "a"]
    assert result.final[0] == items[0]  # the done item is bit-for-bit untouched


def test_apply_reprioritize_moves_and_stamps_positions():
    items = [_item("a", "A"), _item("b", "B"), _item("c", "C")]
    ops = [ChatOp(op="reprioritize", id="c", priority="high", position=0)]
    result = apply_chat_ops(items, ops, today=TODAY)

    assert [i.id for i in result.final] == ["c", "a", "b"]
    assert [i.position for i in result.final] == [0, 1, 2]  # dense, as the M6 reorder stamps
    assert result.final[0].priority == "high"

    cleared = apply_chat_ops(result.final, [ChatOp(op="reprioritize", id="c", priority="none")])
    assert cleared.final[0].priority is None


def test_apply_delete_is_soft_and_reversible_in_the_store():
    items = [_item("a", "Old noise", owner="Lei")]
    result = apply_chat_ops(items, [ChatOp(op="delete", id="a")], today=TODAY)

    (deleted,) = result.final  # the row is kept — hidden, not destroyed
    assert deleted.status == "deleted"
    assert deleted.owner == "Lei" and deleted.created_at == _TS  # everything else intact
    assert "moved to Deleted" in result.applied[0]


def test_deleted_items_are_invisible_to_the_chat_turn():
    items = [_item("a", "Visible task"), _item("z", "Hidden clutter", status="deleted")]
    provider = FakeChatProvider(
        [_result(text="…", calls=[ToolCall(id="t1", name="propose_complete", input={"id": "z"})])]
    )
    turn = _turn(provider, items)

    system = provider.calls[0]["system"]
    assert "Visible task" in system
    assert "Hidden clutter" not in system and "id: z" not in system  # off the injected list
    assert turn.proposals == []  # targeting the hidden id is dropped, not staged
    assert "unknown or stale id" in turn.dropped[0]
