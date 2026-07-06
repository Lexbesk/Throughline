"""Postgres store/profile tests (v4 M16) — run against the local Docker Postgres
(``deploy/docker-compose.yml``); skipped automatically when no DB is reachable
so the suite stays green anywhere.

The heart of this file is the hard-isolation acceptance: a query for one user
can never return another user's data.
"""

from __future__ import annotations

import os
from datetime import date, datetime, timezone
from uuid import uuid4

import psycopg
import pytest

from meeting_notes_todos.db.connection import DEFAULT_DEV_URL, ensure_user, get_pool
from meeting_notes_todos.models import ActionItem
from meeting_notes_todos.profile import PostgresProfile, section_body
from meeting_notes_todos.store.postgres_store import PostgresStore

DB_URL = os.environ.get("TEST_DATABASE_URL", DEFAULT_DEV_URL)


def _db_available() -> bool:
    try:
        with psycopg.connect(DB_URL, connect_timeout=2):
            return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _db_available(),
    reason="no local Postgres — start it with: docker compose -f deploy/docker-compose.yml up -d",
)

_TS = datetime(2026, 7, 6, 12, 0, 0, tzinfo=timezone.utc)


def _item(item_id: str = "a", title: str = "T", **overrides) -> ActionItem:
    base = dict(
        id=item_id,
        title=title,
        source_meeting_id="m",
        source_snippet="s",
        created_at=_TS,
        updated_at=_TS,
    )
    base.update(overrides)
    return ActionItem(**base)


@pytest.fixture()
def pool():
    return get_pool(DB_URL)


@pytest.fixture()
def two_users(pool):
    """Two throwaway users, removed (with all their rows, via cascade) afterwards."""
    ids = [ensure_user(pool, f"test-{uuid4().hex[:12]}") for _ in range(2)]
    yield ids
    with pool.connection() as conn:
        conn.execute("DELETE FROM users WHERE id = ANY(%s)", (ids,))


def test_round_trip_preserves_all_fields(pool, two_users):
    user_a, _ = two_users
    store = PostgresStore(pool, user_a)
    items = [
        _item("a", "Send the Q3 deck", owner="Lei", due_date=date(2026, 7, 10),
              due_date_text="by Friday", description="for review", tags=["q3"],
              project="roadmap", priority="high", position=0,
              rationale="Named owner and deadline."),
        _item("b", "Renew the SSL cert", status="done"),
        _item("c", "Old clutter", status="deleted"),
    ]
    store.save(items)
    assert store.load() == items  # exact, including status/priority/rationale


def test_save_replaces_and_preserves_order(pool, two_users):
    user_a, _ = two_users
    store = PostgresStore(pool, user_a)
    store.save([_item(i, i.upper()) for i in ("a", "b", "c")])
    store.save([_item(i, i.upper()) for i in ("c", "a")])  # reorder + drop
    assert [it.id for it in store.load()] == ["c", "a"]
    store.save([])
    assert store.load() == []


def test_append_skips_existing_ids(pool, two_users):
    user_a, _ = two_users
    store = PostgresStore(pool, user_a)
    store.save([_item("a", "A")])
    added = store.append([_item("a", "A again"), _item("b", "B")])
    assert [it.id for it in added] == ["b"]
    assert [it.id for it in store.load()] == ["a", "b"]


def test_hard_isolation_between_users(pool, two_users):
    """The M16 acceptance: one user's queries can never return another's data."""
    user_a, user_b = two_users
    store_a, store_b = PostgresStore(pool, user_a), PostgresStore(pool, user_b)
    prof_a, prof_b = PostgresProfile(pool, user_a), PostgresProfile(pool, user_b)

    store_a.save([_item("a1", "A's task"), _item("a2", "A's other task")])
    store_b.save([_item("b1", "B's task")])
    prof_a.save("## Long-term goals\n\nA's goals.")
    prof_b.save("## Long-term goals\n\nB's goals.")

    assert [it.title for it in store_a.load()] == ["A's task", "A's other task"]
    assert [it.title for it in store_b.load()] == ["B's task"]
    assert "A's goals" in prof_a.load() and "B's goals" not in prof_a.load()
    assert "B's goals" in prof_b.load() and "A's goals" not in prof_b.load()

    # writes are isolated too: A replacing everything touches zero B rows
    store_a.save([])
    prof_a.update_section("Long-term goals", "A changed course.")
    assert [it.title for it in store_b.load()] == ["B's task"]
    assert section_body(prof_b.load(), "Long-term goals") == "B's goals."

    # and the same item id may exist for both users without collision
    store_a.save([_item("shared-id", "A's copy")])
    store_b.save([_item("shared-id", "B's copy")])
    assert store_a.load()[0].title == "A's copy"
    assert store_b.load()[0].title == "B's copy"


def test_profile_round_trip_and_section_update(pool, two_users):
    user_a, _ = two_users
    prof = PostgresProfile(pool, user_a)
    assert prof.load() is None  # fresh user → no profile yet

    prof.update_section("Long-term goals", "Ship the eval platform.")
    text = prof.load()
    assert section_body(text, "Long-term goals") == "Ship the eval platform."
    assert "## Context & constraints" in text  # fresh profile got the seeded template

    prof.update_section("Current focus", "The v4 hosted build.")
    text = prof.load()
    assert section_body(text, "Long-term goals") == "Ship the eval platform."  # untouched
    assert section_body(text, "Current focus") == "The v4 hosted build."


def test_deleting_a_user_cascades_only_their_rows(pool, two_users):
    user_a, user_b = two_users
    PostgresStore(pool, user_a).save([_item("a1", "A's task")])
    PostgresStore(pool, user_b).save([_item("b1", "B's task")])
    with pool.connection() as conn:
        conn.execute("DELETE FROM users WHERE id = %s", (user_a,))
        remaining = conn.execute(
            "SELECT user_id, count(*) FROM todo_items"
            " WHERE user_id = ANY(%s) GROUP BY user_id",
            ([user_a, user_b],),
        ).fetchall()
    assert remaining == [(user_b, 1)]
