"""Storage layer (build plan §5)."""

from __future__ import annotations

from ..config import StoreConfig
from .base import Store
from .markdown_store import MarkdownStore

__all__ = ["Store", "MarkdownStore", "build_store"]


def build_store(store_config: StoreConfig) -> Store:
    """Return the store implementation named by config (markdown by default)."""
    if store_config.backend == "markdown":
        return MarkdownStore(store_config.path)
    if store_config.backend == "sqlite":
        raise NotImplementedError("A SQLite store is a future option (build plan §5).")
    raise ValueError(f"Unknown store backend: {store_config.backend!r}")
