"""Server-side sessions (v4 M17; keyed hashing added in M19).

The browser holds a random opaque token in an HttpOnly cookie; the database
holds only a hash of the token, so a leaked database can't impersonate anyone.
Sessions expire after ``SESSION_TTL_DAYS`` and can be revoked server-side
(logout deletes the row; a password change deletes all of a user's rows).

v4 M19 hardening: when ``THROUGHLINE_SESSION_SECRET`` is set (a required
production secret, from platform config — never the repo/DB), tokens are hashed
with **HMAC-SHA256 keyed by that secret** instead of a bare SHA-256. The secret
never leaves the server, so a database dump can't be used to verify guessed
tokens offline. In dev, with no secret set, it falls back to plain SHA-256.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets

from psycopg_pool import ConnectionPool

from .accounts import User

COOKIE_NAME = "throughline_session"
SESSION_TTL_DAYS = 30
SESSION_SECRET_ENV = "THROUGHLINE_SESSION_SECRET"


def _token_hash(token: str) -> str:
    secret = os.environ.get(SESSION_SECRET_ENV)
    if secret:  # keyed hash — the key lives only in platform secrets (M19)
        return hmac.new(secret.encode("utf-8"), token.encode("utf-8"), hashlib.sha256).hexdigest()
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_session(pool: ConnectionPool, user_id: str) -> str:
    """Create a session and return the raw token (only its hash is stored)."""
    token = secrets.token_urlsafe(32)
    with pool.connection() as conn:
        conn.execute("DELETE FROM sessions WHERE expires_at < now()")  # opportunistic purge
        conn.execute(
            "INSERT INTO sessions (token_hash, user_id, expires_at)"
            " VALUES (%s, %s, now() + make_interval(days => %s))",
            (_token_hash(token), user_id, SESSION_TTL_DAYS),
        )
    return token


def session_user(pool: ConnectionPool, token: str) -> User | None:
    with pool.connection() as conn:
        row = conn.execute(
            "SELECT u.id, u.username FROM sessions s"
            " JOIN users u ON u.id = s.user_id"
            " WHERE s.token_hash = %s AND s.expires_at > now()",
            (_token_hash(token),),
        ).fetchone()
    return User(id=row[0], username=row[1]) if row else None


def delete_session(pool: ConnectionPool, token: str) -> None:
    with pool.connection() as conn:
        conn.execute("DELETE FROM sessions WHERE token_hash = %s", (_token_hash(token),))


def delete_user_sessions(pool: ConnectionPool, user_id: str) -> None:
    """Revoke everything for a user (used on password change)."""
    with pool.connection() as conn:
        conn.execute("DELETE FROM sessions WHERE user_id = %s", (user_id,))
