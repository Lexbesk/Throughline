"""Review stage (build plan §3 step 5, §7).

Interface-agnostic review logic: turn reconcile decisions into human-facing
proposals, and commit the user's reviewed choices into a final item list. The web
UI (or any other front end) drives these — nothing here imports a web framework,
and nothing is persisted here (the caller writes the returned ``final`` list), so
"nothing is committed until the user confirms" is preserved.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Literal
from uuid import uuid4

from .models import ActionItem
from .pipeline.dates import resolve_due_date
from .pipeline.merge import preview_patch
from .pipeline.reconcile import Decision

_FROZEN = ("done", "cancelled", "deleted")


@dataclass
class Proposal:
    kind: Literal["new", "update", "duplicate"]
    item: ActionItem  # the item as it would land (already patched, for updates)
    target: ActionItem | None = None  # matched existing item (update / duplicate)
    changes: list[str] = field(default_factory=list)  # for update: fields that get filled
    reason: str | None = None  # the reconcile verdict's reason (why matched/excluded)
    rationale: str | None = None  # the extraction's stated rationale for the new item (M7)


@dataclass
class ReviewedItem:
    action: Literal["add", "update"]
    title: str
    owner: str | None = None
    due_date_text: str | None = None
    description: str | None = None
    project: str | None = None
    target_id: str | None = None
    id: str | None = None
    source_snippet: str | None = None
    source_meeting_id: str | None = None
    rationale: str | None = None  # passed through from the proposal, not user-edited (M7)


@dataclass
class CommitResult:
    added: list[ActionItem] = field(default_factory=list)
    updated: list[ActionItem] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)  # (title, reason)
    final: list[ActionItem] = field(default_factory=list)


def build_proposals(decisions: list[Decision]) -> list[Proposal]:
    """Turn reconcile decisions into review proposals (UPDATE shows the filled result)."""
    proposals: list[Proposal] = []
    for decision in decisions:
        # the extraction rationale always belongs to the newly extracted item, even
        # when the proposal's `item` is the patched existing one (updates)
        rationale = decision.item.rationale
        if decision.label == "NEW" or (decision.label == "UPDATE" and decision.target is None):
            proposals.append(
                Proposal(kind="new", item=decision.item, reason=decision.reason, rationale=rationale)
            )
        elif decision.label == "DUPLICATE":
            reason = decision.reason or (
                f"duplicate of {decision.target.title}" if decision.target else "duplicate"
            )
            proposals.append(
                Proposal(kind="duplicate", item=decision.item, target=decision.target,
                         reason=reason, rationale=rationale)
            )
        else:  # UPDATE with a target
            target = decision.target
            if target.status in _FROZEN:
                proposals.append(
                    Proposal(
                        kind="duplicate",
                        item=decision.item,
                        target=target,
                        reason=f"matches a {target.status} task — left untouched",
                        rationale=rationale,
                    )
                )
            else:
                patched, changes = preview_patch(target, decision.item)
                proposals.append(
                    Proposal(kind="update", item=patched, target=target, changes=changes,
                             reason=decision.reason, rationale=rationale)
                )
    return proposals


def commit_reviewed(
    existing_items: list[ActionItem],
    reviewed: list[ReviewedItem],
    meeting_date: date,
) -> CommitResult:
    """Apply the user's reviewed choices. Rejected items are simply not passed in.

    Reviewed values are authoritative (the user saw and could edit them), so an
    UPDATE overwrites the target's editable fields — but never its status, id, or
    creation date, and never touches a done/cancelled item.
    """
    now = datetime.now(timezone.utc)
    by_id = {item.id: item for item in existing_items}
    order = [item.id for item in existing_items]
    result = CommitResult()

    for entry in reviewed:
        title = (entry.title or "").strip()
        if not title:
            result.skipped.append(("(untitled)", "empty title"))
            continue
        due_date = resolve_due_date(entry.due_date_text, meeting_date)

        if entry.action == "update":
            target = by_id.get(entry.target_id) if entry.target_id else None
            if target is None:
                result.skipped.append((title, "target item not found"))
                continue
            if target.status in _FROZEN:
                result.skipped.append((title, f"target is {target.status} — left untouched"))
                continue
            updates = {
                "title": title,
                "owner": _clean(entry.owner),
                "due_date": due_date,
                "due_date_text": _clean(entry.due_date_text),
                "description": _clean(entry.description),
                "project": _clean(entry.project),
                "updated_at": now,
            }
            if _clean(entry.rationale):  # keep the latest rationale; never wipe with None
                updates["rationale"] = _clean(entry.rationale)
            patched = target.model_copy(update=updates)
            by_id[target.id] = patched
            result.updated.append(patched)
        else:  # add
            item_id = entry.id or uuid4().hex
            item = ActionItem(
                id=item_id,
                title=title,
                description=_clean(entry.description),
                status="todo",
                owner=_clean(entry.owner),
                due_date_text=_clean(entry.due_date_text),
                due_date=due_date,
                tags=[],
                project=_clean(entry.project),
                rationale=_clean(entry.rationale),
                source_meeting_id=entry.source_meeting_id or "manual-review",
                source_snippet=entry.source_snippet or title,
                created_at=now,
                updated_at=now,
            )
            if item_id not in by_id:
                order.append(item_id)
            by_id[item_id] = item
            result.added.append(item)

    result.final = [by_id[item_id] for item_id in order]
    return result


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    return value or None
