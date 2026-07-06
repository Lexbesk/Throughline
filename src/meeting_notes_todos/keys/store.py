"""Per-user API key storage (v4 M18).

Scoped by ``user_id`` on every query — a user can only ever see or change their
own keys (the same hard-isolation rule as todos and profile). Plaintext keys are
never persisted (only the Fernet ciphertext) and never returned by the listing
path — ``list_keys`` exposes just the provider, a 4-char mask, and a timestamp.
``get_key`` decrypts, and is called only when a request actually needs to reach
the provider.
"""

from __future__ import annotations

from psycopg_pool import ConnectionPool

from .crypto import KeyCipher

# providers that use a stored per-user key (local endpoints use LOCAL_API_KEY/none)
KEY_PROVIDERS = ("anthropic", "openai")


class ApiKeyStore:
    def __init__(self, pool: ConnectionPool, user_id: str, cipher: KeyCipher) -> None:
        self.pool = pool
        self.user_id = user_id
        self._cipher = cipher

    def set_key(self, provider: str, api_key: str) -> None:
        api_key = api_key.strip()
        if provider not in KEY_PROVIDERS:
            raise ValueError(f"unknown provider {provider!r}")
        if not api_key:
            raise ValueError("api key is empty")
        encrypted = self._cipher.encrypt(api_key)  # plaintext never touches the DB
        with self.pool.connection() as conn:
            conn.execute(
                "INSERT INTO user_api_keys (user_id, provider, encrypted_key, last4, updated_at)"
                " VALUES (%s, %s, %s, %s, now())"
                " ON CONFLICT (user_id, provider) DO UPDATE"
                " SET encrypted_key = EXCLUDED.encrypted_key, last4 = EXCLUDED.last4,"
                "     updated_at = now()",
                (self.user_id, provider, encrypted, api_key[-4:]),
            )

    def get_key(self, provider: str) -> str | None:
        """Decrypt this user's key for ``provider`` (used only to make a request)."""
        with self.pool.connection() as conn:
            row = conn.execute(
                "SELECT encrypted_key FROM user_api_keys WHERE user_id = %s AND provider = %s",
                (self.user_id, provider),
            ).fetchone()
        return self._cipher.decrypt(row[0]) if row else None

    def delete_key(self, provider: str) -> bool:
        with self.pool.connection() as conn:
            cur = conn.execute(
                "DELETE FROM user_api_keys WHERE user_id = %s AND provider = %s",
                (self.user_id, provider),
            )
            return cur.rowcount > 0

    def list_keys(self) -> list[dict]:
        """Masked view for the UI — never the key material."""
        with self.pool.connection() as conn:
            rows = conn.execute(
                "SELECT provider, last4, updated_at FROM user_api_keys"
                " WHERE user_id = %s ORDER BY provider",
                (self.user_id,),
            ).fetchall()
        return [
            {"provider": r[0], "last4": r[1], "updated_at": r[2].isoformat()}
            for r in rows
        ]
