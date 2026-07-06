"""Merge stage (build plan §3, step 6).

Apply reconcile decisions to the store: NEW → append, UPDATE → patch the existing
item (fill-if-empty; preserve status and creation date), DUPLICATE → skip. Items
already marked done, cancelled, or deleted are never modified or resurrected.

``apply_decisions`` is pure (no I/O), so the caller can preview the result and only
write it on confirmation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from ..models import ActionItem
from .reconcile import Decision

_FROZEN = ("done", "cancelled", "deleted")


@dataclass
class MergeResult:
    added: list[ActionItem] = field(default_factory=list)
    updated: list[tuple[ActionItem, list[str]]] = field(default_factory=list)  # (patched, changes)
    skipped: list[tuple[ActionItem, str]] = field(default_factory=list)  # (new item, reason)
    final: list[ActionItem] = field(default_factory=list)


def apply_decisions(
    existing_items: list[ActionItem], decisions: list[Decision]
) -> MergeResult:
    by_id = {item.id: item for item in existing_items}
    order = [item.id for item in existing_items]
    result = MergeResult()

    for decision in decisions:
        if decision.label == "NEW":
            _append(result, by_id, order, decision.item)

        elif decision.label == "DUPLICATE":
            target = decision.target.title if decision.target else "an existing item"
            result.skipped.append((decision.item, f"duplicate of {target}"))

        elif decision.label == "UPDATE":
            target = by_id.get(decision.target.id) if decision.target else None
            if target is None:
                # the matched item vanished — safest is to add the new one
                _append(result, by_id, order, decision.item)
            elif target.status in _FROZEN:
                result.skipped.append(
                    (
                        decision.item,
                        f"matches a {target.status} task ({target.title}) — left untouched",
                    )
                )
            else:
                patched, changes = preview_patch(target, decision.item)
                if changes:
                    by_id[target.id] = patched
                    result.updated.append((patched, changes))
                else:
                    result.skipped.append((decision.item, f"no new info vs {target.title}"))

    result.final = [by_id[item_id] for item_id in order]
    return result


def _append(result: MergeResult, by_id: dict, order: list, item: ActionItem) -> None:
    by_id[item.id] = item
    order.append(item.id)
    result.added.append(item)


def preview_patch(existing: ActionItem, new: ActionItem) -> tuple[ActionItem, list[str]]:
    """Fill fields the existing item lacks; never overwrite, never change status.

    Used by the automatic merge and by the review stage to preview what an UPDATE
    would fill.
    """
    updates: dict = {}
    changes: list[str] = []

    if existing.owner is None and new.owner:
        updates["owner"] = new.owner
        changes.append(f"owner: {new.owner}")
    if existing.description is None and new.description:
        updates["description"] = new.description
        changes.append("description")
    if existing.project is None and new.project:
        updates["project"] = new.project
        changes.append(f"project: {new.project}")
    if existing.due_date is None and new.due_date is not None:
        updates["due_date"] = new.due_date
        changes.append(f"due: {new.due_date.isoformat()}")
        if new.due_date_text:
            updates["due_date_text"] = new.due_date_text
    elif not existing.due_date_text and new.due_date_text:
        updates["due_date_text"] = new.due_date_text
        changes.append(f'due phrase: "{new.due_date_text}"')

    if not changes:
        return existing, []
    updates["updated_at"] = datetime.now(timezone.utc)
    return existing.model_copy(update=updates), changes
