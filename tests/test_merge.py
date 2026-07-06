"""Merge-stage tests (pure; no provider, no I/O)."""

from __future__ import annotations

from datetime import date, datetime, timezone

from meeting_notes_todos.models import ActionItem
from meeting_notes_todos.pipeline.merge import apply_decisions
from meeting_notes_todos.pipeline.reconcile import Decision

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


def test_new_is_appended_in_order():
    existing = [_item(id="a", title="A"), _item(id="b", title="B")]
    new = _item(id="c", title="C")
    result = apply_decisions(existing, [Decision(item=new, label="NEW")])
    assert [i.id for i in result.final] == ["a", "b", "c"]
    assert result.added == [new]


def test_duplicate_is_skipped():
    existing = [_item(id="a", title="A")]
    new = _item(id="b", title="A")
    result = apply_decisions(existing, [Decision(item=new, label="DUPLICATE", target=existing[0])])
    assert [i.id for i in result.final] == ["a"]
    assert len(result.skipped) == 1


def test_update_fills_due_date_preserving_status_and_creation():
    existing = [_item(id="a", title="Send deck", status="doing", owner=None, due_date=None)]
    new = _item(
        id="b", title="Send deck", owner="Lei", due_date=date(2026, 7, 3), due_date_text="by Friday"
    )
    result = apply_decisions(existing, [Decision(item=new, label="UPDATE", target=existing[0])])

    assert len(result.updated) == 1
    patched = result.final[0]
    assert patched.id == "a"  # existing id preserved
    assert patched.status == "doing"  # status preserved
    assert patched.created_at == _TS  # creation preserved
    assert patched.owner == "Lei"  # filled
    assert patched.due_date == date(2026, 7, 3)  # filled
    assert patched.due_date_text == "by Friday"
    assert patched.updated_at > _TS  # bumped


def test_update_with_no_new_info_is_skipped():
    existing = [_item(id="a", title="X", owner="Lei", due_date=date(2026, 7, 3))]
    new = _item(id="b", title="X", owner="Lei", due_date=date(2026, 7, 3))
    result = apply_decisions(existing, [Decision(item=new, label="UPDATE", target=existing[0])])
    assert result.updated == []
    assert len(result.skipped) == 1
    assert result.final[0].id == "a"


def test_update_targeting_done_item_is_left_untouched():
    existing = [_item(id="a", title="X", status="done", due_date=None)]
    new = _item(id="b", title="X", due_date=date(2026, 7, 3), due_date_text="by Friday")
    result = apply_decisions(existing, [Decision(item=new, label="UPDATE", target=existing[0])])
    assert result.updated == []
    assert len(result.skipped) == 1
    assert result.final[0].due_date is None  # done item untouched
    assert result.final[0].status == "done"


def test_update_targeting_deleted_item_is_left_untouched():
    existing = [_item(id="a", title="X", status="deleted", due_date=None)]
    new = _item(id="b", title="X", due_date=date(2026, 7, 3), due_date_text="by Friday")
    result = apply_decisions(existing, [Decision(item=new, label="UPDATE", target=existing[0])])
    assert result.updated == []
    assert len(result.skipped) == 1
    assert result.final[0].status == "deleted"  # never auto-resurrected
    assert result.final[0].due_date is None
