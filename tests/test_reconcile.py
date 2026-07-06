"""Reconcile-stage tests (key-free, via a scripted fake provider).

Also covers the two M3 acceptance criteria end-to-end (reconcile + merge), with the
LLM's verdict mocked.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

from meeting_notes_todos.config import PromptsConfig
from meeting_notes_todos.models import ActionItem, Adjudication
from meeting_notes_todos.pipeline.merge import apply_decisions
from meeting_notes_todos.pipeline.reconcile import reconcile
from meeting_notes_todos.providers.base import CompletionResult, LLMProvider, Usage

PROMPTS = PromptsConfig()
_TS = datetime(2026, 6, 28, 12, 0, 0, tzinfo=timezone.utc)


def _item(**overrides) -> ActionItem:
    base = dict(
        id="x",
        title="T",
        description=None,
        status="todo",
        owner=None,
        due_date_text=None,
        due_date=None,
        tags=[],
        project=None,
        source_meeting_id="m",
        source_snippet="s",
        created_at=_TS,
        updated_at=_TS,
    )
    base.update(overrides)
    return ActionItem(**base)


class FakeProvider(LLMProvider):
    def __init__(self, scripted: list[CompletionResult]) -> None:
        self._scripted = list(scripted)
        self.calls: list[dict] = []

    def complete(self, *, system_prompt, user_content, max_tokens=None, response_schema=None):
        self.calls.append({"user": user_content, "schema": response_schema})
        return self._scripted.pop(0)


def _verdict(label, candidate=None) -> CompletionResult:
    return CompletionResult(
        text="", usage=Usage(3, 1), parsed=Adjudication(label=label, candidate=candidate)
    )


def _reconcile(provider, new_items, existing_items):
    return reconcile(
        provider=provider, prompts=PROMPTS, new_items=new_items, existing_items=existing_items
    )


def test_no_candidate_means_new_without_an_llm_call():
    provider = FakeProvider([])  # must not be called
    run = _reconcile(provider, [_item(id="b", title="Refactor the data loader")], [_item(id="a", title="Buy milk")])
    assert run.decisions[0].label == "NEW"
    assert provider.calls == []  # no candidate → no LLM call (cost control)


def test_similar_title_triggers_llm_and_duplicate_maps_to_target():
    existing = [_item(id="a", title="Send the benchmark results to the eval team")]
    new = _item(id="b", title="Send benchmark results to eval team")
    provider = FakeProvider([_verdict("DUPLICATE", candidate=1)])

    decision = _reconcile(provider, [new], existing).decisions[0]

    assert decision.label == "DUPLICATE"
    assert decision.target.id == "a"
    assert len(provider.calls) == 1
    assert provider.calls[0]["schema"] is Adjudication


def test_candidate_index_maps_to_the_right_existing_item():
    existing = [_item(id="a", title="Unrelated chore"), _item(id="b", title="Update the API docs")]
    new = _item(id="c", title="Update the API docs")
    # only the API-docs item is fuzzy-similar, so it is candidate #1
    decision = _reconcile(FakeProvider([_verdict("UPDATE", candidate=1)]), [new], existing).decisions[0]
    assert decision.label == "UPDATE"
    assert decision.target.id == "b"


def test_bad_candidate_index_falls_back_to_new():
    existing = [_item(id="a", title="Same task here")]
    new = _item(id="b", title="Same task here")
    decision = _reconcile(FakeProvider([_verdict("UPDATE", candidate=9)]), [new], existing).decisions[0]
    assert decision.label == "NEW"


def test_deleted_items_are_excluded_from_candidate_matching():
    provider = FakeProvider([])  # must not be called: the only fuzzy match is deleted
    existing = [_item(id="a", title="Send the deck", status="deleted")]
    run = _reconcile(provider, [_item(id="b", title="Send the deck")], existing)
    assert run.decisions[0].label == "NEW"  # re-mentioned task comes back as a fresh item
    assert provider.calls == []  # the deleted row is invisible to reconcile


def test_usage_is_accumulated():
    existing = [_item(id="a", title="Send the report")]
    run = _reconcile(FakeProvider([_verdict("DUPLICATE", candidate=1)]), [_item(id="b", title="Send the report")], existing)
    assert run.usage.input_tokens == 3 and run.usage.output_tokens == 1


# --- M3 acceptance criteria (reconcile + merge end-to-end) ------------------


def test_reingesting_the_same_meeting_adds_nothing():
    existing = [_item(id="a", title="Send the deck")]
    new = [_item(id="b", title="Send the deck")]
    run = _reconcile(FakeProvider([_verdict("DUPLICATE", candidate=1)]), new, existing)
    result = apply_decisions(existing, run.decisions)
    assert result.added == [] and result.updated == []
    assert [i.id for i in result.final] == ["a"]


def test_adding_a_due_date_is_an_update_not_a_duplicate():
    existing = [_item(id="a", title="Send the deck", due_date=None)]
    new = [_item(id="b", title="Send the deck", due_date=date(2026, 7, 3), due_date_text="by Friday")]
    run = _reconcile(FakeProvider([_verdict("UPDATE", candidate=1)]), new, existing)
    result = apply_decisions(existing, run.decisions)
    assert len(result.final) == 1  # not duplicated
    assert result.final[0].id == "a"  # patched the existing one
    assert result.final[0].due_date == date(2026, 7, 3)
    assert len(result.updated) == 1
