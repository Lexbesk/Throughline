"""Human-readable, git-friendly markdown store (build plan §5, default backend).

Each item is one checkbox line: the visible text is for humans, and the trailing
``<!-- mnt:… -->`` comment carries the canonical ActionItem (base64-encoded JSON)
for lossless round-tripping. Status is taken from the checkbox glyph, so a human
can change an item's status simply by toggling the box.
"""

from __future__ import annotations

import base64
import json
import re
from pathlib import Path

from ..models import ActionItem, Status
from .base import Store

_GLYPH_BY_STATUS: dict[str, str] = {
    "todo": " ",
    "doing": "/",
    "done": "x",
    "cancelled": "-",
    "deleted": "~",
}
_STATUS_BY_GLYPH: dict[str, Status] = {
    " ": "todo",
    "/": "doing",
    "x": "done",
    "X": "done",
    "-": "cancelled",
    "~": "deleted",
}

_LINE_RE = re.compile(
    r"^- \[(?P<glyph>.)\]\s.*?<!-- mnt:(?P<payload>[A-Za-z0-9+/=]+) -->\s*$"
)

_HEADER = (
    "# TODOs\n\n"
    "<!-- Managed by meeting-notes-todos. The checkbox line is human-readable; the\n"
    "     trailing HTML comment on each item carries the canonical data. Toggle the\n"
    "     checkbox to change an item's status; delete a line to remove an item. -->\n\n"
)


class MarkdownStore(Store):
    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)

    @property
    def path(self) -> Path:
        return self._path

    def load(self) -> list[ActionItem]:
        if not self._path.exists():
            return []
        items: list[ActionItem] = []
        for line in self._path.read_text(encoding="utf-8").splitlines():
            item = _parse_line(line)
            if item is not None:
                items.append(item)
        return items

    def save(self, items: list[ActionItem]) -> None:
        body = "\n".join(_render_line(item) for item in items)
        contents = _HEADER + (body + "\n" if body else "_No action items yet._\n")
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(contents, encoding="utf-8")


def _render_line(item: ActionItem) -> str:
    glyph = _GLYPH_BY_STATUS.get(item.status, " ")
    parts: list[str] = [item.title]
    if item.owner:
        parts.append(f"@{item.owner}")
    if item.priority:
        parts.append(f"!{item.priority}")
    if item.due_date:
        parts.append(f"due {item.due_date.isoformat()}")
    elif item.due_date_text:
        parts.append(f'due "{item.due_date_text}" (unresolved)')
    if item.project:
        parts.append(f"#{item.project}")
    parts.extend(f"~{tag}" for tag in item.tags)
    visible = "  ·  ".join(parts)
    return f"- [{glyph}] {visible}  <!-- mnt:{_encode(item)} -->"


def _encode(item: ActionItem) -> str:
    data = item.model_dump(mode="json")
    data.pop("status", None)  # status is carried by the checkbox glyph
    raw = json.dumps(data, separators=(",", ":"), sort_keys=True)
    return base64.b64encode(raw.encode("utf-8")).decode("ascii")


def _parse_line(line: str) -> ActionItem | None:
    match = _LINE_RE.match(line.rstrip())
    if match is None:
        return None
    data = json.loads(base64.b64decode(match.group("payload")).decode("utf-8"))
    data["status"] = _STATUS_BY_GLYPH.get(match.group("glyph"), "todo")
    return ActionItem.model_validate(data)
