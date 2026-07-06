"""Token-usage logging tests (key-free)."""

from __future__ import annotations

from meeting_notes_todos.providers.base import Usage
from meeting_notes_todos.usage import record_run, summarize


def test_record_and_summarize(tmp_path):
    path = tmp_path / "usage.jsonl"
    record_run(path, command="extract", provider="anthropic", model="claude-haiku-4-5", usage=Usage(10, 5))
    record_run(path, command="ingest", provider="anthropic", model="claude-haiku-4-5", usage=Usage(20, 7))
    record_run(path, command="web-review", provider="local", model="llama3.1", usage=Usage(3, 1))

    summary = summarize(path)
    assert summary["runs"] == 3
    assert summary["input_tokens"] == 33 and summary["output_tokens"] == 13
    assert summary["by_model"]["claude-haiku-4-5"]["runs"] == 2
    assert summary["by_model"]["claude-haiku-4-5"]["input_tokens"] == 30
    assert summary["by_model"]["llama3.1"]["output_tokens"] == 1


def test_summarize_missing_file(tmp_path):
    assert summarize(tmp_path / "nope.jsonl")["runs"] == 0
