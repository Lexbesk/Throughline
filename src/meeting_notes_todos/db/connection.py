"""Postgres connection + schema (v4 M16).

The database is addressed by a standard ``DATABASE_URL`` (``.env`` in dev —
pointing at the local Docker Postgres from ``deploy/docker-compose.yml`` — and
platform secrets in production, chosen at M19). Plain SQL through psycopg, no
ORM: the schema is three small tables, and keeping access standard avoids
platform lock-in (v4 plan §14).

Schema notes:
- ``users`` exists from M16 so every row can be scoped by ``user_id`` (the
  cardinal isolation rule); ``password_hash`` stays empty until M17's login.
- ``todo_items`` stores each ActionItem as a JSONB payload plus an explicit
  ``position`` — the database analog of the markdown store's line order — so
  the whole-list ``load()/save()`` semantics carry over unchanged.
- ``profiles`` stores the profile as one markdown document per user; all the
  v3 section machinery operates on the text, unchanged.
"""

from __future__ import annotations

import atexit
import os
from uuid import uuid4

from psycopg_pool import ConnectionPool

DEFAULT_DEV_URL = "postgresql://throughline:throughline@localhost:5432/throughline"

_SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS users (
        id TEXT PRIMARY KEY,
        username TEXT NOT NULL UNIQUE,
        password_hash TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    # v4: the user's selected model tier persists in the DB (not in memory), so
    # it survives restarts and works across machines. Idempotent for existing rows.
    """
    ALTER TABLE users ADD COLUMN IF NOT EXISTS model_tier TEXT
    """,
    """
    CREATE TABLE IF NOT EXISTS todo_items (
        user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        item_id TEXT NOT NULL,
        position INTEGER NOT NULL,
        payload JSONB NOT NULL,
        PRIMARY KEY (user_id, item_id)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS todo_items_user_order
        ON todo_items (user_id, position)
    """,
    """
    CREATE TABLE IF NOT EXISTS profiles (
        user_id TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
        content TEXT NOT NULL,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    # v4 M17: server-side sessions. The cookie carries a random opaque token;
    # only its hash is stored, so a database leak yields no usable sessions.
    """
    CREATE TABLE IF NOT EXISTS sessions (
        token_hash TEXT PRIMARY KEY,
        user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        expires_at TIMESTAMPTZ NOT NULL
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS sessions_user ON sessions (user_id)
    """,
    # v4 M18: per-user, per-provider API keys. Only the ciphertext is stored;
    # last4 is kept plaintext purely for masked display (never decrypt to list).
    """
    CREATE TABLE IF NOT EXISTS user_api_keys (
        user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        provider TEXT NOT NULL,
        encrypted_key TEXT NOT NULL,
        last4 TEXT NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        PRIMARY KEY (user_id, provider)
    )
    """,
)

_POOLS: dict[str, ConnectionPool] = {}


def database_url() -> str:
    return os.environ.get("DATABASE_URL", DEFAULT_DEV_URL)


def get_pool(url: str | None = None) -> ConnectionPool:
    """A process-wide pool per URL; the schema is ensured (idempotently) once."""
    url = url or database_url()
    pool = _POOLS.get(url)
    if pool is None:
        pool = ConnectionPool(url, min_size=1, max_size=5, open=True)
        atexit.register(pool.close)  # clean shutdown (matters for one-shot CLI runs)
        with pool.connection() as conn:
            for statement in _SCHEMA_STATEMENTS:
                conn.execute(statement)
        _POOLS[url] = pool
    return pool


def get_user_tier(pool: ConnectionPool, user_id: str) -> str | None:
    """The user's saved model tier, or None (falls back to the config default)."""
    with pool.connection() as conn:
        row = conn.execute(
            "SELECT model_tier FROM users WHERE id = %s", (user_id,)
        ).fetchone()
    return row[0] if row and row[0] else None


def set_user_tier(pool: ConnectionPool, user_id: str, tier: str) -> None:
    with pool.connection() as conn:
        conn.execute(
            "UPDATE users SET model_tier = %s WHERE id = %s", (tier, user_id)
        )


def ensure_user(pool: ConnectionPool, username: str) -> str:
    """Return the user's id, creating the row if needed (M17 adds credentials)."""
    with pool.connection() as conn:
        row = conn.execute(
            "SELECT id FROM users WHERE username = %s", (username,)
        ).fetchone()
        if row:
            return row[0]
        conn.execute(
            "INSERT INTO users (id, username) VALUES (%s, %s)"
            " ON CONFLICT (username) DO NOTHING",
            (uuid4().hex, username),
        )
        return conn.execute(
            "SELECT id FROM users WHERE username = %s", (username,)
        ).fetchone()[0]
