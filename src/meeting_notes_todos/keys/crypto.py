"""Symmetric encryption for per-user API keys (v4 M18).

Fernet (AES-128-CBC + HMAC-SHA256, authenticated) from ``cryptography``. The
**master key never lives in the repo or the database** — it comes from the
environment (a ``.env`` line in dev, a platform secret in production, set at
M19). Generate one with ``meeting-notes-todos keygen``.

Rotating the master key would invalidate stored ciphertext, so it is a single
long-lived secret; re-keying (decrypt-with-old, encrypt-with-new) is a future
concern, out of M18 scope.
"""

from __future__ import annotations

import os

from cryptography.fernet import Fernet

MASTER_KEY_ENV = "THROUGHLINE_MASTER_KEY"


class KeyCipher:
    def __init__(self, master_key: str | bytes) -> None:
        self._fernet = Fernet(master_key)

    def encrypt(self, plaintext: str) -> str:
        return self._fernet.encrypt(plaintext.encode("utf-8")).decode("ascii")

    def decrypt(self, token: str) -> str:
        return self._fernet.decrypt(token.encode("ascii")).decode("utf-8")


def generate_master_key() -> str:
    """A fresh Fernet master key (url-safe base64) for the platform secret store."""
    return Fernet.generate_key().decode("ascii")


def get_cipher() -> KeyCipher:
    """Build the cipher from the environment master key (re-read each call so a
    test can set it without import-order surprises)."""
    key = os.environ.get(MASTER_KEY_ENV)
    if not key:
        raise RuntimeError(
            f"{MASTER_KEY_ENV} is not set — per-user key storage needs a master "
            "encryption key. Generate one with `meeting-notes-todos keygen` and "
            "provide it via the environment (a .env line in dev; a platform "
            "secret in production)."
        )
    return KeyCipher(key)
