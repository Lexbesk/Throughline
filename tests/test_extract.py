"""Extract + validate pipeline tests (key-free, via a scripted fake provider)."""

from __future__ import annotations

import json
from datetime import date

import pytest

from meeting_notes_todos.config import PromptsConfig
from meeting_notes_todos.models import ExtractedItem, ExtractionResponse
from meeting_notes_todos.pipeline.extract import ExtractionError, extract_action_items
from meeting_notes_todos.providers.base import CompletionResult, LLMProvider, Usage

PROMPTS = PromptsConfig()  # reads prompts/ from the repo root (pytest cwd)
MEETING_DATE = date(2026, 6, 28)  # Sunday; July 3 = Fri


class FakeProvider(LLMProvider):
    """Returns pre-scripted CompletionResults in order; records each call."""

    def __init__(self, scripted: list[CompletionResult]) -> None:
        self._scripted = list(scripted)
        self.calls: list[dict] = []

    def complete(self, *, system_prompt, user_content, max_tokens=None, response_schema=None):
        self.calls.append({"user": user_content, "schema": response_schema})
        return self._scripted.pop(0)


def _run(provider: FakeProvider):
    return extract_action_items(
        provider=provider,
        prompts=PROMPTS,
        notes="some notes",
        meeting_date=MEETING_DATE,
        meeting_id="m1",
    )


def test_native_parsed_happy_path_resolves_date_in_code():
    parsed = ExtractionResponse(
        items=[
            ExtractedItem(
                title="Send the benchmark results",
                owner="Lei",
                due_date_text="by Friday",
                source_snippet="Lei needs to send the benchmark results by Friday",
                rationale="Direct commitment with a named owner and deadline.",
            )
        ]
    )
    provider = FakeProvider([CompletionResult(text="", usage=Usage(10, 5), parsed=parsed)])

    run = _run(provider)

    assert len(run.items) == 1
    item = run.items[0]
    assert item.title == "Send the benchmark results"
    assert item.owner == "Lei"
    assert item.rationale == "Direct commitment with a named owner and deadline."  # M7
    assert item.due_date_text == "by Friday"  # raw phrase retained
    assert item.due_date == date(2026, 7, 3)  # resolved deterministically in code
    assert item.status == "todo"
    assert item.source_meeting_id == "m1"
    assert run.usage.input_tokens == 10
    assert provider.calls[0]["schema"] is ExtractionResponse


def test_vague_phrase_leaves_due_date_empty_but_keeps_text():
    parsed = ExtractionResponse(
        items=[
            ExtractedItem(
                title="Refactor the data loader",
                owner="Austin",
                due_date_text="this week",
                source_snippet="Austin will refactor the data loader this week",
            )
        ]
    )
    provider = FakeProvider([CompletionResult(text="", usage=Usage(0, 0), parsed=parsed)])

    item = _run(provider).items[0]

    assert item.due_date is None  # vague -> not guessed
    assert item.due_date_text == "this week"  # phrase kept as a display fallback


def test_unowned_item_is_kept_with_empty_owner():
    parsed = ExtractionResponse(
        items=[
            ExtractedItem(
                title="Update the API docs",
                owner=None,
                source_snippet="We should update the API docs — nobody owns this yet",
            )
        ]
    )
    provider = FakeProvider([CompletionResult(text="", usage=Usage(0, 0), parsed=parsed)])

    item = _run(provider).items[0]

    assert item.title == "Update the API docs"
    assert item.owner is None
    assert item.due_date is None and item.due_date_text is None


def test_repair_retry_recovers_from_malformed_output():
    good = json.dumps(
        {
            "items": [
                {
                    "title": "Renew the SSL cert",
                    "due_date_text": "July 3",
                    "source_snippet": "renew the SSL cert before July 3",
                }
            ]
        }
    )
    provider = FakeProvider(
        [
            CompletionResult(text="not json at all", usage=Usage(8, 2), parsed=None),
            CompletionResult(text=good, usage=Usage(9, 4), parsed=None),
        ]
    )

    run = _run(provider)

    assert [i.title for i in run.items] == ["Renew the SSL cert"]
    assert run.items[0].due_date == date(2026, 7, 3)
    assert len(provider.calls) == 2
    assert provider.calls[0]["schema"] is ExtractionResponse  # first call: native schema
    assert provider.calls[1]["schema"] is None  # repair: plain text mode
    assert run.usage.input_tokens == 17  # usage aggregated across both calls


def test_repair_exhaustion_raises():
    provider = FakeProvider(
        [
            CompletionResult(text="nope", usage=Usage(1, 1), parsed=None),
            CompletionResult(text="still nope", usage=Usage(1, 1), parsed=None),
        ]
    )
    with pytest.raises(ExtractionError):
        _run(provider)
    assert len(provider.calls) == 2  # exactly one repair attempt, then give up


def test_within_batch_duplicates_collapsed():
    parsed = ExtractionResponse(
        items=[
            ExtractedItem(title="Refactor the data loader", source_snippet="a"),
            ExtractedItem(title="refactor the data loader.", source_snippet="b"),  # same
            ExtractedItem(title="Renew the SSL cert", source_snippet="c"),
        ]
    )
    provider = FakeProvider([CompletionResult(text="", usage=Usage(0, 0), parsed=parsed)])

    run = _run(provider)

    assert [i.title for i in run.items] == ["Refactor the data loader", "Renew the SSL cert"]


def test_profile_is_injected_into_the_extraction_prompt():
    provider = FakeProvider(
        [CompletionResult(text="", usage=Usage(0, 0), parsed=ExtractionResponse(items=[]))]
    )
    extract_action_items(
        provider=provider, prompts=PROMPTS, notes="some notes",
        meeting_date=MEETING_DATE, meeting_id="m1",
        profile="Name: Austin. Role: eval lead on project Atlas.",
    )
    assert "Role: eval lead on project Atlas" in provider.calls[0]["user"]


def test_missing_profile_uses_the_placeholder():
    provider = FakeProvider(
        [CompletionResult(text="", usage=Usage(0, 0), parsed=ExtractionResponse(items=[]))]
    )
    _run(provider)
    assert "(no profile provided)" in provider.calls[0]["user"]


def test_code_fenced_json_is_parsed_without_repair():
    fenced = '```json\n{"items": []}\n```'
    provider = FakeProvider([CompletionResult(text=fenced, usage=Usage(0, 0), parsed=None)])

    run = _run(provider)

    assert run.items == []
    assert len(provider.calls) == 1  # fenced-but-valid parsed on the first try
