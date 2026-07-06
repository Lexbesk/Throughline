"""Per-user, Postgres-backed store (v4 M16).

Same thin interface as the markdown store — ``load()`` returns every item in
order, ``save()`` replaces the full store contents — so the pipeline, assistant,
and UI logic are unchanged (the payoff of keeping the store thin since v1).

Every query is scoped by ``user_id``: this class can only ever see or write the
rows of the user it was constructed for. That is the hard-isolation rule (v4
plan §6) — cross-user access isn't a code path that exists.

Each item is one JSONB payload (the canonical ActionItem, exactly like the
markdown store's ``mnt:`` comment) plus an explicit ``position`` carrying the
list order that the file's line order used to carry. ``save()`` replaces the
user's rows in a single transaction.
"""

from __future__ import annotations

from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from ..models import ActionItem
from .base import Store


class PostgresStore(Store):
    def __init__(self, pool: ConnectionPool, user_id: str) -> None:
        self.pool = pool
        self.user_id = user_id

    def load(self) -> list[ActionItem]:
        with self.pool.connection() as conn:
            rows = conn.execute(
                "SELECT payload FROM todo_items WHERE user_id = %s ORDER BY position",
                (self.user_id,),
            ).fetchall()
        return [ActionItem.model_validate(row[0]) for row in rows]

    def save(self, items: list[ActionItem]) -> None:
        with self.pool.connection() as conn:  # one transaction: replace-all
            conn.execute(
                "DELETE FROM todo_items WHERE user_id = %s", (self.user_id,)
            )
            with conn.cursor() as cur:
                cur.executemany(
                    "INSERT INTO todo_items (user_id, item_id, position, payload)"
                    " VALUES (%s, %s, %s, %s)",
                    [
                        (self.user_id, item.id, position, Jsonb(item.model_dump(mode="json")))
                        for position, item in enumerate(items)
                    ],
                )
