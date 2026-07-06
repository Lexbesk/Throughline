"""Select the (store, profile) pair for the configured backend (v4 M16).

- ``markdown`` (default): the v1–v3 local files — single-user, no database.
- ``postgres``: per-user rows in the database addressed by ``DATABASE_URL``.
  Until accounts land (M17), the web app and CLI act as one configurable
  default user (``THROUGHLINE_USER``, default ``"local"``); M17 replaces this
  with the logged-in session user.

Both backends sit behind the same thin interfaces (``Store``,
``ProfileBackend``), so everything above them — pipeline, assistant, UI — is
unchanged by the swap.
"""

from __future__ import annotations

import os

from .config import StoreConfig
from .profile import FileProfile, PostgresProfile, ProfileBackend
from .store import Store, build_store
from .store.postgres_store import PostgresStore

DEFAULT_USERNAME_ENV = "THROUGHLINE_USER"


def build_backends(store_config: StoreConfig) -> tuple[Store, ProfileBackend]:
    if store_config.backend == "postgres":
        from .db import ensure_user, get_pool

        pool = get_pool()
        user_id = ensure_user(pool, os.environ.get(DEFAULT_USERNAME_ENV, "local"))
        return PostgresStore(pool, user_id), PostgresProfile(pool, user_id)
    store = build_store(store_config)
    return store, FileProfile(store.path)
