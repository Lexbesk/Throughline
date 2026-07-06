"""Apply user-accepted chat proposals to the item list (v2 §4.3; pure, no I/O).

Only ops the user explicitly accepted reach this function — that is the advisory
gate. Accepted values are authoritative (the user saw and could edit them), but
the v1 spine still holds: a done/cancelled/deleted item's status, id, and
creation date are never rewritten, and every id is re-validated against the live
list at commit time (the store may have changed since the proposal was staged).
DELETE is a *soft* delete: the item keeps its row, moves to status ``deleted``
(off the active view), and can be restored. The caller writes ``final`` back to
the store only after this returns.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel

from ..models import ActionItem
from ..pipeline.dates import resolve_due_date
from ..pipeline.merge import preview_patch

_FROZEN = ("done", "cancelled", "deleted")

_UPDATE_FIELDS = ("title", "description", "owner", "due_date_text", "project")


class ChatOp(BaseModel):
    """One accepted proposal, as posted back by the UI (possibly user-edited)."""

    op: Literal["new", "update", "delete", "merge", "reprioritize", "complete", "profile"]
    id: str | None = None  # target item (the kept item, for merge)
    absorb_id: str | None = None  # merge only: the duplicate folded in
    title: str | None = None
    description: str | None = None
    owner: str | None = None
    due_date_text: str | None = None
    project: str | None = None
    priority: Literal["high", "medium", "low", "none"] | None = None
    position: int | None = None
    section: str | None = None  # profile only: the named section to update (v3 M11)
    new_text: str | None = None  # profile only: complete replacement body for the section
    source_snippet: str | None = None  # new only: the pasted text the item came from (v3 M12)


@dataclass
class ChatApplyResult:
    applied: list[str] = field(default_factory=list)  # human-readable summaries
    skipped: list[tuple[str, str]] = field(default_factory=list)  # (summary, reason)
    final: list[ActionItem] = field(default_factory=list)


def apply_chat_ops(
    existing_items: list[ActionItem], ops: list[ChatOp], today: date | None = None
) -> ChatApplyResult:
    """Apply accepted ops in order; each op sees the result of the previous ones."""
    today = today or date.today()
    now = datetime.now(timezone.utc)
    by_id = {item.id: item for item in existing_items}
    order = [item.id for item in existing_items]
    result = ChatApplyResult()
    positions_dirty = False

    for op in ops:
        if op.op == "profile":
            # profile updates are written by the endpoint (profile.save_profile),
            # not through the item store — skip defensively if one lands here
            result.skipped.append(("profile", "not an item op — handled at the endpoint"))
            continue

        if op.op == "new":
            title = _clean(op.title)
            if not title:
                result.skipped.append(("new: (untitled)", "empty title"))
                continue
            due_text = _clean(op.due_date_text)
            item = ActionItem(
                id=uuid4().hex,
                title=title,
                description=_clean(op.description),
                status="todo",
                priority=op.priority if op.priority in ("high", "medium", "low") else None,
                owner=_clean(op.owner),
                due_date_text=due_text,
                due_date=resolve_due_date(due_text, today),
                source_meeting_id="chat",
                source_snippet=_clean(op.source_snippet) or title,
                created_at=now,
                updated_at=now,
            )
            by_id[item.id] = item
            order.append(item.id)
            result.applied.append(f'new: "{title}"')
            continue

        target, reason = _resolve(by_id, op.id, op.op)
        if target is None:
            result.skipped.append((f"{op.op}: id {op.id!r}", reason))
            continue

        if op.op == "update":
            updates: dict = {}
            for name in _UPDATE_FIELDS:
                value = _clean(getattr(op, name))
                if value is not None and value != getattr(target, name):
                    updates[name] = value
            if "due_date_text" in updates:
                updates["due_date"] = resolve_due_date(updates["due_date_text"], today)
            if not updates:
                result.skipped.append((f'update: "{target.title}"', "no effective change"))
                continue
            updates["updated_at"] = now
            by_id[target.id] = target.model_copy(update=updates)
            changed = [name for name in updates if name not in ("updated_at", "due_date")]
            result.applied.append(f'update: "{target.title}" ({", ".join(changed)})')

        elif op.op == "delete":
            # soft delete: the row stays in the store, hidden from the active view
            # and reversible from the Deleted list
            by_id[target.id] = target.model_copy(
                update={"status": "deleted", "updated_at": now}
            )
            result.applied.append(f'delete: "{target.title}" (moved to Deleted)')

        elif op.op == "merge":
            absorb, reason = _resolve(by_id, op.absorb_id, "merge absorb_id")
            if absorb is None:
                result.skipped.append((f'merge into "{target.title}"', reason))
                continue
            if absorb.id == target.id:
                result.skipped.append((f'merge into "{target.title}"', "keep and absorb are the same item"))
                continue
            merged, _ = preview_patch(target, absorb)  # fill-if-empty from the duplicate
            overrides: dict = {"updated_at": now}
            if _clean(op.title):
                overrides["title"] = _clean(op.title)
            if _clean(op.description):
                overrides["description"] = _clean(op.description)
            by_id[target.id] = merged.model_copy(update=overrides)
            del by_id[absorb.id]
            order.remove(absorb.id)
            result.applied.append(f'merge: "{absorb.title}" folded into "{target.title}"')

        elif op.op == "reprioritize":
            bits = []
            patched = target
            if op.priority is not None:
                patched = patched.model_copy(
                    update={
                        "priority": None if op.priority == "none" else op.priority,
                        "updated_at": now,
                    }
                )
                bits.append(f"priority → {op.priority}")
            by_id[target.id] = patched
            if op.position is not None:
                pos = max(0, min(int(op.position), len(order) - 1))
                order.remove(target.id)
                order.insert(pos, target.id)
                positions_dirty = True
                bits.append(f"position → {pos}")
            if not bits:
                result.skipped.append((f'reprioritize: "{target.title}"', "nothing to change"))
                continue
            result.applied.append(f'reprioritize: "{target.title}" ({", ".join(bits)})')

        elif op.op == "complete":
            by_id[target.id] = target.model_copy(update={"status": "done", "updated_at": now})
            result.applied.append(f'complete: "{target.title}"')

    if positions_dirty:  # keep positions dense and in list order, as the M6 reorder does
        by_id = {
            item_id: by_id[item_id].model_copy(update={"position": pos})
            for pos, item_id in enumerate(order)
        }

    result.final = [by_id[item_id] for item_id in order]
    return result


def _resolve(
    by_id: dict[str, ActionItem], item_id: str | None, op_name: str
) -> tuple[ActionItem | None, str | None]:
    target = by_id.get(item_id or "")
    if target is None:
        return None, "unknown or stale item id — not applied"
    if target.status in _FROZEN:
        return None, f"item is {target.status} — left untouched"
    return target, None


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    return value or None
