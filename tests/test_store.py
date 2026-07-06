"""Markdown store tests (key-free; round-trip, status, append, readability)."""

from __future__ import annotations

import base64
import json
from datetime import date, datetime, timezone

import pytest

from meeting_notes_todos.config import StoreConfig
from meeting_notes_todos.models import ActionItem
from meeting_notes_todos.store import MarkdownStore, build_store

_TS = datetime(2026, 6, 28, 12, 0, 0, tzinfo=timezone.utc)


def _item(**overrides) -> ActionItem:
    base = dict(
        id="id1",
        title="Send the Q3 deck",
        description=None,
        status="todo",
        owner=None,
        due_date=None,
        tags=[],
        project=None,
        source_meeting_id="m1",
        source_snippet="snippet",
        created_at=_TS,
        updated_at=_TS,
    )
    base.update(overrides)
    return ActionItem(**base)


def test_round_trip_preserves_all_fields(tmp_path):
    store = MarkdownStore(tmp_path / "todos.md")
    items = [
        _item(
            id="a",
            title="Send the Q3 deck",
            owner="Lei",
            due_date=date(2026, 6, 30),
            description="for the leadership review",
            tags=["sales", "q3"],
            project="roadmap",
        ),
        _item(id="b", title="Renew the SSL cert", status="done"),
        _item(id="c", title="Investigate latency", status="doing", owner="Marcus"),
        _item(id="d", title="Prep the offsite", priority="high", position=3),
        _item(id="e", title="Old clutter", status="deleted"),
        _item(id="f", title="Write the summary", rationale="First-person commitment."),
    ]
    store.save(items)
    assert store.load() == items


def test_status_comes_from_checkbox_glyph(tmp_path):
    path = tmp_path / "todos.md"
    store = MarkdownStore(path)
    store.save([_item(id="a", title="Task", status="todo")])

    # a human checks the box by hand
    path.write_text(path.read_text().replace("- [ ] Task", "- [x] Task"))

    assert store.load()[0].status == "done"


def test_append_skips_existing_ids_and_persists(tmp_path):
    store = MarkdownStore(tmp_path / "todos.md")
    store.save([_item(id="a", title="A")])

    added = store.append([_item(id="a", title="A again"), _item(id="b", title="B")])

    assert [it.id for it in added] == ["b"]  # existing id skipped
    assert [it.id for it in store.load()] == ["a", "b"]  # persisted union, in order


def test_file_is_human_readable(tmp_path):
    path = tmp_path / "todos.md"
    MarkdownStore(path).save(
        [_item(id="a", title="Send the Q3 deck", owner="Lei",
               due_date=date(2026, 6, 30), priority="high")]
    )
    text = path.read_text()
    assert "# TODOs" in text
    assert "- [ ] Send the Q3 deck" in text
    assert "@Lei" in text
    assert "!high" in text
    assert "due 2026-06-30" in text


def test_v1_lines_without_priority_fields_still_parse(tmp_path):
    # a payload written before M6 added priority/position
    data = dict(
        id="old1", title="Old task", description=None, owner=None,
        due_date=None, due_date_text=None, tags=[], project=None,
        source_meeting_id="m1", source_snippet="s",
        created_at="2026-06-28T12:00:00Z", updated_at="2026-06-28T12:00:00Z",
    )
    payload = base64.b64encode(json.dumps(data).encode("utf-8")).decode("ascii")
    path = tmp_path / "todos.md"
    path.write_text(f"- [ ] Old task  <!-- mnt:{payload} -->\n", encoding="utf-8")

    (item,) = MarkdownStore(path).load()
    assert item.title == "Old task"
    assert item.priority is None and item.position is None


def test_output_is_deterministic_and_stable(tmp_path):
    items = [_item(id="a", title="A", tags=["y", "x"]), _item(id="b", title="B")]
    p1, p2 = tmp_path / "a.md", tmp_path / "b.md"
    MarkdownStore(p1).save(items)
    MarkdownStore(p2).save(items)
    assert p1.read_text() == p2.read_text()

    # re-saving the loaded items is byte-stable (clean git diffs)
    before = p1.read_text()
    store = MarkdownStore(p1)
    store.save(store.load())
    assert p1.read_text() == before


def test_missing_file_loads_empty(tmp_path):
    assert MarkdownStore(tmp_path / "nope.md").load() == []


def test_save_empty_then_load(tmp_path):
    store = MarkdownStore(tmp_path / "todos.md")
    store.save([])
    assert store.load() == []


def test_freeform_lines_are_ignored(tmp_path):
    path = tmp_path / "todos.md"
    store = MarkdownStore(path)
    store.save([_item(id="a", title="Real")])
    with path.open("a", encoding="utf-8") as f:
        f.write("\n## My notes\n- [ ] a hand-written todo without metadata\n")

    assert [it.id for it in store.load()] == ["a"]


def test_build_store_factory(tmp_path):
    assert isinstance(build_store(StoreConfig(path=tmp_path / "t.md")), MarkdownStore)
    with pytest.raises(NotImplementedError):
        build_store(StoreConfig(backend="sqlite"))
    with pytest.raises(ValueError):
        build_store(StoreConfig(backend="weird"))
