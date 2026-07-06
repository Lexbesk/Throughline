"""Account provisioning and credential checks (v4 M17).

There is no signup: accounts are created by the admin via the CLI
(``meeting-notes-todos user add``). A user row without a password hash (e.g.
one created by the M16 default-user path) cannot log in.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import uuid4

from psycopg_pool import ConnectionPool

from .passwords import DUMMY_HASH, hash_password, verify_password


@dataclass(frozen=True)
class User:
    id: str
    username: str


def provision_user(pool: ConnectionPool, username: str, password: str) -> User:
    """Create an account with credentials; errors if the username exists."""
    username = username.strip()
    if not username:
        raise ValueError("username is empty")
    with pool.connection() as conn:
        existing = conn.execute(
            "SELECT id FROM users WHERE username = %s", (username,)
        ).fetchone()
        if existing:
            raise ValueError(f"user {username!r} already exists (use `user passwd` to reset)")
        user_id = uuid4().hex
        conn.execute(
            "INSERT INTO users (id, username, password_hash) VALUES (%s, %s, %s)",
            (user_id, username, hash_password(password)),
        )
    return User(id=user_id, username=username)


def set_password(pool: ConnectionPool, username: str, password: str) -> User:
    """Reset a password (admin CLI) — also used by the change-password endpoint."""
    with pool.connection() as conn:
        row = conn.execute(
            "UPDATE users SET password_hash = %s WHERE username = %s RETURNING id",
            (hash_password(password), username.strip()),
        ).fetchone()
    if row is None:
        raise ValueError(f"no such user {username!r}")
    return User(id=row[0], username=username.strip())


def authenticate(pool: ConnectionPool, username: str, password: str) -> User | None:
    """Verify credentials; None on any failure (unknown user, no password set,
    wrong password) — indistinguishable to the caller by design."""
    with pool.connection() as conn:
        row = conn.execute(
            "SELECT id, password_hash FROM users WHERE username = %s",
            (username.strip(),),
        ).fetchone()
    if row is None or not row[1]:
        verify_password(DUMMY_HASH, password)  # burn comparable time
        return None
    if not verify_password(row[1], password):
        return None
    return User(id=row[0], username=username.strip())


def list_users(pool: ConnectionPool) -> list[tuple[str, bool]]:
    """(username, can_log_in) pairs for the admin CLI."""
    with pool.connection() as conn:
        rows = conn.execute(
            "SELECT username, password_hash IS NOT NULL FROM users ORDER BY username"
        ).fetchall()
    return [(r[0], r[1]) for r in rows]
