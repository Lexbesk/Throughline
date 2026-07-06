"""Storage interface — a thin, swappable module (build plan §5)."""

from __future__ import annotations

from abc import ABC, abstractmethod

from ..models import ActionItem


class Store(ABC):
    """Read/write the TODO store. Markdown is the default; SQLite could swap in."""

    @abstractmethod
    def load(self) -> list[ActionItem]:
        """Return all stored items, in file order."""

    @abstractmethod
    def save(self, items: list[ActionItem]) -> None:
        """Write the given items as the full store contents."""

    def append(self, new_items: list[ActionItem]) -> list[ActionItem]:
        """Append items whose id isn't already stored; return the ones added.

        Content-level deduplication against existing tasks is M3 (reconcile);
        this only guards against re-appending the exact same ActionItem id.
        """
        existing = self.load()
        known = {item.id for item in existing}
        added = [item for item in new_items if item.id not in known]
        if added:
            self.save(existing + added)
        return added
