"""Review-stage tests (interface-agnostic; no web, no provider)."""

from __future__ import annotations

from datetime import date, datetime, timezone

from meeting_notes_todos.models import ActionItem
from meeting_notes_todos.pipeline.reconcile import Decision
from meeting_notes_todos.review import ReviewedItem, build_proposals, commit_reviewed

_TS = datetime(2026, 6, 28, 12, 0, 0, tzinfo=timezone.utc)  # a stable past timestamp
MEETING = date(2026, 7, 1)  # Wednesday; Friday = 2026-07-03


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


# --- build_proposals --------------------------------------------------------


def test_build_proposals_labels_and_update_preview():
    existing = _item(id="e2", title="Send deck", owner=None, due_date=None)
    decisions = [
        Decision(item=_item(id="1", title="Brand new"), label="NEW"),
        Decision(item=_item(id="2", title="Dup"), label="DUPLICATE", target=_item(id="e", title="Existing")),
        Decision(
            item=_item(id="3", title="Send deck", owner="Lei", due_date=date(2026, 7, 3), due_date_text="by Friday"),
            label="UPDATE",
            target=existing,
        ),
    ]
    props = build_proposals(decisions)
    assert [p.kind for p in props] == ["new", "duplicate", "update"]
    update = props[2]
    assert update.item.due_date == date(2026, 7, 3)  # the filled preview
    assert any("due" in change for change in update.changes)
    assert update.target.id == "e2"


def test_proposals_carry_the_extraction_rationale():
    # for updates the proposal's item is the patched *target*, so the rationale
    # must still come from the newly extracted item (M7)
    target = _item(id="e", title="Send deck", owner=None)
    decisions = [
        Decision(item=_item(id="1", title="New", rationale="Soft commitment."), label="NEW"),
        Decision(item=_item(id="2", title="Send deck", owner="Lei", rationale="Adds an owner."),
                 label="UPDATE", target=target),
        Decision(item=_item(id="3", title="Dup", rationale="Same task re-mentioned."),
                 label="DUPLICATE", target=_item(id="e2", title="Existing")),
    ]
    props = build_proposals(decisions)
    assert [p.rationale for p in props] == [
        "Soft commitment.", "Adds an owner.", "Same task re-mentioned."
    ]
    assert props[1].item.id == "e"  # still the patched target


def test_commit_persists_the_rationale():
    reviewed = [
        ReviewedItem(action="add", title="New task", rationale="Explicit ask in the notes."),
    ]
    result = commit_reviewed([], reviewed, MEETING)
    assert result.final[0].rationale == "Explicit ask in the notes."

    # an update stores the latest rationale, and never wipes it with None
    existing = [_item(id="a", title="T", rationale="Original reading.")]
    updated = commit_reviewed(
        existing, [ReviewedItem(action="update", target_id="a", title="T", rationale="Newer reading.")], MEETING
    )
    assert updated.final[0].rationale == "Newer reading."
    kept = commit_reviewed(
        existing, [ReviewedItem(action="update", target_id="a", title="T2")], MEETING
    )
    assert kept.final[0].rationale == "Original reading."


def test_update_on_done_target_is_shown_as_duplicate():
    done = _item(id="e", title="X", status="done")
    props = build_proposals(
        [Decision(item=_item(id="1", title="X", due_date=date(2026, 7, 3)), label="UPDATE", target=done)]
    )
    assert props[0].kind == "duplicate"


# --- commit_reviewed (the M4 acceptance) ------------------------------------


def test_only_passed_items_are_written_and_hand_added_items_are_included():
    # The model proposed three, the user rejected one (not passed in) and added one.
    reviewed = [
        ReviewedItem(action="add", title="Send the report", owner="Lei", due_date_text="by Friday"),
        ReviewedItem(action="add", title="Ping the vendor"),  # added by hand
    ]
    result = commit_reviewed([], reviewed, MEETING)
    assert [i.title for i in result.final] == ["Send the report", "Ping the vendor"]
    assert result.final[0].due_date == date(2026, 7, 3)  # resolved in code from the edited phrase
    assert len(result.added) == 2


def test_update_overwrites_editable_fields_but_preserves_identity():
    existing = [_item(id="a", title="Send deck", status="doing", owner=None, due_date=None)]
    reviewed = [ReviewedItem(action="update", target_id="a", title="Send deck", owner="Lei", due_date_text="by Friday")]
    result = commit_reviewed(existing, reviewed, MEETING)
    patched = result.final[0]
    assert patched.id == "a" and patched.status == "doing" and patched.created_at == _TS
    assert patched.owner == "Lei" and patched.due_date == date(2026, 7, 3)
    assert patched.updated_at > _TS
    assert len(result.updated) == 1 and len(result.final) == 1


def test_update_targeting_done_item_is_skipped():
    existing = [_item(id="a", title="X", status="done")]
    reviewed = [ReviewedItem(action="update", target_id="a", title="X", due_date_text="by Friday")]
    result = commit_reviewed(existing, reviewed, MEETING)
    assert result.updated == [] and len(result.skipped) == 1
    assert result.final[0].due_date is None and result.final[0].status == "done"


def test_empty_title_is_skipped():
    result = commit_reviewed([], [ReviewedItem(action="add", title="   ")], MEETING)
    assert result.final == [] and len(result.skipped) == 1
